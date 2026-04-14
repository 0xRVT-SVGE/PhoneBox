# ============================================================
# FILE: back_end/slot_monitor/ops_handler.py
# ============================================================

import logging
import threading
from flask_socketio import emit, SocketIO
from flask import request

from back_end.slot_monitor.services.operation_context import op_ctx
from back_end.slot_monitor.camera.qr_pid_reader import scan_and_validate_pid_from_buffer
from back_end.slot_monitor.camera.top_camera import top_camera
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB
from back_end.slot_monitor.phone_tracker import (
    create_tracker_for_operation,
    make_dvw_context_overlay,
    load_all_top_rois,
)

logger = logging.getLogger(__name__)

QR_SCAN_TIMEOUT = 15.0


class DVWSocketHandler:

    def __init__(self, slot_operations: SlotOperations, socketio: SocketIO):
        self.slot_ops = slot_operations
        self.socketio = socketio

    # ══════════════════════════════════════════════════════
    # CANCEL
    # ══════════════════════════════════════════════════════

    def handle_cancel_operation(self, data: dict):
        client_id = request.sid
        op = op_ctx.get(client_id)
        if op is None:
            emit("operation_error", {"status": "error", "message": "no_active_operation"})
            return

        logger.info(
            f"[DVW] Cancel: client={client_id} "
            f"op_type={op.op_type} PID={op.pid} stage={op.stage}"
        )
        top_camera.clear_context_overlay()
        op_ctx.clear(client_id)
        emit("operation_cancelled", {"status": "success", "pid": op.pid})

    # ══════════════════════════════════════════════════════
    # DEPOSIT
    # ══════════════════════════════════════════════════════

    def handle_deposit(self, data: dict):
        client_id = request.sid
        pid = data.get("pid")
        if not pid:
            emit("operation_error", {"status": "error", "message": "missing_pid"})
            return

        pid = str(pid)
        if not SlotMonitorDB.pid_exists(pid):
            emit("operation_error", {"status": "error", "message": "pid_not_found", "pid": pid})
            return
        if SlotMonitorDB.is_phone_stored(pid):
            emit("operation_error", {"status": "error", "message": "phone_already_stored", "pid": pid})
            return

        lid = SlotMonitorDB.get_next_free_lid()
        if lid is None:
            emit("operation_error", {"status": "error", "message": "no_free_slots"})
            return

        self._pause_slot(lid)
        try:
            op_ctx.start(client_id, "deposit", pid=pid, lid=lid)
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(lid, is_occupied=False)
            return

        # Capture background frame NOW — slot is empty, phone not yet present.
        # top_camera.start() is called lazily in handle_qr_scanned but may already
        # be running.  If the camera hasn't warmed up yet the frame will be None;
        # create_tracker_for_operation handles that with a retry wait.
        op = op_ctx.get(client_id)
        if op is not None:
            op.background_frame = top_camera.get_frame()

        emit("deposit_waiting_for_qr", {
            "status":  "waiting",
            "pid":     pid,
            "lid":     lid,
            "slot":    lid + 1,
            "message": f"Hold QR for phone {pid} under the top camera, then carry it to slot {lid + 1}",
        })

        # Auto-start QR scan — no button press needed.
        threading.Thread(
            target=self._scan_and_dispatch,
            args=(client_id,),
            daemon=True,
            name=f"QRScan-deposit-{pid[:8]}",
        ).start()

    # ══════════════════════════════════════════════════════
    # WITHDRAW
    # ══════════════════════════════════════════════════════

    def handle_withdraw(self, data: dict):
        client_id = request.sid
        pid = data.get("pid")
        if not pid:
            emit("operation_error", {"status": "error", "message": "missing_pid"})
            return

        pid = str(pid)
        lid = SlotMonitorDB.get_lid_for_pid(pid)
        if lid is None:
            emit("operation_error", {"status": "error", "message": "phone_not_in_storage", "pid": pid})
            return

        self._pause_slot(lid)
        try:
            op_ctx.start(client_id, "withdraw", pid=pid, lid=lid)
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(lid, is_occupied=True)
            return

        emit("withdraw_waiting_for_action", {
            "status":  "waiting",
            "pid":     pid,
            "lid":     lid,
            "slot":    lid + 1,
            "message": f"Remove phone {pid} from slot {lid + 1}, then hold its QR under the top camera",
        })

        # Auto-start QR scan — no button press needed.
        threading.Thread(
            target=self._scan_and_dispatch,
            args=(client_id,),
            daemon=True,
            name=f"QRScan-withdraw-{pid[:8]}",
        ).start()

    # ══════════════════════════════════════════════════════
    # VERIFY
    # ══════════════════════════════════════════════════════

    def handle_verify(self, data: dict):
        client_id    = request.sid
        pid          = data.get("pid")
        original_lid = data.get("original_lid")

        if not pid or original_lid is None:
            emit("operation_error", {"status": "error", "message": "missing_parameters"})
            return

        pid          = str(pid)
        original_lid = int(original_lid)

        target_lid = SlotMonitorDB.get_lid_for_pid(pid)
        if target_lid is None:
            emit("operation_error", {
                "status":  "error",
                "message": "phone_not_registered_to_any_slot",
                "pid":     pid,
            })
            return

        if target_lid != original_lid:
            blocking_pid = SlotMonitorDB.get_pid_for_lid(target_lid)
            if blocking_pid is not None and blocking_pid != pid:
                emit("operation_error", {
                    "status":       "error",
                    "message":      "swap_requires_admin_resolution",
                    "pid":          pid,
                    "original_lid": original_lid,
                    "target_lid":   target_lid,
                    "blocking_pid": blocking_pid,
                })
                return

        self._pause_slot(original_lid)
        if target_lid != original_lid:
            self._pause_slot(target_lid)

        try:
            op_ctx.start(
                client_id, "verify",
                pid=pid, lid=target_lid, original_lid=original_lid,
            )
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(original_lid, is_occupied=True)
            if target_lid != original_lid:
                self._restore_slot(target_lid, is_occupied=False)
            return

        # NOTE: background_frame is NOT captured here.
        # At this point the phone is still physically in original_lid,
        # so capturing now would include the phone in the background image,
        # defeating the frame-diff detection.
        # It is captured in _complete_verify, after QR scan confirms the
        # phone is in the admin's hand and the destination slot is clear.

        same_slot = (target_lid == original_lid)
        emit("verify_waiting_for_action", {
            "status":        "waiting",
            "pid":           pid,
            "original_lid":  original_lid,
            "original_slot": original_lid + 1,
            "target_lid":    target_lid,
            "target_slot":   target_lid + 1,
            "same_slot":     same_slot,
            "message": (
                f"Take phone {pid} from slot {original_lid + 1}, "
                "scan QR, place back in same slot."
                if same_slot else
                f"Take phone {pid} from slot {original_lid + 1}, "
                f"scan QR, place in slot {target_lid + 1}."
            ),
        })

    # ══════════════════════════════════════════════════════
    # QR SCANNED  (verify only — deposit/withdraw auto-scan above)
    # ══════════════════════════════════════════════════════

    def handle_qr_scanned(self, data: dict):
        """
        Client-triggered QR scan.  Only meaningful for *verify* operations;
        deposit and withdraw start scanning automatically in handle_deposit /
        handle_withdraw so this event is a no-op for those op types.
        """
        client_id = request.sid
        op = op_ctx.get(client_id)
        if op is None:
            emit("operation_error", {"status": "error", "message": "no_active_operation"})
            return
        if op.op_type in ("deposit", "withdraw"):
            # Scan already running in background — ignore.
            return
        # verify: kick off scan in a background thread so we don’t block SocketIO.
        threading.Thread(
            target=self._scan_and_dispatch,
            args=(client_id,),
            daemon=True,
            name=f"QRScan-verify-{op.pid[:8]}",
        ).start()

    # ══════════════════════════════════════════════════════
    # SCAN WORKER  (runs in daemon thread for all op types)
    # ══════════════════════════════════════════════════════

    def _scan_and_dispatch(self, client_id: str) -> None:
        """
        Blocking QR scan + dispatch to the correct completion handler.
        Always runs in a daemon thread — uses self.socketio.emit (never Flask emit).
        """
        op = op_ctx.get(client_id)
        if op is None:
            return

        top_camera.start()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(True)
        except Exception:
            pass

        scan_result = scan_and_validate_pid_from_buffer(
            top_camera,
            timeout_sec=QR_SCAN_TIMEOUT,
            cancel_event=op.cancel_event,
        )
        self._top_buffer_idle()

        if op.cancel_event.is_set():
            return

        if scan_result["status"] != "success":
            self.socketio.emit(
                "operation_error", scan_result, to=client_id, namespace="/"
            )
            op_ctx.clear(client_id)
            return

        scanned_pid = scan_result["pid"]
        if scanned_pid != op.pid:
            self.socketio.emit(
                "operation_error",
                {
                    "status":       "error",
                    "message":      "pid_mismatch",
                    "expected_pid": op.pid,
                    "scanned_pid":  scanned_pid,
                },
                to=client_id, namespace="/",
            )
            op_ctx.clear(client_id)
            return

        op_ctx.qr_scanned(client_id)

        dispatch = {
            "deposit":  self._complete_deposit,
            "withdraw": self._complete_withdraw,
            "verify":   self._complete_verify,
        }
        dispatch[op.op_type](op)

    # ══════════════════════════════════════════════════════
    # COMPLETION HANDLERS
    # ══════════════════════════════════════════════════════

    def _complete_deposit(self, op):
        """
        QR confirmed for deposit.
        Sets DVW context overlay (destination slot highlighted),
        then launches PhoneTracker.
        Falls back to _finalize_deposit directly if tracker unavailable.
        """
        client_id = op.client_id
        pid       = op.pid
        lid       = op.lid

        # Set context overlay before tracker starts
        all_rois = load_all_top_rois()
        if all_rois:
            top_camera.set_context_overlay(
                make_dvw_context_overlay(
                    all_rois   = all_rois,
                    source_lid = None,   # deposit has no source slot
                    dest_lid   = lid,
                )
            )

        tracker = create_tracker_for_operation(op, self.socketio)

        if tracker is None:
            logger.warning(
                f"[DVW] Tracker unavailable for deposit PID={pid} lid={lid} "
                "— completing without motion verification"
            )
            top_camera.clear_context_overlay()
            self._finalize_deposit(op)
            return

        op_ctx.set_tracking(client_id)
        self.socketio.emit(
            "tracking_started",
            {
                "pid":     pid,
                "lid":     lid,
                "slot":    lid + 1,
                "message": f"Place phone {pid} in slot {lid + 1}. Keep the QR visible.",
            },
            to=client_id, namespace="/",
        )
        tracker.start(
            on_success = lambda: self._finalize_deposit(op),
            on_failure = lambda reason: self._on_tracking_failed(op, reason),
        )

    def _finalize_deposit(self, op):
        """
        Called by PhoneTracker on success, or directly as fallback.
        Runs in tracker daemon thread — uses socketio.emit().
        """
        pid, lid, client_id = op.pid, op.lid, op.client_id

        top_camera.clear_context_overlay()

        baseline_result = self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=True, wait_for_stable=2.0
        )
        if baseline_result["status"] != "success":
            self.socketio.emit(
                "deposit_result",
                {"status": "error", "message": "baseline_capture_failed",
                 "pid": pid, "lid": lid},
                to=client_id, namespace="/",
            )
            op_ctx.clear(client_id)
            return

        db_result = self.slot_ops.deposit_phone_db(pid, lid)
        if db_result["status"] != "success":
            self.socketio.emit("deposit_result", db_result, to=client_id, namespace="/")
            op_ctx.clear(client_id)
            return

        self._resume_slot(lid)
        op_ctx.complete(client_id)
        self.socketio.emit(
            "deposit_result",
            {
                "status":     "success",
                "pid":        pid,
                "lid":        lid,
                "slot":       lid + 1,
                "storage_id": db_result.get("storage_id"),
            },
            to=client_id, namespace="/",
        )

    def _on_tracking_failed(self, op, reason: str):
        """Called by PhoneTracker on any failure. Runs in tracker daemon thread."""
        client_id = op.client_id
        top_camera.clear_context_overlay()
        op_ctx.clear(client_id)

        messages = {
            "qr_lost":
                "QR code disappeared before the phone reached the slot. "
                "This may indicate a substitution attempt. Please retry.",
            "out_of_frame":
                "Phone left the camera view before reaching the slot. Please retry.",
            "timeout":      "Placement timed out. Please retry.",
            "detect_timeout":
                "Phone not detected entering the camera view. Please retry.",
            "cancelled":    "Operation was cancelled.",
        }
        self.socketio.emit(
            "tracking_failed",
            {
                "status":  "error",
                "reason":  reason,
                "message": messages.get(reason, f"Tracking failed: {reason}"),
            },
            to=client_id, namespace="/",
        )

    def _complete_withdraw(self, op):
        pid, lid, client_id = op.pid, op.lid, op.client_id

        db_result = self.slot_ops.withdraw_phone_db(pid)
        if db_result["status"] != "success":
            emit("withdraw_result", db_result)
            op_ctx.clear(client_id)
            return

        self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=False, wait_for_stable=2.0
        )
        self._resume_slot(lid)
        op_ctx.complete(client_id)
        emit("withdraw_result", {
            "status":     "success",
            "pid":        pid,
            "lid":        lid,
            "slot":       lid + 1,
            "storage_id": db_result.get("storage_id"),
        })

    def _complete_verify(self, op):
        """
        QR confirmed for verify.

        Background frame is captured HERE because the phone was still in
        original_lid when handle_verify ran — we couldn't use that frame.
        By now the QR scan has confirmed the phone is in the student's
        hand, so both original_lid and target_lid are clear.

        We wait for a fresh frame from the camera to ensure the background
        image doesn't include the phone.
        """
        client_id    = op.client_id
        pid          = op.pid
        target_lid   = op.lid
        original_lid = op.original_lid

        # Wait for a fresh frame (camera is running since QR scan just used it)
        top_camera.wait_for_frame(timeout=0.5)
        op.background_frame = top_camera.get_frame()
        top_camera.clear_frame_event()

        # Set context overlay: source (orange) + destination (yellow pulsing)
        all_rois = load_all_top_rois()
        if all_rois:
            top_camera.set_context_overlay(
                make_dvw_context_overlay(
                    all_rois   = all_rois,
                    source_lid = original_lid,
                    dest_lid   = target_lid,
                )
            )

        tracker = create_tracker_for_operation(op, self.socketio)

        if tracker is None:
            logger.warning(
                f"[DVW] Tracker unavailable for verify PID={pid} — "
                "completing without motion verification"
            )
            top_camera.clear_context_overlay()
            self._finalize_verify(op)
            return

        op_ctx.set_tracking(client_id)
        self.socketio.emit(
            "tracking_started",
            {
                "pid":     pid,
                "lid":     target_lid,
                "slot":    target_lid + 1,
                "message": (
                    f"Place phone {pid} in slot {target_lid + 1}. "
                    "Keep the QR visible."
                ),
            },
            to=client_id, namespace="/",
        )
        tracker.start(
            on_success = lambda: self._finalize_verify(op),
            on_failure = lambda reason: self._on_tracking_failed(op, reason),
        )

    def _finalize_verify(self, op):
        """
        Called by PhoneTracker on success, or directly as fallback.
        Runs in tracker daemon thread — uses socketio.emit().
        """
        pid, original_lid, target_lid, client_id = (
            op.pid, op.original_lid, op.lid, op.client_id
        )
        same_slot = (original_lid == target_lid)

        top_camera.clear_context_overlay()

        if not same_slot:
            if not SlotMonitorDB.update_storage_lid(pid, target_lid):
                self.socketio.emit(
                    "verify_result",
                    {"status": "error", "message": "database_update_failed"},
                    to=client_id, namespace="/",
                )
                op_ctx.clear(client_id)
                return

        self.slot_ops.capture_and_save_baseline(
            lid=target_lid, is_occupied=True, wait_for_stable=1.5
        )
        self._resume_slot(target_lid)

        if not same_slot:
            self.slot_ops.capture_and_save_baseline(
                lid=original_lid, is_occupied=False, wait_for_stable=1.5
            )
            self._resume_slot(original_lid)

        op_ctx.complete(client_id)
        self.socketio.emit(
            "verify_result",
            {
                "status":       "success",
                "pid":          pid,
                "original_lid": original_lid,
                "target_lid":   target_lid,
                "target_slot":  target_lid + 1,
            },
            to=client_id, namespace="/",
        )

    # ══════════════════════════════════════════════════════
    # SLOT HELPERS
    # ══════════════════════════════════════════════════════

    def _top_buffer_idle(self):
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

    def _worker_pool(self):
        m = self.slot_ops.monitor
        return m.worker_pool if m else None

    def _pause_slot(self, lid: int):
        wp = self._worker_pool()
        if wp:
            wp.pause_slot(lid)

    def _resume_slot(self, lid: int):
        wp = self._worker_pool()
        if wp:
            wp.resume_slot(lid)

    def _restore_slot(self, lid: int, is_occupied: bool):
        wp = self._worker_pool()
        if wp:
            wp.restore_slot(lid, is_occupied)


# ══════════════════════════════════════════════════════════
# REGISTRATION
# ══════════════════════════════════════════════════════════

def register_dvw_handlers(socketio: SocketIO, slot_operations: SlotOperations):
    handler = DVWSocketHandler(slot_operations, socketio)

    @socketio.on("cancel_operation")
    def on_cancel_operation(data):
        handler.handle_cancel_operation(data)

    @socketio.on("deposit")
    def on_deposit(data):
        handler.handle_deposit(data)

    @socketio.on("withdraw")
    def on_withdraw(data):
        handler.handle_withdraw(data)

    @socketio.on("verify")
    def on_verify(data):
        handler.handle_verify(data)

    @socketio.on("qr_scanned")
    def on_qr_scanned(data):
        handler.handle_qr_scanned(data)

    @socketio.on("get_alarm_status")
    def on_get_alarm_status(data):
        client_id = request.sid
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm is None:
            emit("alarm_status", {"active": False}, to=client_id)
            return
        status = alarm.get_status()
        if status["active"]:
            with alarm._lock:
                mismatches = [[p, l] for p, l in alarm.mismatches]
            emit("alarm_status", {
                "active":         True,
                "mismatch_count": len(mismatches),
                "mismatches":     mismatches,
            }, to=client_id)
        else:
            emit("alarm_status", {"active": False}, to=client_id)

    @socketio.on("alarm_acknowledge")
    def on_alarm_acknowledge(data):
        client_id = request.sid
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm is None:
            emit("operation_error",
                 {"status": "error", "message": "alarm_not_available"}, to=client_id)
            return
        result = alarm.authenticate_admin(data.get("password", ""))
        if result["authenticated"]:
            alarm.silence()
            emit("alarm_acknowledge_result", {"status": "success"}, to=client_id)
        else:
            emit("alarm_acknowledge_result",
                 {"status": "error", "message": "wrong_password"}, to=client_id)

    @socketio.on("alarm_unsilence")
    def on_alarm_unsilence(data):
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm:
            alarm.unsilence()

    logger.info("DVW WebSocket handlers registered")