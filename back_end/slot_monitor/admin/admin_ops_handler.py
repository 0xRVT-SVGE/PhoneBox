# ============================================================
# FILE: back_end/slot_monitor/admin/admin_ops_handler.py
# ============================================================
"""
Admin Resolution WebSocket handlers.

Overlay management
──────────────────
top_camera.set_context_overlay() is called after every action that
changes what the admin needs to see:

  Session open    → staging zones (empty, initial state)
  QR scanned      → staging zones + source slot (orange) + dest slot (yellow)
  Same-slot QR    → source == dest drawn with orange + corner brackets
  Stage phone     → staged PID fills staging zone (bright color)
  Unstage phone   → staging zone empties + source/dest shown again
  Place phone     → reset to staging zones only (no source/dest)
  No QR           → reset to staging zones only
  Session close   → clear all overlays

The overlay is built by make_admin_session_overlay() from phone_tracker.py.
"""

import json
import logging
import os
import threading
from typing import Optional

from flask import request
from flask_socketio import SocketIO, emit

from back_end.slot_monitor.admin.resolution_session import admin_ctx, SESSION_TIMEOUT
from back_end.slot_monitor.admin.evidence_recorder import EvidenceRecorder
from back_end.slot_monitor.camera.qr_pid_reader import scan_and_validate_pid_from_buffer
from back_end.slot_monitor.camera.top_camera import top_camera
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB
from back_end.slot_monitor.alarm_controller import AlarmController

logger = logging.getLogger(__name__)

TOP_CAMERA_INDEX = 2
QR_SCAN_TIMEOUT  = 90.0   # long scan — frontend shows "No QR" button after a delay

# ── StagingConfig ─────────────────────────────────────────

_ADMIN_DIR        = os.path.dirname(os.path.abspath(__file__))
_STAGING_ROI_FILE = os.path.join(_ADMIN_DIR, "staging_rois.json")
_FALLBACK_ROIS    = [(50, 50, 150, 150), (250, 50, 150, 150)]


class StagingConfig:
    """
    Pixel coordinates of the two physical staging zones.
    Written by staging_calibration.py, stored in admin/staging_rois.json.
    Completely separate from rois_top.json (slot ROIs used by the tracker).
    """
    _rois: Optional[list] = None

    @classmethod
    def get_rois(cls) -> list:
        if cls._rois is not None:
            return cls._rois
        if os.path.exists(_STAGING_ROI_FILE):
            try:
                with open(_STAGING_ROI_FILE) as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) == 2:
                    cls._rois = [tuple(int(v) for v in r) for r in data]
                    logger.info(f"[StagingConfig] Loaded 2 staging ROIs from {_STAGING_ROI_FILE}")
                    return cls._rois
            except Exception as e:
                logger.warning(f"[StagingConfig] Failed to load {_STAGING_ROI_FILE}: {e}")
        logger.warning(
            f"[StagingConfig] {_STAGING_ROI_FILE} not found — using fallback ROIs. "
            "Run staging_calibration.py to configure staging zones."
        )
        cls._rois = list(_FALLBACK_ROIS)
        return cls._rois

    @classmethod
    def invalidate(cls) -> None:
        cls._rois = None


# ══════════════════════════════════════════════════════════
# Handler
# ══════════════════════════════════════════════════════════

class AdminOpsHandler:

    def __init__(self, slot_ops: SlotOperations, alarm: AlarmController, socketio: SocketIO):
        self.slot_ops = slot_ops
        self.alarm    = alarm
        self.socketio = socketio
        self._recorder: Optional[EvidenceRecorder] = None

    # ── Overlay management ────────────────────────────────

    def _refresh_overlay(
        self,
        session,
        source_lid: Optional[int] = None,
        dest_lid:   Optional[int] = None,
    ) -> None:
        """
        Rebuild the top-camera context overlay from current session state.

        Call this after every action that changes any of:
          - staged_phones (stage / unstage / place)
          - source slot  (after QR scan)
          - destination  (after QR scan)
        """
        try:
            from back_end.slot_monitor.phone_tracker import (
                make_admin_session_overlay,
                load_all_top_rois,
            )
            staged_pids = list(session.staged_phones.keys()) if session else []
            # Build a 2-element list: zone 0 pid, zone 1 pid (None if empty)
            zone_pids = [
                staged_pids[0] if len(staged_pids) > 0 else None,
                staged_pids[1] if len(staged_pids) > 1 else None,
            ]
            top_camera.set_context_overlay(
                make_admin_session_overlay(
                    all_slot_rois = load_all_top_rois(),
                    staging_rois  = StagingConfig.get_rois(),
                    staged_pids   = zone_pids,
                    source_lid    = source_lid,
                    dest_lid      = dest_lid,
                )
            )
        except Exception as e:
            logger.warning(f"[AdminOps] Failed to refresh overlay: {e}")

    # ── Evidence helpers ──────────────────────────────────

    def _start_clip(self, pid: str, lid: int):
        if self._recorder:
            self._recorder.start_clip(pid, lid)

    def _stop_clip(self, keep: bool, reason: str = ""):
        if self._recorder:
            self._recorder.stop_clip(keep=keep, reason=reason)

    # ── SESSION OPEN ──────────────────────────────────────

    def handle_session_start(self, data: dict):
        try:
            self._handle_session_start(data)
        except Exception as exc:
            logger.error(
                f"[AdminSession] handle_session_start unhandled error: {exc}",
                exc_info=True,
            )
            if admin_ctx.is_active():
                admin_ctx.close()
            emit("admin_session_error", {"message": f"server_error: {exc}"})

    def _handle_session_start(self, data: dict):
        password = data.get("password", "")
        if not self.alarm.authenticate_admin(password)["authenticated"]:
            emit("admin_session_error", {"message": "wrong_password"})
            return

        if admin_ctx.is_active():
            emit("admin_session_error", {"message": "session_already_active"})
            return

        with self.alarm._lock:
            raw = list(self.alarm.mismatches)
        if not raw:
            emit("admin_session_error", {"message": "no_active_mismatches"})
            return

        initial_mismatches = {str(pid): int(lid) for pid, lid in raw}

        try:
            phone_count = SlotMonitorDB.count_stored_phones()
            if phone_count is None:
                phone_count = -1
        except Exception as e:
            logger.warning(f"[AdminSession] count_stored_phones() failed ({e})")
            phone_count = -1

        try:
            session = admin_ctx.open(
                client_id=request.sid,
                initial_mismatches=initial_mismatches,
                phone_count=phone_count,
            )
        except RuntimeError as exc:
            emit("admin_session_error", {"message": str(exc)})
            return

        top_camera.start()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(True)
        except Exception:
            pass

        # Start evidence recorder
        self._recorder = EvidenceRecorder(session.session_id)
        try:
            self._recorder.start()
        except Exception as e:
            logger.warning(
                f"[AdminSession] EvidenceRecorder.start() failed: {e}. "
                "Check that migration 004 has been applied."
            )
            self._recorder = None

        for lid in initial_mismatches.values():
            self._pause_slot(lid)

        # Initial overlay: staging zones empty, no source/dest yet
        self._refresh_overlay(session)

        emit("admin_session_opened", {
            "session_id":  session.session_id,
            "mismatches": [
                {"pid": pid, "expected_lid": lid}
                for pid, lid in initial_mismatches.items()
            ],
            "phone_count":  phone_count,
            "staging_rois": StagingConfig.get_rois(),
        })

    # ── STEP 1 — REMOVE PHONE FROM SLOT ──────────────────

    def handle_remove_phone(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        self._check_timeout(session)

        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "step_lock_violated",
                "detail": (
                    f"Phone {session.in_transit_pid!r} is already in hand. "
                    "Place or stage it before removing another."
                ),
            })
            return

        from_lid = data.get("from_lid")
        if from_lid is None:
            emit("admin_operation_error", {"message": "missing_from_lid"})
            return
        from_lid = int(from_lid)

        session.in_transit_from_lid      = from_lid
        session.in_transit_pid           = None
        session.in_transit_qr_confirmed  = False

        pid_at_lid = SlotMonitorDB.get_pid_for_lid(from_lid)
        if pid_at_lid:
            session.visited_pids.add(pid_at_lid)
        else:
            session.visited_pids.add(f"unknown-{from_lid}")

        self._start_clip(pid=f"pending-lid{from_lid}", lid=from_lid)

        # Overlay: highlight source slot only (destination unknown until QR)
        self._refresh_overlay(session, source_lid=from_lid, dest_lid=None)

        client_id = request.sid   # must be captured here — request context is valid

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"object removed from lid={from_lid}, auto-starting QR scan"
        )
        emit("admin_remove_ok", {
            "from_lid": from_lid,
            "message": (
                f"Slot {from_lid} selected. "
                "Scanning for QR code now — scan will run until found."
            ),
        })

        # Auto-start QR scan immediately — no extra button press needed.
        threading.Thread(
            target=self._scan_qr_background,
            args=(session, client_id),
            daemon=True,
            name="AdminQRScan",
        ).start()

    # ── STEP 2a — QR SCAN ────────────────────────────────────

    def handle_qr_scanned(self, data: dict):
        """
        Validation is synchronous; the blocking pyzbar scan runs in a
        daemon thread to avoid stalling the SocketIO event loop.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if session.in_transit_from_lid is None:
            emit("admin_operation_error", {
                "message": "no_object_removed",
                "detail": "Call admin_remove_phone before scanning QR.",
            })
            return
        if session.in_transit_qr_confirmed:
            emit("admin_operation_error", {"message": "qr_already_confirmed"})
            return

        self._check_timeout(session)
        client_id = request.sid

        threading.Thread(
            target=self._scan_qr_background,
            args=(session, client_id),
            daemon=True,
            name="AdminQRScan",
        ).start()

    def _scan_qr_background(self, session, client_id: str) -> None:
        """
        Runs in a daemon thread.  Uses self.socketio.emit (not Flask emit).
        Scans until QR found or cancel_event set.  Does NOT time out by itself
        — the frontend shows a 'No QR found' button after a delay instead.
        After a successful QR scan, auto-starts PhoneTracker immediately.
        """
        # Build a cancel event the QR scan can react to
        # (reuse session cancel or create a fresh one per scan)
        cancel_event = threading.Event()
        session._qr_scan_cancel = cancel_event

        scan = scan_and_validate_pid_from_buffer(
            top_camera,
            timeout_sec=QR_SCAN_TIMEOUT,
            cancel_event=cancel_event,
        )

        # If scan was cancelled by admin pressing "No QR found"
        if cancel_event.is_set() or (scan["status"] != "success" and
                                      scan.get("message") == "cancelled"):
            return   # handle_no_qr_found already taking over

        if scan["status"] != "success":
            self.socketio.emit(
                "admin_operation_error", scan, to=client_id, namespace="/"
            )
            return

        pid      = scan["pid"]
        from_lid = session.in_transit_from_lid

        # ── Case: phone has no storage record ────────────────────────────
        if not SlotMonitorDB.is_phone_stored(pid):
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} found in lid={from_lid} but has no storage record."
            )
            self._stop_clip(keep=False, reason="restarting_with_real_pid")
            self._start_clip(pid=pid, lid=from_lid)
            self._stop_clip(keep=True, reason="needs_deposit_no_storage_record")

            self.slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=1.5
            )
            self._resume_slot(from_lid)
            self.alarm.resolve(f"unknown-{from_lid}", from_lid)

            session.needs_deposit_pids.add(pid)
            session.in_transit_pid           = None
            session.in_transit_from_lid      = None
            session.in_transit_qr_confirmed  = False
            self._refresh_overlay(session)

            self.socketio.emit("admin_qr_result", {
                "pid":           pid,
                "needs_deposit": True,
                "message": (
                    f"Phone {pid} is not in the storage system. "
                    f"Slot {from_lid} has been cleared. "
                    "Initiate a normal deposit for this phone."
                ),
            }, to=client_id, namespace="/")
            return

        # ── Normal case ───────────────────────────────────────────────────
        self._stop_clip(keep=False, reason="restarting_with_real_pid")
        self._start_clip(pid=pid, lid=from_lid)

        expected_lid = SlotMonitorDB.get_lid_for_pid(pid)
        same_slot    = (expected_lid == from_lid)

        session.in_transit_pid          = pid
        session.in_transit_qr_confirmed = True
        session.current_same_slot       = same_slot

        self._refresh_overlay(session, source_lid=from_lid, dest_lid=expected_lid)

        if pid not in session.initial_mismatches:
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} was NOT in the initial mismatch list."
            )

        # Notify frontend of QR result — tracking starts automatically below
        self.socketio.emit("admin_qr_result", {
            "pid":           pid,
            "expected_lid":  expected_lid,
            "needs_deposit": False,
            "same_slot":     same_slot,
            "auto_tracking": True,   # tells frontend: tracking is starting now
            "message":       f"Phone {pid}: moving to slot {expected_lid + 1}.",
        }, to=client_id, namespace="/")

        # ── Auto-start tracking immediately ──────────────────────────────
        self._launch_tracker(session, pid, from_lid, expected_lid, same_slot, client_id)

    # ── STEP 2b — NO QR FOUND ────────────────────────────

    def _launch_tracker(
        self,
        session,
        pid:       str,
        from_lid:  Optional[int],
        to_lid:    int,
        same_slot: bool,
        client_id: str,
    ) -> None:
        """
        Start PhoneTracker immediately after QR is confirmed.
        Passes staging ROIs so the tracker can auto-detect staging placement.
        Called from _scan_qr_background (daemon thread).
        """
        self._pause_slot(to_lid)

        cancel_event = threading.Event()
        session.placement_cancel_event = cancel_event

        top_camera.start()
        top_camera.wait_for_frame(timeout=0.5)
        raw = top_camera.get_raw_frame()
        bg_frame = raw if raw is not None else top_camera.get_frame()

        from back_end.slot_monitor.phone_tracker import PhoneTracker, _load_top_roi
        slot_roi     = _load_top_roi(to_lid)
        staging_rois = StagingConfig.get_rois()

        self.socketio.emit("tracking_started", {
            "pid":     pid,
            "lid":     to_lid,
            "slot":    to_lid + 1,
            "message": (
                f"Move phone {pid} to slot {to_lid + 1}. "
                "Keep QR visible — or place in staging zone if slot is occupied."
            ),
        }, to=client_id, namespace="/")

        if slot_roi is None or bg_frame is None:
            logger.warning(
                f"[AdminOps] No slot ROI or bg frame for lid={to_lid} — "
                "falling back to immediate confirmation."
            )
            self._admin_finalize_placement(session, to_lid, pid, from_lid, same_slot, client_id)
            return

        tracker = PhoneTracker(
            pid              = pid,
            lid              = to_lid,
            slot_roi         = slot_roi,
            background_frame = bg_frame,
            cancel_event     = cancel_event,
            socketio         = self.socketio,
            client_id        = client_id,
            staging_rois     = staging_rois,
        )
        tracker.start(
            on_success = lambda: self._admin_finalize_placement(
                session, to_lid, pid, from_lid, same_slot, client_id
            ),
            on_failure = lambda reason: self._admin_placement_failed(
                session, to_lid, pid, from_lid, reason, client_id
            ),
            on_staged  = lambda zone_idx: self._admin_handle_staged(
                session, pid, from_lid, to_lid, zone_idx, client_id
            ),
        )

    def _admin_handle_staged(
        self,
        session,
        pid:       str,
        from_lid:  Optional[int],
        dest_lid:  int,
        zone_idx:  int,
        client_id: str,
    ) -> None:
        """
        Called by PhoneTracker when the phone is placed in a staging zone.
        Records the staging, resumes the destination slot, emits event,
        then auto-selects the blocking phone at dest_lid for resolution.
        """
        top_camera.clear_context_overlay()
        session.placement_cancel_event = None

        session.staged_phones[pid]       = from_lid
        session.in_transit_pid           = None
        session.in_transit_from_lid      = None
        session.in_transit_qr_confirmed  = False

        self._stop_clip(keep=False, reason="auto_staged")
        # Resume destination — admin will clear it now
        self._resume_slot(dest_lid)

        remaining = sorted(session.pending_pids())
        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} auto-staged in zone {zone_idx}. "
            f"dest_lid={dest_lid} remaining={remaining}"
        )

        # Determine what is currently physically in dest_lid
        blocking_pid = SlotMonitorDB.get_pid_for_lid(dest_lid)

        self.socketio.emit("admin_auto_staged", {
            "pid":          pid,
            "zone_idx":     zone_idx,
            "dest_lid":     dest_lid,
            "blocking_pid": blocking_pid,
            "remaining":    remaining,
            "staged":       list(session.staged_phones.keys()),
        }, to=client_id, namespace="/")

    def handle_no_qr_found(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if session.in_transit_from_lid is None:
            emit("admin_operation_error", {
                "message": "no_object_removed",
                "detail":  "Call admin_remove_phone before reporting no QR.",
            })
            return
        if session.in_transit_qr_confirmed:
            emit("admin_operation_error", {
                "message": "qr_already_confirmed",
                "detail":  "QR confirmed — place the phone instead.",
            })
            return

        # Cancel any running QR scan
        cancel_ev = getattr(session, "_qr_scan_cancel", None)
        if cancel_ev is not None:
            cancel_ev.set()

        from_lid = session.in_transit_from_lid
        self._stop_clip(keep=True, reason="no_qr_found_unidentified_object")

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"Foreign object (no QR) removed from lid={from_lid}. Evidence KEPT."
        )

        self.slot_ops.capture_and_save_baseline(
            lid=from_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(from_lid)
        self.alarm.resolve(f"unknown-{from_lid}", from_lid)

        unknown_pid = f"unknown-{from_lid}"
        if unknown_pid in session.initial_mismatches:
            session.resolved_pids.add(unknown_pid)

        session.in_transit_pid           = None
        session.in_transit_from_lid      = None
        session.in_transit_qr_confirmed  = False

        # Reset overlay to staging only
        self._refresh_overlay(session)

        emit("admin_no_qr_result", {
            "from_lid": from_lid,
            "message": (
                f"Foreign object removed from slot {from_lid}. "
                "Slot cleared. Evidence permanently recorded."
            ),
        })

    # ── STEP 2c — STAGE PHONE ────────────────────────────

    def handle_stage_phone(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if not session.in_transit_qr_confirmed or session.in_transit_pid is None:
            emit("admin_operation_error", {
                "message": "qr_not_confirmed",
                "detail":  "Scan QR before staging.",
            })
            return

        pid      = session.in_transit_pid
        from_lid = session.in_transit_from_lid

        session.staged_phones[pid]       = from_lid
        session.in_transit_pid           = None
        session.in_transit_from_lid      = None
        session.in_transit_qr_confirmed  = False

        self._stop_clip(keep=False, reason="staged_awaiting_resolution")

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} staged from_lid={from_lid} "
            f"staged_count={len(session.staged_phones)}"
        )

        # Refresh overlay: staging zone now shows this PID; no source/dest
        self._refresh_overlay(session)

        emit("admin_stage_ok", {
            "pid":         pid,
            "staged_count": len(session.staged_phones),
            "message": f"Phone {pid} is in staging. Hand is free.",
        })

    # ── STEP 2d — UNSTAGE PHONE ──────────────────────────

    def handle_unstage_phone(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "step_lock_violated",
                "detail":  "Place the current phone before retrieving one from staging.",
            })
            return

        pid = str(data.get("pid", ""))
        if pid not in session.staged_phones:
            emit("admin_operation_error", {
                "message": "phone_not_in_staging",
                "pid":     pid,
                "staged":  list(session.staged_phones.keys()),
            })
            return

        from_lid = session.staged_phones.pop(pid)
        session.in_transit_pid          = pid
        session.in_transit_from_lid     = from_lid
        session.in_transit_qr_confirmed = True

        self._start_clip(pid=pid, lid=from_lid)

        expected_lid = session.initial_mismatches.get(pid)
        same_slot    = (expected_lid == from_lid) if expected_lid is not None else False
        client_id    = request.sid

        # Overlay: staging zone now empty + source (from_lid) + dest (expected_lid)
        self._refresh_overlay(
            session,
            source_lid = from_lid,
            dest_lid   = expected_lid,
        )

        emit("admin_unstage_ok", {
            "pid":      pid,
            "from_lid": from_lid,
            "message":  f"Phone {pid} retrieved from staging. Tracking started.",
        })

        # Auto-start tracker — admin just carries phone to the destination slot
        if expected_lid is not None:
            threading.Thread(
                target=self._launch_tracker,
                args=(session, pid, from_lid, expected_lid, same_slot, client_id),
                daemon=True,
                name=f"AdminTrack-unstage-{pid[:8]}",
            ).start()

    # ── STEP 3 — PLACE PHONE (with motion tracking) ────────────────

    def handle_place_phone(self, data: dict):
        """
        Admin taps “Start placement tracking”.
        Captures a background frame, launches PhoneTracker, and emits
        tracking_started.  The tracker callback (_admin_finalize_placement
        or _admin_placement_failed) runs in the tracker daemon thread.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if not session.in_transit_qr_confirmed or session.in_transit_pid is None:
            emit("admin_operation_error", {"message": "qr_not_confirmed"})
            return

        to_lid = data.get("to_lid")
        if to_lid is None:
            emit("admin_operation_error", {"message": "missing_to_lid"})
            return
        to_lid = int(to_lid)

        pid       = session.in_transit_pid
        from_lid  = session.in_transit_from_lid
        same_slot = getattr(session, "current_same_slot", False)
        expected_lid = session.initial_mismatches.get(pid)

        self._check_timeout(session)

        deviated = expected_lid is not None and to_lid != expected_lid
        if deviated:
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} placed in lid={to_lid} but expected lid={expected_lid}. "
                "DB updated to reflect actual placement."
            )

        # Pause destination slot so the monitor does not fire during tracking.
        self._pause_slot(to_lid)

        cancel_event = threading.Event()
        session.placement_cancel_event = cancel_event

        # Capture raw (pre-overlay) background frame NOW.
        top_camera.start()
        top_camera.wait_for_frame(timeout=0.5)
        raw_bg   = top_camera.get_raw_frame()
        bg_frame = raw_bg if raw_bg is not None else top_camera.get_frame()

        client_id = request.sid

        # Inform the client that tracking is starting.
        emit("tracking_started", {
            "pid":     pid,
            "lid":     to_lid,
            "slot":    to_lid + 1,
            "message": (
                f"Move phone {pid} to slot {to_lid + 1}. "
                "Keep the QR code visible until the phone lands."
            ),
        })

        from back_end.slot_monitor.phone_tracker import (
            PhoneTracker, _load_top_roi,
        )
        slot_roi     = _load_top_roi(to_lid)
        staging_rois = StagingConfig.get_rois()

        if slot_roi is None or bg_frame is None:
            logger.warning(
                f"[AdminOps] No slot ROI or bg frame for lid={to_lid} -- "
                "falling back to immediate placement confirmation."
            )
            self._admin_finalize_placement(
                session, to_lid, pid, from_lid, same_slot, client_id
            )
            return

        tracker = PhoneTracker(
            pid              = pid,
            lid              = to_lid,
            slot_roi         = slot_roi,
            background_frame = bg_frame,
            cancel_event     = cancel_event,
            socketio         = self.socketio,
            client_id        = client_id,
            staging_rois     = staging_rois,
        )
        tracker.start(
            on_success = lambda: self._admin_finalize_placement(
                session, to_lid, pid, from_lid, same_slot, client_id
            ),
            on_failure = lambda reason: self._admin_placement_failed(
                session, to_lid, pid, from_lid, reason, client_id
            ),
            on_staged  = lambda zone_idx: self._admin_handle_staged(
                session, pid, from_lid, to_lid, zone_idx, client_id
            ),
        )

    def _admin_finalize_placement(
        self,
        session,
        to_lid:   int,
        pid:      str,
        from_lid: Optional[int],
        same_slot: bool,
        client_id: str,
    ) -> None:
        """
        Called by PhoneTracker on success (or immediately as fallback).
        Runs in the tracker daemon thread — uses self.socketio.emit.
        """
        top_camera.clear_context_overlay()
        session.placement_cancel_event = None

        # Update DB: move the storage record to the actual destination slot.
        if not same_slot and from_lid != to_lid:
            if not SlotMonitorDB.update_storage_lid(pid, to_lid):
                logger.error(
                    f"[AdminSession] {session.session_id} — "
                    f"DB update failed for PID={pid} to lid={to_lid}"
                )
                self._stop_clip(keep=True, reason="db_update_failed")
                self.socketio.emit(
                    "admin_operation_error",
                    {"message": "db_update_failed"},
                    to=client_id, namespace="/",
                )
                self._restore_slot(to_lid, is_occupied=False)
                return

        result = self.slot_ops.capture_and_save_baseline(
            lid=to_lid, is_occupied=True, wait_for_stable=1.5
        )
        if result["status"] != "success":
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"occupied baseline capture failed for lid={to_lid}: {result['message']}."
            )
        self._resume_slot(to_lid)

        if from_lid is not None and from_lid != to_lid:
            result = self.slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=1.5
            )
            if result["status"] != "success":
                logger.warning(
                    f"[AdminSession] {session.session_id} — "
                    f"empty baseline capture failed for lid={from_lid}."
                )
            self._resume_slot(from_lid)

        if from_lid is not None:
            self.alarm.resolve(pid, from_lid)
        self.alarm.resolve(pid, to_lid)

        session.resolved_pids.add(pid)
        session.in_transit_pid           = None
        session.in_transit_from_lid      = None
        session.in_transit_qr_confirmed  = False

        self._stop_clip(keep=False, reason="placed_successfully")
        self._refresh_overlay(session)

        remaining = sorted(session.pending_pids())
        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} placed lid={to_lid}. remaining={remaining} "
            f"staged={list(session.staged_phones.keys())}"
        )
        self.socketio.emit("admin_place_result", {
            "pid":       pid,
            "from_lid":  from_lid,
            "to_lid":    to_lid,
            "same_slot": same_slot,
            "remaining": remaining,
            "staged":    list(session.staged_phones.keys()),
        }, to=client_id, namespace="/")

    def _admin_placement_failed(
        self,
        session,
        to_lid:   int,
        pid:      str,
        from_lid: Optional[int],
        reason:   str,
        client_id: str,
    ) -> None:
        """
        Called by PhoneTracker on failure.  Runs in tracker daemon thread.
        Restores the destination slot, keeps the clip, and notifies the client.
        """
        top_camera.clear_context_overlay()
        session.placement_cancel_event = None
        self._restore_slot(to_lid, is_occupied=False)
        self._stop_clip(keep=True, reason=f"placement_failed_{reason}")
        self._refresh_overlay(session)
        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"placement failed: PID={pid} lid={to_lid} reason={reason}"
        )
        self.socketio.emit(
            "admin_operation_error",
            {"message": "placement_failed", "reason": reason},
            to=client_id, namespace="/",
        )

    # ── DECLARE MISSING ───────────────────────────────────

    def handle_declare_missing(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "phone_in_hand",
                "detail": (
                    "You have a phone in hand. Place it first."
                ),
            })
            return

        pid = str(data.get("pid", ""))
        if not pid:
            emit("admin_operation_error", {"message": "missing_pid"})
            return
        if pid not in session.initial_mismatches:
            emit("admin_operation_error", {
                "message": "pid_not_in_session_mismatches",
                "pid":     pid,
                "detail":  "Only phones from the initial alarm list can be declared missing.",
            })
            return
        if pid in session.resolved_pids or pid in session.declared_missing_pids:
            emit("admin_operation_error", {"message": "pid_already_resolved", "pid": pid})
            return

        other_pending = set(session.pending_pids()) - {pid}
        unvisited     = other_pending - session.visited_pids
        if unvisited:
            emit("admin_operation_error", {
                "message":       "must_visit_all_others_first",
                "pid":           pid,
                "unvisited_lids": [session.initial_mismatches[p] for p in unvisited],
            })
            return

        expected_lid = session.initial_mismatches[pid]

        self._start_clip(pid=pid, lid=expected_lid)
        self.slot_ops.capture_and_save_baseline(
            lid=expected_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._stop_clip(keep=True, reason="declared_missing")

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"PHONE DECLARED MISSING: PID={pid} expected_lid={expected_lid}"
        )

        withdraw_result = self.slot_ops.withdraw_phone_db(pid)
        if withdraw_result["status"] != "success":
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"withdraw_phone_db failed for missing PID={pid}: "
                f"{withdraw_result['message']}."
            )

        self.slot_ops.capture_and_save_baseline(
            lid=expected_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(expected_lid)
        self.alarm.resolve(pid, expected_lid)

        session.declared_missing_pids.add(pid)

        emit("admin_missing_result", {
            "pid":          pid,
            "expected_lid": expected_lid,
            "warning": (
                "Phone declared missing — DB record withdrawn. "
                "Evidence permanently recorded."
            ),
        })

    # ── SESSION CLOSE ─────────────────────────────────────

    def handle_session_close(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        if session.staged_phones:
            emit("admin_operation_error", {
                "message": "staged_phones_must_be_placed",
                "staged":  list(session.staged_phones.keys()),
            })
            return

        warnings = []
        if session.has_phone_in_hand():
            msg = (
                f"Session closed with object still in hand "
                f"(from_lid={session.in_transit_from_lid}, "
                f"pid={session.in_transit_pid!r})"
            )
            logger.warning(f"[AdminSession] {session.session_id} — {msg}")
            warnings.append(msg)

        remaining = sorted(session.pending_pids())
        if remaining:
            msg = f"Session closed with unresolved mismatches: {remaining}"
            logger.warning(f"[AdminSession] {session.session_id} — {msg}")
            warnings.append(msg)

        if session.phone_count_at_open >= 0:
            current_count = SlotMonitorDB.count_stored_phones()
            if current_count is not None:
                expected = (
                    session.phone_count_at_open
                    - len(session.declared_missing_pids)
                )
                if current_count != expected:
                    msg = (
                        f"Phone count mismatch: expected {expected}, actual={current_count}"
                    )
                    logger.warning(f"[AdminSession] {session.session_id} — {msg}")
                    warnings.append(msg)

        for lid in session.initial_mismatches.values():
            is_occ = SlotMonitorDB.is_slot_occupied(lid)
            self._restore_slot(lid, is_occupied=bool(is_occ))

        if self._recorder:
            self._recorder.stop_current_clip_if_active(
                keep=True, reason="session_closed_with_active_clip"
            )

        if self._recorder:
            outcome      = "clean" if not warnings and not self._recorder.is_flagged else "flagged"
            evidence_kept = self._recorder.is_flagged or bool(warnings)
            self._recorder.close(outcome=outcome, warnings=warnings)
            self._recorder = None
        else:
            evidence_kept = False

        # Clear all overlays and stop rolling buffer
        top_camera.clear_context_overlay()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

        summary = admin_ctx.close().summary()
        emit("admin_session_closed", {
            "summary":       summary,
            "warnings":      warnings,
            "evidence_kept": evidence_kept,
        })

    # ── Internal helpers ──────────────────────────────────

    def _pause_slot(self, lid: int):
        m = self.slot_ops.monitor
        if m and m.worker_pool:
            m.worker_pool.pause_slot(lid)

    def _resume_slot(self, lid: int):
        m = self.slot_ops.monitor
        if m and m.worker_pool:
            m.worker_pool.resume_slot(lid)

    def _restore_slot(self, lid: int, is_occupied: bool):
        m = self.slot_ops.monitor
        if m and m.worker_pool:
            m.worker_pool.restore_slot(lid, is_occupied)

    @staticmethod
    def _check_timeout(session) -> None:
        if session.is_expired():
            logger.warning(
                f"[AdminSession] {session.session_id} — session exceeded timeout "
                f"(elapsed={session.elapsed():.0f}s)"
            )


# ══════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════

def register_admin_handlers(
    socketio: SocketIO,
    slot_operations: SlotOperations,
    alarm: AlarmController,
) -> None:
    handler = AdminOpsHandler(slot_operations, alarm, socketio)

    @socketio.on("admin_session_start")
    def on_session_start(data):
        handler.handle_session_start(data)

    @socketio.on("admin_remove_phone")
    def on_remove_phone(data):
        handler.handle_remove_phone(data)

    @socketio.on("admin_qr_scanned")
    def on_qr_scanned(data):
        handler.handle_qr_scanned(data)

    @socketio.on("admin_no_qr_found")
    def on_no_qr_found(data):
        handler.handle_no_qr_found(data)

    @socketio.on("admin_stage_phone")
    def on_stage_phone(data):
        handler.handle_stage_phone(data)

    @socketio.on("admin_unstage_phone")
    def on_unstage_phone(data):
        handler.handle_unstage_phone(data)

    @socketio.on("admin_place_phone")
    def on_place_phone(data):
        handler.handle_place_phone(data)

    @socketio.on("admin_declare_missing")
    def on_declare_missing(data):
        handler.handle_declare_missing(data)

    @socketio.on("admin_session_close")
    def on_session_close(data):
        handler.handle_session_close(data)

    logger.info("Admin resolution WebSocket handlers registered")