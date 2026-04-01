# ============================================================
# FILE: back_end/slot_monitor/ops_handler.py
# ============================================================
"""
DVW (Deposit / Withdraw / Verify) WebSocket event handler.

ALL operations are camera-driven and fully automatic.
After the student initiates an operation, the server:
  1. Instructs the student via `operation_step` events.
  2. Watches cameras to confirm physical actions automatically.
  3. Writes to DB only after visual confirmation.
  4. Emits `operation_complete` or `operation_failed`.

The student never presses a confirmation button.

─────────────────────────────────────────────────────────────
Socket events  (client → server)
─────────────────────────────────────────────────────────────
  start_deposit   {pid, lid}                 Deposit phone in slot lid
  start_withdraw  {pid}                      Retrieve phone (lid looked up)
  start_verify    {pid, from_lid, to_lid}    Move phone from wrong slot to correct one
  cancel_operation {}                        Abort active operation

─────────────────────────────────────────────────────────────
Socket events  (server → client)
─────────────────────────────────────────────────────────────
  operation_step       {step, message, lid?, from_lid?, to_lid?}
      Instruction for the student.  `step` is a stable string key;
      the frontend can use it to show localised text or animations.

  operation_tracking   {bbox:[x,y,w,h], overlap:float, type:str}
      Live tracking data (only during deposit / verify tracking phase).
      Sent every camera frame (~15 fps) so the frontend can draw the
      bounding box on top of the WebRTC admin stream.

  operation_complete   {type, pid, lid}
      Terminal success event.

  operation_failed     {reason}
      Terminal failure event.  reason is a short string key.

  operation_cancelled  {}
      Emitted when the client calls cancel_operation.

─────────────────────────────────────────────────────────────
Step strings by operation type
─────────────────────────────────────────────────────────────
  Deposit
    deposit_scan_qr          →  "Hold your phone's QR code under the camera"
    deposit_place_phone      →  "Place your phone in the highlighted slot"
    deposit_tracking         →  "Tracking your phone — move it into the slot"
    deposit_settling         →  "Hold still…"
    deposit_complete         →  "✓ Phone stored successfully!"

  Withdraw
    withdraw_open_slot       →  "Open slot {lid} and take your phone"
    withdraw_detected        →  "Phone removal detected — you may close the lid"
    withdraw_complete        →  "✓ Phone retrieved successfully!"

  Verify
    verify_scan_qr           →  "Hold your phone's QR code under the camera"
    verify_move_to_slot      →  "Move your phone to the highlighted slot"
    verify_tracking          →  "Tracking — move the phone into the correct slot"
    verify_settling          →  "Hold still…"
    verify_complete          →  "✓ Phone verified and placed correctly!"

─────────────────────────────────────────────────────────────
Withdraw detection strategy
─────────────────────────────────────────────────────────────
  The slot monitor is PAUSED for the target slot during a withdraw so
  no false alarm fires.  In parallel, this handler polls the BOTTOM
  camera (slot monitor frame buffer) and computes the embedding distance
  from the slot's stored baseline.  When distance exceeds
  mismatch_threshold × WITHDRAW_DETECT_FACTOR for
  WITHDRAW_CONFIRM_SAMPLES consecutive samples, physical removal is
  confirmed.  This avoids the need for the student to press any button.
"""

import json
import logging
import os
import threading
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────────────────────────
_TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tools"
)
_ROIS_TOP_FILE = os.path.join(_TOOLS_DIR, "rois_top.json")

# ── Withdraw detection tuning ─────────────────────────────────────────────────
WITHDRAW_DETECT_FACTOR   = 1.8   # × mismatch_threshold → removal detected
WITHDRAW_CONFIRM_SAMPLES = 4     # consecutive above-threshold samples needed
WITHDRAW_POLL_INTERVAL   = 0.15  # seconds between bottom-cam checks
WITHDRAW_TIMEOUT         = 90.0  # seconds student has to remove phone

# ── Human-readable step messages (fallback; frontend should localise) ─────────
_STEP_MESSAGES: Dict[str, str] = {
    "deposit_scan_qr":     "Hold your phone's QR code under the top camera",
    "deposit_place_phone": "QR confirmed! Place your phone in the highlighted slot",
    "deposit_tracking":    "Tracking your phone — move it into the slot",
    "deposit_settling":    "Almost there — hold still…",
    "deposit_complete":    "✓ Phone stored successfully!",

    "withdraw_open_slot":  "Open slot {lid} and take your phone",
    "withdraw_detected":   "Removal detected — you may close the lid",
    "withdraw_complete":   "✓ Phone retrieved successfully!",

    "verify_scan_qr":      "Hold your phone's QR code under the top camera",
    "verify_move_to_slot": "QR confirmed! Move your phone to the highlighted slot",
    "verify_tracking":     "Tracking — move the phone into the correct slot",
    "verify_settling":     "Almost there — hold still…",
    "verify_complete":     "✓ Phone placed correctly!",
}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_top_rois() -> Dict[int, Tuple[int, int, int, int]]:
    """
    Load slot ROIs for the top-down camera from rois_top.json.

    The ROIs are used to initialise the PhoneTracker target region.
    Falls back to an empty dict on missing / malformed file — the
    tracker will then fail fast with a clear error message.
    """
    if not os.path.exists(_ROIS_TOP_FILE):
        logger.warning(
            f"[DVW] rois_top.json not found at {_ROIS_TOP_FILE}. "
            "Run roi_calibration.py to generate it. "
            "Deposit and verify operations will not track phones."
        )
        return {}
    try:
        with open(_ROIS_TOP_FILE, "r") as f:
            data = json.load(f)
        rois = {i: tuple(int(v) for v in r) for i, r in enumerate(data)}
        logger.info(f"[DVW] Loaded {len(rois)} top-camera ROIs")
        return rois
    except Exception as e:
        logger.error(f"[DVW] Failed to load rois_top.json: {e}")
        return {}


def _wait_for_slot_removal(
    lid:             int,
    slot_ops,
    cancel_event:    threading.Event,
    timeout:         float = WITHDRAW_TIMEOUT,
    detect_factor:   float = WITHDRAW_DETECT_FACTOR,
    confirm_samples: int   = WITHDRAW_CONFIRM_SAMPLES,
) -> bool:
    """
    Poll the bottom camera until the phone in `lid` has been physically removed.

    Detection algorithm
    ────────────────────
    Every WITHDRAW_POLL_INTERVAL seconds we:
      1. Read the latest frame from the slot monitor's frame buffer.
      2. Compute the embedding distance between the current frame and
         the stored baseline for this slot.
      3. If distance > mismatch_threshold × detect_factor for
         confirm_samples consecutive reads → return True.

    We do NOT rely on the async worker (it is paused during operations).
    We read the bottom camera directly through the synchronous frame buffer.

    Returns True if removal was confirmed, False on timeout or cancel.
    """
    if not slot_ops.monitor or not slot_ops.monitor.worker_pool:
        # Fallback: no monitor available — assume removal after short wait
        logger.warning(
            f"[DVW] No monitor available for slot {lid} removal detection. "
            "Falling back to 3 s wait."
        )
        time.sleep(3.0)
        return not cancel_event.is_set()

    worker = slot_ops.monitor.worker_pool._get_worker(lid)
    if worker is None:
        logger.warning(f"[DVW] No worker found for slot {lid}. Falling back.")
        time.sleep(3.0)
        return not cancel_event.is_set()

    slot = worker._slot_map.get(lid)
    if slot is None:
        logger.warning(f"[DVW] Slot {lid} not in worker map. Falling back.")
        time.sleep(3.0)
        return not cancel_event.is_set()

    # Read mismatch_threshold from the monitor config
    threshold = getattr(slot_ops.monitor, "mismatch_threshold", 0.15)
    detect_at = threshold * detect_factor

    consecutive = 0
    deadline    = time.time() + timeout

    while time.time() < deadline:
        if cancel_event.is_set():
            return False

        frame = slot_ops.monitor.frame_buffer.get_frame_sync()
        if frame is not None:
            try:
                dist = slot.compute_distance(frame)
                if dist > detect_at:
                    consecutive += 1
                    logger.debug(
                        f"[DVW] Slot {lid} removal sample {consecutive}/{confirm_samples}: "
                        f"dist={dist:.4f} (threshold={detect_at:.4f})"
                    )
                    if consecutive >= confirm_samples:
                        logger.info(
                            f"[DVW] ✓ Phone removal confirmed for slot {lid} "
                            f"(dist={dist:.4f})"
                        )
                        return True
                else:
                    consecutive = 0   # reset on any in-threshold reading
            except Exception as e:
                logger.debug(f"[DVW] Slot distance compute error: {e}")

        time.sleep(WITHDRAW_POLL_INTERVAL)

    logger.warning(f"[DVW] Removal detection timed out for slot {lid}")
    return False


def _pause_slot(slot_ops, lid: int) -> None:
    if slot_ops.monitor and slot_ops.monitor.worker_pool:
        slot_ops.monitor.worker_pool.pause_slot(lid)


def _resume_slot(slot_ops, lid: int) -> None:
    if slot_ops.monitor and slot_ops.monitor.worker_pool:
        slot_ops.monitor.worker_pool.resume_slot(lid)


def _restore_slot(slot_ops, lid: int, was_occupied: bool) -> None:
    if slot_ops.monitor and slot_ops.monitor.worker_pool:
        slot_ops.monitor.worker_pool.restore_slot(lid, was_occupied)


# ══════════════════════════════════════════════════════════════════════════════
# Register handlers
# ══════════════════════════════════════════════════════════════════════════════

def register_dvw_handlers(socketio, slot_ops) -> None:
    """
    Register all DVW WebSocket handlers with `socketio`.

    Called once from server_main after the slot monitor is ready.
    """
    from back_end.slot_monitor.services.operation_context import op_ctx
    from back_end.slot_monitor.camera.top_camera import top_camera
    from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
    from back_end.slot_monitor.phone_tracker import (
        PhoneTracker, make_operation_context_overlay,
    )
    from back_end.slot_monitor.camera.qr_pid_reader import (
        scan_and_validate_pid_from_buffer,
    )
    from back_end.slot_monitor.db_interface import SlotMonitorDB
    from flask import request

    # Load top-camera ROIs once at startup
    top_rois: Dict[int, Tuple[int, int, int, int]] = _load_top_rois()

    # ── Emit helpers ──────────────────────────────────────────────────────────

    def _step(client_id: str, key: str, **extra) -> None:
        msg = _STEP_MESSAGES.get(key, key)
        socketio.emit(
            "operation_step",
            {"step": key, "message": msg, **extra},
            to=client_id,
        )

    def _complete(client_id: str, op_type: str, pid: str, lid: int) -> None:
        socketio.emit(
            "operation_complete",
            {"type": op_type, "pid": pid, "lid": lid},
            to=client_id,
        )

    def _failed(client_id: str, reason: str) -> None:
        socketio.emit("operation_failed", {"reason": reason}, to=client_id)

    def _tracking(client_id: str, bbox, overlap: float, op_type: str) -> None:
        socketio.emit(
            "operation_tracking",
            {"bbox": list(bbox), "overlap": round(overlap, 3), "type": op_type},
            to=client_id,
        )

    # ── Shared cleanup ────────────────────────────────────────────────────────

    def _cleanup(op_type: str = ""):
        """Clear camera overlays and deactivate rolling buffer."""
        top_camera.clear_context_overlay()
        top_rolling_buffer.set_active(False)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # DEPOSIT
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @socketio.on("start_deposit")
    def handle_start_deposit(data: dict) -> None:
        client_id = request.sid
        pid = (data or {}).get("pid", "").strip()
        lid = (data or {}).get("lid")

        if not pid or lid is None:
            _failed(client_id, "missing_pid_or_lid")
            return

        lid = int(lid)

        if op_ctx.is_active(client_id):
            _failed(client_id, "operation_already_active")
            return

        # Verify the target slot is not already occupied
        if SlotMonitorDB.is_slot_occupied(lid):
            _failed(client_id, "slot_already_occupied")
            return

        # Verify phone is not already stored somewhere
        if SlotMonitorDB.is_phone_stored(pid):
            _failed(client_id, "phone_already_stored")
            return

        _pause_slot(slot_ops, lid)

        op_ctx.start(
            client_id=client_id, op_type="deposit", pid=pid, lid=lid
        )
        op = op_ctx.get(client_id)
        op.lid_occupied_before = False

        top_rolling_buffer.set_active(True)

        # Capture background before the student approaches
        op.background_frame = top_camera.get_frame()

        overlay_fn = make_operation_context_overlay(
            slot_rois=top_rois, target_lid=lid
        )
        top_camera.set_context_overlay(overlay_fn)

        _step(client_id, "deposit_scan_qr", lid=lid)
        logger.info(f"[Deposit] Started: PID={pid} LID={lid} client={client_id}")

        threading.Thread(
            target=_run_deposit,
            args=(client_id, pid, lid, op.cancel_event,
                  op.background_frame, overlay_fn),
            daemon=True,
            name=f"Deposit-{pid[:8]}-lid{lid}",
        ).start()

    def _run_deposit(
        client_id:    str,
        pid:          str,
        lid:          int,
        cancel_event: threading.Event,
        bg_frame,
        overlay_fn:   Callable,
    ) -> None:
        try:
            # ── Step 1: QR scan ───────────────────────────────────────────────
            scan = scan_and_validate_pid_from_buffer(
                frame_buffer=top_camera,
                timeout_sec=25.0,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                _cleanup()
                return

            if scan["status"] != "success":
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, scan.get("message", "qr_scan_failed"))
                return

            if scan["pid"] != pid:
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, "qr_pid_mismatch")
                return

            op_ctx.qr_scanned(client_id)
            _step(client_id, "deposit_place_phone", lid=lid)

            # Give the student a moment and refresh background after QR scan
            # (the phone briefly appeared, now it will be placed in the slot)
            time.sleep(0.6)
            fresh_bg = top_camera.get_frame() or bg_frame

            # Validate top-camera ROI exists
            target_roi = top_rois.get(lid)
            if target_roi is None:
                logger.error(f"[Deposit] No top-camera ROI for LID={lid}")
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, "no_top_roi_for_slot")
                return

            # ── Step 2: Track phone to slot ───────────────────────────────────
            op_ctx.set_tracking(client_id)

            confirmed = threading.Event()
            fail_reason: list = [None]

            def on_detected(bbox):
                _step(client_id, "deposit_tracking", lid=lid)

            def on_progress(bbox, overlap):
                _tracking(client_id, bbox, overlap, "deposit")
                if overlap > 0.3:
                    _step(client_id, "deposit_settling", lid=lid)

            def on_confirmed():
                confirmed.set()

            def on_timeout():
                fail_reason[0] = "placement_timeout"
                confirmed.set()

            def on_failed(reason: str):
                fail_reason[0] = reason
                confirmed.set()

            tracker = PhoneTracker(
                target_roi=target_roi,
                background_frame=fresh_bg,
                cancel_event=cancel_event,
                on_detected=on_detected,
                on_progress=on_progress,
                on_confirmed=on_confirmed,
                on_timeout=on_timeout,
                on_failed=on_failed,
            )
            tracker.start()
            confirmed.wait()   # blocks until tracker fires a terminal callback

            if cancel_event.is_set() or fail_reason[0]:
                _cleanup()
                op_ctx.clear(client_id)   # also restores slot via _restore_slots
                _failed(client_id, fail_reason[0] or "cancelled")
                return

            # ── Step 3: Write DB record ───────────────────────────────────────
            # Allow phone to settle so baseline is clean
            _step(client_id, "deposit_settling", lid=lid)
            time.sleep(1.5)

            db_result = slot_ops.deposit_phone_db(pid, lid)
            if db_result["status"] != "success":
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, db_result.get("message", "db_error"))
                return

            # ── Step 4: Capture new baseline ──────────────────────────────────
            baseline_result = slot_ops.capture_and_save_baseline(
                lid=lid, is_occupied=True, wait_for_stable=1.0
            )
            if baseline_result["status"] != "success":
                logger.warning(
                    f"[Deposit] Baseline capture failed for LID={lid}: "
                    f"{baseline_result.get('message')}"
                )

            _resume_slot(slot_ops, lid)
            op_ctx.complete(client_id)
            _cleanup()

            _step(client_id, "deposit_complete", lid=lid)
            _complete(client_id, "deposit", pid, lid)
            logger.info(f"[Deposit] ✓ PID={pid} LID={lid}")

        except Exception as e:
            logger.error(f"[Deposit] Unhandled error: {e}", exc_info=True)
            _cleanup()
            op_ctx.clear(client_id)
            _failed(client_id, "internal_error")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # WITHDRAW
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @socketio.on("start_withdraw")
    def handle_start_withdraw(data: dict) -> None:
        client_id = request.sid
        pid = (data or {}).get("pid", "").strip()

        if not pid:
            _failed(client_id, "missing_pid")
            return

        if op_ctx.is_active(client_id):
            _failed(client_id, "operation_already_active")
            return

        lid = SlotMonitorDB.get_lid_for_pid(pid)
        if lid is None:
            _failed(client_id, "phone_not_in_storage")
            return

        _pause_slot(slot_ops, lid)

        op_ctx.start(
            client_id=client_id, op_type="withdraw", pid=pid, lid=lid
        )
        op = op_ctx.get(client_id)
        op.lid_occupied_before = True

        top_rolling_buffer.set_active(True)

        # Show which slot to open
        overlay_fn = make_operation_context_overlay(
            slot_rois=top_rois, source_lid=lid
        )
        top_camera.set_context_overlay(overlay_fn)

        msg = _STEP_MESSAGES["withdraw_open_slot"].replace("{lid}", str(lid))
        socketio.emit(
            "operation_step",
            {"step": "withdraw_open_slot", "message": msg, "lid": lid},
            to=client_id,
        )
        logger.info(f"[Withdraw] Started: PID={pid} LID={lid} client={client_id}")

        threading.Thread(
            target=_run_withdraw,
            args=(client_id, pid, lid, op.cancel_event),
            daemon=True,
            name=f"Withdraw-{pid[:8]}-lid{lid}",
        ).start()

    def _run_withdraw(
        client_id:    str,
        pid:          str,
        lid:          int,
        cancel_event: threading.Event,
    ) -> None:
        try:
            # ── Step 1: Wait for physical removal via bottom camera ────────────
            removed = _wait_for_slot_removal(
                lid=lid,
                slot_ops=slot_ops,
                cancel_event=cancel_event,
                timeout=WITHDRAW_TIMEOUT,
            )

            if cancel_event.is_set():
                _cleanup()
                return

            if not removed:
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, "removal_timeout")
                return

            _step(client_id, "withdraw_detected", lid=lid)

            # ── Step 2: Write DB record ───────────────────────────────────────
            db_result = slot_ops.withdraw_phone_db(pid)
            if db_result["status"] != "success":
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, db_result.get("message", "db_error"))
                return

            # ── Step 3: Recapture empty-slot baseline ─────────────────────────
            baseline_result = slot_ops.capture_and_save_baseline(
                lid=lid, is_occupied=False, wait_for_stable=2.0
            )
            if baseline_result["status"] != "success":
                logger.warning(
                    f"[Withdraw] Baseline capture failed for LID={lid}: "
                    f"{baseline_result.get('message')}"
                )

            _resume_slot(slot_ops, lid)
            op_ctx.complete(client_id)
            _cleanup()

            _step(client_id, "withdraw_complete", lid=lid)
            _complete(client_id, "withdraw", pid, lid)
            logger.info(f"[Withdraw] ✓ PID={pid} LID={lid}")

        except Exception as e:
            logger.error(f"[Withdraw] Unhandled error: {e}", exc_info=True)
            _cleanup()
            op_ctx.clear(client_id)
            _failed(client_id, "internal_error")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # VERIFY (phone in wrong slot → move to correct slot)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @socketio.on("start_verify")
    def handle_start_verify(data: dict) -> None:
        client_id = request.sid
        pid      = (data or {}).get("pid", "").strip()
        from_lid = (data or {}).get("from_lid")
        to_lid   = (data or {}).get("to_lid")

        if not pid or from_lid is None or to_lid is None:
            _failed(client_id, "missing_params")
            return

        from_lid = int(from_lid)
        to_lid   = int(to_lid)

        if op_ctx.is_active(client_id):
            _failed(client_id, "operation_already_active")
            return

        if from_lid == to_lid:
            _failed(client_id, "same_slot")
            return

        # Validate DB state
        actual_lid = SlotMonitorDB.get_lid_for_pid(pid)
        if actual_lid != from_lid:
            _failed(client_id, "phone_not_at_from_lid")
            return

        if SlotMonitorDB.is_slot_occupied(to_lid):
            _failed(client_id, "destination_occupied")
            return

        # Pause both slots
        _pause_slot(slot_ops, from_lid)
        _pause_slot(slot_ops, to_lid)

        op_ctx.start(
            client_id=client_id,
            op_type="verify",
            pid=pid,
            lid=to_lid,
            original_lid=from_lid,
        )
        op = op_ctx.get(client_id)
        op.lid_occupied_before = False
        op.original_lid_occupied_before = True

        top_rolling_buffer.set_active(True)
        op.background_frame = top_camera.get_frame()

        overlay_fn = make_operation_context_overlay(
            slot_rois=top_rois, source_lid=from_lid, target_lid=to_lid
        )
        top_camera.set_context_overlay(overlay_fn)

        _step(client_id, "verify_scan_qr", from_lid=from_lid, to_lid=to_lid)
        logger.info(
            f"[Verify] Started: PID={pid} from={from_lid} to={to_lid} "
            f"client={client_id}"
        )

        threading.Thread(
            target=_run_verify,
            args=(client_id, pid, from_lid, to_lid,
                  op.cancel_event, op.background_frame, overlay_fn),
            daemon=True,
            name=f"Verify-{pid[:8]}-{from_lid}>{to_lid}",
        ).start()

    def _run_verify(
        client_id:    str,
        pid:          str,
        from_lid:     int,
        to_lid:       int,
        cancel_event: threading.Event,
        bg_frame,
        overlay_fn:   Callable,
    ) -> None:
        try:
            # ── Step 1: QR scan ───────────────────────────────────────────────
            scan = scan_and_validate_pid_from_buffer(
                frame_buffer=top_camera,
                timeout_sec=25.0,
                cancel_event=cancel_event,
            )

            if cancel_event.is_set():
                _cleanup()
                return

            if scan["status"] != "success":
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, scan.get("message", "qr_scan_failed"))
                return

            if scan["pid"] != pid:
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, "qr_pid_mismatch")
                return

            op_ctx.qr_scanned(client_id)
            _step(client_id, "verify_move_to_slot", from_lid=from_lid, to_lid=to_lid)

            time.sleep(0.6)
            fresh_bg = top_camera.get_frame() or bg_frame

            target_roi = top_rois.get(to_lid)
            if target_roi is None:
                logger.error(f"[Verify] No top-camera ROI for to_lid={to_lid}")
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, "no_top_roi_for_slot")
                return

            # ── Step 2: Track phone to target slot ────────────────────────────
            op_ctx.set_tracking(client_id)

            confirmed = threading.Event()
            fail_reason: list = [None]

            def on_detected(bbox):
                _step(client_id, "verify_tracking",
                      from_lid=from_lid, to_lid=to_lid)

            def on_progress(bbox, overlap):
                _tracking(client_id, bbox, overlap, "verify")
                if overlap > 0.3:
                    _step(client_id, "verify_settling",
                          from_lid=from_lid, to_lid=to_lid)

            def on_confirmed():
                confirmed.set()

            def on_timeout():
                fail_reason[0] = "placement_timeout"
                confirmed.set()

            def on_failed(reason: str):
                fail_reason[0] = reason
                confirmed.set()

            tracker = PhoneTracker(
                target_roi=target_roi,
                background_frame=fresh_bg,
                cancel_event=cancel_event,
                on_detected=on_detected,
                on_progress=on_progress,
                on_confirmed=on_confirmed,
                on_timeout=on_timeout,
                on_failed=on_failed,
            )
            tracker.start()
            confirmed.wait()

            if cancel_event.is_set() or fail_reason[0]:
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, fail_reason[0] or "cancelled")
                return

            # ── Step 3: DB update ─────────────────────────────────────────────
            _step(client_id, "verify_settling",
                  from_lid=from_lid, to_lid=to_lid)
            time.sleep(1.5)

            from back_end.slot_monitor.db_interface import SlotMonitorDB
            ok = SlotMonitorDB.update_storage_lid(pid, to_lid)
            if not ok:
                _cleanup()
                op_ctx.clear(client_id)
                _failed(client_id, "db_update_failed")
                return

            # ── Step 4: Baselines for both slots ──────────────────────────────
            # from_lid is now empty
            slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=0.5
            )
            # to_lid is now occupied
            slot_ops.capture_and_save_baseline(
                lid=to_lid, is_occupied=True, wait_for_stable=1.0
            )

            _resume_slot(slot_ops, from_lid)
            _resume_slot(slot_ops, to_lid)
            op_ctx.complete(client_id)
            _cleanup()

            _step(client_id, "verify_complete",
                  from_lid=from_lid, to_lid=to_lid)
            _complete(client_id, "verify", pid, to_lid)
            logger.info(f"[Verify] ✓ PID={pid} moved {from_lid}→{to_lid}")

        except Exception as e:
            logger.error(f"[Verify] Unhandled error: {e}", exc_info=True)
            _cleanup()
            op_ctx.clear(client_id)
            _failed(client_id, "internal_error")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # CANCEL
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @socketio.on("cancel_operation")
    def handle_cancel(_data) -> None:
        client_id = request.sid
        op = op_ctx.get(client_id)
        if not op:
            return
        logger.info(
            f"[DVW] Cancel requested: {op.op_type} PID={op.pid} "
            f"client={client_id}"
        )
        # op_ctx.clear() sets cancel_event → unblocks scanner + tracker threads
        op_ctx.clear(client_id)
        _cleanup()
        socketio.emit("operation_cancelled", {}, to=client_id)

    # ── Disconnect cleanup ────────────────────────────────────────────────────

    @socketio.on("disconnect")
    def handle_disconnect() -> None:
        client_id = request.sid
        if op_ctx.is_active(client_id):
            logger.warning(
                f"[DVW] Client {client_id} disconnected mid-operation — "
                "cleaning up"
            )
            op_ctx.clear(client_id)
            _cleanup()

    logger.info("[DVW] Handlers registered — fully automatic tracking mode")