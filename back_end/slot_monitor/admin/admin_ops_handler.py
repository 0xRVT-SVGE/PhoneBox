# ============================================================
# FILE: back_end/slot_monitor/admin/admin_ops_handler.py
# ============================================================
"""
Admin Resolution WebSocket handlers.

Session watchdog
────────────────
_start_session_watchdog() launches a daemon thread that checks
session.is_expired() every WATCHDOG_POLL_INTERVAL seconds.

  • If expired AND no phone in hand → auto force-close the session.
  • If expired AND phone in hand   → emit a warning to the frontend and
    defer the close by one poll interval, repeating until the admin
    places/stages the phone.  This avoids interrupting an active
    placement while still enforcing the time limit.

No-QR button delay
──────────────────
When admin_remove_ok is emitted, it now includes `no_qr_button_delay_s`
(from AdminConfig.NO_QR_BUTTON_DELAY_S).  The Flutter client uses this
value for its timer instead of a hardcoded 8-second constant, so the
"No QR on this object" button only appears after a meaningful wait.
"""

import json
import logging
import os
import threading
import time
from typing import Optional

from flask import request
from flask_socketio import SocketIO, emit

from back_end.slot_monitor.admin.resolution_session import (
    admin_ctx, SESSION_TIMEOUT,
)
from back_end.slot_monitor.admin.evidence_recorder import EvidenceRecorder
from back_end.slot_monitor.camera.qr_pid_reader import (
    scan_and_validate_pid_from_buffer,
)
from back_end.slot_monitor.camera.top_camera import top_camera
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB
from back_end.slot_monitor.alarm_controller import AlarmController
from back_end.config import AdminConfig as _ADM

logger = logging.getLogger(__name__)

QR_SCAN_TIMEOUT      = _ADM.ADMIN_QR_SCAN_TIMEOUT
NO_QR_BUTTON_DELAY_S = _ADM.NO_QR_BUTTON_DELAY_S

# ── StagingConfig ─────────────────────────────────────────

_ADMIN_DIR        = os.path.dirname(os.path.abspath(__file__))
_STAGING_ROI_FILE = os.path.join(_ADMIN_DIR, "staging_rois.json")
_FALLBACK_ROIS    = [(50, 50, 150, 150), (250, 50, 150, 150)]


class StagingConfig:
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
                    logger.info(
                        f"[StagingConfig] Loaded 2 staging ROIs from "
                        f"{_STAGING_ROI_FILE}"
                    )
                    return cls._rois
            except Exception as e:
                logger.warning(
                    f"[StagingConfig] Failed to load {_STAGING_ROI_FILE}: {e}"
                )
        logger.warning(
            f"[StagingConfig] {_STAGING_ROI_FILE} not found — using fallback ROIs."
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

    def __init__(
        self,
        slot_ops: SlotOperations,
        alarm: AlarmController,
        socketio: SocketIO,
    ):
        self.slot_ops = slot_ops
        self.alarm    = alarm
        self.socketio = socketio
        self._recorder: Optional[EvidenceRecorder] = None
        self._watchdog_thread: Optional[threading.Thread] = None

    # ── Overlay management ────────────────────────────────

    def _refresh_overlay(self, session, source_lid=None, dest_lid=None):
        try:
            from back_end.slot_monitor.phone_tracker import (
                make_admin_session_overlay, load_all_top_rois,
            )
            staged_pids = list(session.staged_phones.keys()) if session else []
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

    # ── Session watchdog ──────────────────────────────────

    def _start_session_watchdog(self, session, client_id: str) -> None:
        """
        Daemon thread that enforces the session timeout.

        If expired AND no phone in hand → force-close immediately.
        If expired AND phone in hand   → emit a warning, defer by one poll
          interval, repeat until the admin places the phone.
        """
        session_id = session.session_id

        def _watch():
            while True:
                time.sleep(_ADM.WATCHDOG_POLL_INTERVAL)

                current = admin_ctx.get()
                if current is None or current.session_id != session_id:
                    return  # session closed normally

                if not current.is_expired():
                    continue

                if current.has_phone_in_hand():
                    # Admin is mid-operation — warn but don't interrupt.
                    logger.warning(
                        f"[AdminSession] {session_id} expired but phone in hand "
                        f"(lid={current.in_transit_from_lid}) — deferring close"
                    )
                    self.socketio.emit(
                        "admin_operation_error",
                        {
                            "message": "session_expired_finish_operation",
                            "detail": (
                                "Session has expired. "
                                "Place or stage the current phone to close."
                            ),
                        },
                        to=client_id,
                        namespace="/",
                    )
                    # Loop again; _auto_force_close fires on next poll if idle.
                    continue

                # No phone in hand and session expired → close now.
                logger.warning(
                    f"[AdminSession] {session_id} expired with no active "
                    "operation — auto force-closing"
                )
                self._auto_force_close(current, client_id)
                return

        t = threading.Thread(
            target=_watch, daemon=True,
            name=f"AdminWatchdog-{session_id[:8]}",
        )
        t.start()
        self._watchdog_thread = t

    def _auto_force_close(self, session, client_id: str) -> None:
        if admin_ctx.get() is None:
            return

        if getattr(session, "placement_cancel_event", None):
            session.placement_cancel_event.set()
        cancel_ev = getattr(session, "_qr_scan_cancel", None)
        if cancel_ev:
            cancel_ev.set()

        allowed_s = SESSION_TIMEOUT + session.resolve_count * float(
            _ADM.SESSION_EXTEND_PER_RESOLVE
        )
        warnings  = [f"Session auto-closed: {allowed_s:.0f}s timeout exceeded"]
        remaining = sorted(session.pending_pids())
        if remaining:
            warnings.append(f"Unresolved mismatches at timeout: {remaining}")
        if session.staged_phones:
            warnings.append(
                f"Staged phones at timeout: {list(session.staged_phones.keys())}"
            )
        if session.has_phone_in_hand():
            warnings.append(
                f"Phone in hand at timeout: lid={session.in_transit_from_lid}"
            )

        for lid in session.initial_mismatches.values():
            try:
                is_occ = SlotMonitorDB.is_slot_occupied(lid)
                self._restore_slot(lid, is_occupied=bool(is_occ))
            except Exception as e:
                logger.warning(
                    f"[AdminSession] slot restore error lid={lid}: {e}"
                )

        if self._recorder:
            try:
                self._recorder.stop_current_clip_if_active(
                    keep=True, reason="session_timeout"
                )
                self._recorder.close(outcome="force_closed", warnings=warnings)
            except Exception as e:
                logger.warning(
                    f"[AdminSession] evidence close on timeout error: {e}"
                )
            self._recorder = None

        top_camera.clear_context_overlay()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

        try:
            summary = admin_ctx.close().summary()
        except Exception:
            summary = {}

        self.alarm.unsilence()

        self.socketio.emit(
            "admin_session_closed",
            {
                "summary":       summary,
                "warnings":      warnings,
                "evidence_kept": True,
                "force_closed":  True,
                "reason":        "timeout",
            },
            to=client_id,
            namespace="/",
        )
        logger.warning(
            f"[AdminSession] Auto force-close complete: {session.session_id}"
        )

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
                client_id          = request.sid,
                initial_mismatches = initial_mismatches,
                phone_count        = phone_count,
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

        self._recorder = EvidenceRecorder(session.session_id)
        try:
            self._recorder.start()
        except Exception as e:
            logger.warning(
                f"[AdminSession] EvidenceRecorder.start() failed: {e}."
            )
            self._recorder = None

        for lid in initial_mismatches.values():
            self._pause_slot(lid)

        self._refresh_overlay(session)
        self._start_session_watchdog(session, request.sid)

        emit("admin_session_opened", {
            "session_id":         session.session_id,
            "mismatches": [
                {"pid": pid, "expected_lid": lid}
                for pid, lid in initial_mismatches.items()
            ],
            "phone_count":        phone_count,
            "staging_rois":       StagingConfig.get_rois(),
            # Sent so the client uses the server-side value instead of a
            # hardcoded constant.
            "no_qr_button_delay_s": NO_QR_BUTTON_DELAY_S,
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
        session.visited_pids.add(pid_at_lid if pid_at_lid else f"unknown-{from_lid}")

        self._start_clip(pid=f"pending-lid{from_lid}", lid=from_lid)
        self._refresh_overlay(session, source_lid=from_lid, dest_lid=None)

        client_id = request.sid

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"object removed from lid={from_lid}, auto-starting QR scan"
        )
        emit("admin_remove_ok", {
            "from_lid":             from_lid,
            # Client uses this instead of a hardcoded timer constant.
            "no_qr_button_delay_s": NO_QR_BUTTON_DELAY_S,
            "message": (
                f"Slot {from_lid} selected. "
                "Scanning for QR code now."
            ),
        })

        threading.Thread(
            target=self._scan_qr_background,
            args=(session, client_id),
            daemon=True,
            name="AdminQRScan",
        ).start()

    # ── STEP 2a — QR SCAN ────────────────────────────────

    def handle_qr_scanned(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if session.in_transit_from_lid is None:
            emit("admin_operation_error", {
                "message": "no_object_removed",
                "detail":  "Call admin_remove_phone before scanning QR.",
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
        cancel_event = threading.Event()
        session._qr_scan_cancel = cancel_event

        scan = scan_and_validate_pid_from_buffer(
            top_camera,
            timeout_sec=QR_SCAN_TIMEOUT,
            cancel_event=cancel_event,
        )

        if cancel_event.is_set() or (
            scan["status"] != "success"
            and scan.get("message") == "cancelled"
        ):
            return

        if scan["status"] != "success":
            self.socketio.emit(
                "admin_operation_error", scan, to=client_id, namespace="/"
            )
            return

        pid      = scan["pid"]
        from_lid = session.in_transit_from_lid

        if not SlotMonitorDB.is_phone_stored(pid):
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} found in lid={from_lid} but has no storage record."
            )
            session.needs_deposit_pids.add(pid)
            session.in_transit_pid          = None
            session.in_transit_from_lid     = None
            session.in_transit_qr_confirmed = False
            self._refresh_overlay(session)

            self.socketio.emit("admin_qr_result", {
                "pid":           pid,
                "needs_deposit": True,
                "message": (
                    f"Phone {pid} is not in the storage system. "
                    "Initiate a normal deposit for this phone."
                ),
            }, to=client_id, namespace="/")

            threading.Thread(
                target=self._needs_deposit_bg,
                args=(pid, from_lid),
                daemon=True,
                name=f"NeedsDepBg-{pid[:8]}",
            ).start()
            return

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

        self.socketio.emit("admin_qr_result", {
            "pid":           pid,
            "expected_lid":  expected_lid,
            "needs_deposit": False,
            "same_slot":     same_slot,
            "auto_tracking": True,
            "message":       f"Phone {pid}: moving to slot {expected_lid + 1}.",
        }, to=client_id, namespace="/")

        self._launch_tracker(
            session, pid, from_lid, expected_lid, same_slot, client_id
        )

    # ── STEP 2b — NO QR FOUND ────────────────────────────

    def _launch_tracker(self, session, pid, from_lid, to_lid, same_slot, client_id):
        self._pause_slot(to_lid)

        cancel_event = threading.Event()
        session.placement_cancel_event = cancel_event

        top_camera.start()
        top_camera.wait_for_frame(timeout=0.5)
        raw      = top_camera.get_raw_frame()
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
            self._admin_finalize_placement(
                session, to_lid, pid, from_lid, same_slot, client_id
            )
            return

        verify_fn = None
        try:
            verify_fn = self.slot_ops.make_placement_verifier(to_lid)
        except Exception as exc:
            logger.warning(f"[AdminOps] verify_fn lid={to_lid}: {exc}")

        tracker = PhoneTracker(
            pid              = pid,
            lid              = to_lid,
            slot_roi         = slot_roi,
            background_frame = bg_frame,
            cancel_event     = cancel_event,
            socketio         = self.socketio,
            client_id        = client_id,
            staging_rois     = staging_rois,
            verify_fn        = verify_fn,
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

    def _admin_handle_staged(self, session, pid, from_lid, dest_lid, zone_idx, client_id):
        top_camera.clear_context_overlay()
        session.placement_cancel_event = None
        session.staged_phones[pid]      = from_lid
        session.in_transit_pid          = None
        session.in_transit_from_lid     = None
        session.in_transit_qr_confirmed = False
        self._stop_clip(keep=False, reason="auto_staged")
        self._resume_slot(dest_lid)
        remaining    = sorted(session.pending_pids())
        blocking_pid = SlotMonitorDB.get_pid_for_lid(dest_lid)
        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} auto-staged in zone {zone_idx}. dest_lid={dest_lid} "
            f"remaining={remaining}"
        )
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

        cancel_ev = getattr(session, "_qr_scan_cancel", None)
        if cancel_ev is not None:
            cancel_ev.set()

        from_lid    = session.in_transit_from_lid
        unknown_pid = f"unknown-{from_lid}"

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"Foreign object (no QR) removed from lid={from_lid}. Evidence KEPT."
        )

        if unknown_pid in session.initial_mismatches:
            session.resolved_pids.add(unknown_pid)

        session.in_transit_pid          = None
        session.in_transit_from_lid     = None
        session.in_transit_qr_confirmed = False

        self._refresh_overlay(session)

        emit("admin_no_qr_result", {
            "from_lid": from_lid,
            "message": (
                f"Foreign object removed from slot {from_lid}. "
                "Slot cleared. Evidence permanently recorded."
            ),
        })

        threading.Thread(
            target=self._no_qr_finalize_bg,
            args=(from_lid,),
            daemon=True,
            name=f"NoQRFinalize-lid{from_lid}",
        ).start()

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

        session.staged_phones[pid]      = from_lid
        session.in_transit_pid          = None
        session.in_transit_from_lid     = None
        session.in_transit_qr_confirmed = False

        self._stop_clip(keep=False, reason="staged_awaiting_resolution")
        self._refresh_overlay(session)

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} staged from_lid={from_lid} "
            f"staged_count={len(session.staged_phones)}"
        )

        emit("admin_stage_ok", {
            "pid":          pid,
            "staged_count": len(session.staged_phones),
            "message":      f"Phone {pid} is in staging. Hand is free.",
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

        self._refresh_overlay(session, source_lid=from_lid, dest_lid=expected_lid)

        emit("admin_unstage_ok", {
            "pid":      pid,
            "from_lid": from_lid,
            "message":  f"Phone {pid} retrieved from staging. Tracking started.",
        })

        if expected_lid is not None:
            threading.Thread(
                target=self._launch_tracker,
                args=(session, pid, from_lid, expected_lid, same_slot, client_id),
                daemon=True,
                name=f"AdminTrack-unstage-{pid[:8]}",
            ).start()

    # ── STEP 3 — PLACE PHONE ─────────────────────────────

    def handle_place_phone(self, data: dict):
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

        if expected_lid is not None and to_lid != expected_lid:
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} placed in lid={to_lid} but expected lid={expected_lid}."
            )

        self._pause_slot(to_lid)

        cancel_event = threading.Event()
        session.placement_cancel_event = cancel_event

        top_camera.start()
        top_camera.wait_for_frame(timeout=0.5)
        raw_bg   = top_camera.get_raw_frame()
        bg_frame = raw_bg if raw_bg is not None else top_camera.get_frame()

        client_id = request.sid

        emit("tracking_started", {
            "pid":     pid,
            "lid":     to_lid,
            "slot":    to_lid + 1,
            "message": (
                f"Move phone {pid} to slot {to_lid + 1}. "
                "Keep the QR code visible until the phone lands."
            ),
        })

        from back_end.slot_monitor.phone_tracker import PhoneTracker, _load_top_roi
        slot_roi     = _load_top_roi(to_lid)
        staging_rois = StagingConfig.get_rois()

        if slot_roi is None or bg_frame is None:
            logger.warning(
                f"[AdminOps] No slot ROI or bg frame for lid={to_lid} — "
                "falling back to immediate placement confirmation."
            )
            self._admin_finalize_placement(
                session, to_lid, pid, from_lid, same_slot, client_id
            )
            return

        verify_fn = None
        try:
            verify_fn = self.slot_ops.make_placement_verifier(to_lid)
        except Exception as exc:
            logger.warning(f"[AdminOps] verify_fn lid={to_lid}: {exc}")

        tracker = PhoneTracker(
            pid              = pid,
            lid              = to_lid,
            slot_roi         = slot_roi,
            background_frame = bg_frame,
            cancel_event     = cancel_event,
            socketio         = self.socketio,
            client_id        = client_id,
            staging_rois     = staging_rois,
            verify_fn        = verify_fn,
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

    def _admin_finalize_placement(self, session, to_lid, pid, from_lid, same_slot, client_id):
        top_camera.clear_context_overlay()
        session.placement_cancel_event = None

        if not same_slot and from_lid != to_lid:
            if not SlotMonitorDB.update_storage_lid(pid, to_lid):
                logger.error(
                    f"[AdminSession] {session.session_id} — "
                    f"DB update failed for PID={pid} to lid={to_lid}"
                )
                self.socketio.emit(
                    "admin_operation_error",
                    {"message": "db_update_failed"},
                    to=client_id, namespace="/",
                )
                self._restore_slot(to_lid, is_occupied=False)
                threading.Thread(
                    target=self._stop_clip, args=(True, "db_update_failed"),
                    daemon=True,
                ).start()
                return

        if from_lid is not None:
            self.alarm.resolve(pid, from_lid)
        self.alarm.resolve(pid, to_lid)

        session.resolved_pids.add(pid)
        session.in_transit_pid          = None
        session.in_transit_from_lid     = None
        session.in_transit_qr_confirmed = False

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

        threading.Thread(
            target=self._finalize_placement_baselines_bg,
            args=(to_lid, from_lid, same_slot),
            daemon=True,
            name=f"BaselineBg-{pid[:8]}",
        ).start()

    def _admin_placement_failed(self, session, to_lid, pid, from_lid, reason, client_id):
        top_camera.clear_context_overlay()
        session.placement_cancel_event = None
        self._restore_slot(to_lid, is_occupied=False)
        session.in_transit_pid          = None
        session.in_transit_from_lid     = None
        session.in_transit_qr_confirmed = False
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
        threading.Thread(
            target=self._stop_clip,
            args=(True, f"placement_failed_{reason}"),
            daemon=True,
            name=f"EvidenceStop-{pid[:8]}",
        ).start()

    # ── DECLARE MISSING ───────────────────────────────────

    def handle_declare_missing(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return
        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "phone_in_hand",
                "detail":  "You have a phone in hand. Place it first.",
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
            })
            return
        if pid in session.resolved_pids or pid in session.declared_missing_pids:
            emit("admin_operation_error", {
                "message": "pid_already_resolved", "pid": pid,
            })
            return

        other_pending = set(session.pending_pids()) - {pid}
        unvisited     = other_pending - session.visited_pids
        if unvisited:
            emit("admin_operation_error", {
                "message":        "must_visit_all_others_first",
                "pid":            pid,
                "unvisited_lids": [session.initial_mismatches[p] for p in unvisited],
            })
            return

        expected_lid = session.initial_mismatches[pid]
        session.declared_missing_pids.add(pid)

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"PHONE DECLARED MISSING: PID={pid} expected_lid={expected_lid}"
        )

        emit("admin_missing_result", {
            "pid":          pid,
            "expected_lid": expected_lid,
            "warning": (
                "Phone declared missing — DB record withdrawn. "
                "Evidence permanently recorded."
            ),
        })

        threading.Thread(
            target=self._declare_missing_bg,
            args=(pid, expected_lid),
            daemon=True,
            name=f"MissingBg-{pid[:8]}",
        ).start()

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
                        f"Phone count mismatch: expected {expected}, "
                        f"actual={current_count}"
                    )
                    logger.warning(
                        f"[AdminSession] {session.session_id} — {msg}"
                    )
                    warnings.append(msg)

        for lid in session.initial_mismatches.values():
            is_occ = SlotMonitorDB.is_slot_occupied(lid)
            self._restore_slot(lid, is_occupied=bool(is_occ))

        if self._recorder:
            self._recorder.stop_current_clip_if_active(
                keep=True, reason="session_closed_with_active_clip"
            )

        if self._recorder:
            outcome      = (
                "clean"
                if not warnings and not self._recorder.is_flagged
                else "flagged"
            )
            evidence_kept = self._recorder.is_flagged or bool(warnings)
            self._recorder.close(outcome=outcome, warnings=warnings)
            self._recorder = None
        else:
            evidence_kept = False

        top_camera.clear_context_overlay()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

        summary = admin_ctx.close().summary()
        self.alarm.unsilence()
        emit("admin_session_closed", {
            "summary":       summary,
            "warnings":      warnings,
            "evidence_kept": evidence_kept,
        })

    # ── FORCE CLOSE SESSION ───────────────────────────────

    def handle_force_close_session(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        safe = data.get("safe", True)

        if safe:
            if session.has_phone_in_hand():
                emit("admin_operation_error", {
                    "message": "force_close_blocked",
                    "reason":  "phone_in_hand",
                    "detail":  "Put the phone down before force-closing.",
                })
                return
            if session.staged_phones:
                emit("admin_operation_error", {
                    "message": "force_close_blocked",
                    "reason":  "phones_staged",
                    "detail": (
                        f"Staged phones must be resolved first: "
                        f"{list(session.staged_phones.keys())}"
                    ),
                })
                return
        else:
            logger.warning(
                f"[AdminSession] {session.session_id} — UNSAFE force-close"
            )

        if session.placement_cancel_event is not None:
            session.placement_cancel_event.set()
        cancel_ev = getattr(session, "_qr_scan_cancel", None)
        if cancel_ev is not None:
            cancel_ev.set()

        warnings = []
        if session.has_phone_in_hand():
            warnings.append(
                f"Force-closed with phone in hand "
                f"(from_lid={session.in_transit_from_lid}, "
                f"pid={session.in_transit_pid!r})"
            )
        remaining = sorted(session.pending_pids())
        if remaining:
            warnings.append(f"Unresolved mismatches at force-close: {remaining}")
        if session.staged_phones:
            warnings.append(
                f"Staged phones at force-close: {list(session.staged_phones.keys())}"
            )

        for lid in session.initial_mismatches.values():
            is_occ = SlotMonitorDB.is_slot_occupied(lid)
            self._restore_slot(lid, is_occupied=bool(is_occ))

        if self._recorder:
            self._recorder.stop_current_clip_if_active(keep=True, reason="force_close")
            self._recorder.close(outcome="force_closed", warnings=warnings)
            self._recorder = None

        top_camera.clear_context_overlay()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

        summary = admin_ctx.close().summary()
        logger.warning(
            f"[AdminSession] {session.session_id} — FORCE CLOSED "
            f"(safe={safe}, remaining={remaining})"
        )
        self.alarm.unsilence()
        emit("admin_session_closed", {
            "summary":       summary,
            "warnings":      warnings,
            "evidence_kept": True,
            "force_closed":  True,
        })

    # ── PRE-HIGHLIGHT SLOT ────────────────────────────────

    def handle_pre_highlight_slot(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            return
        lid = data.get("lid")
        if lid is None:
            return
        self._refresh_overlay(session, source_lid=int(lid), dest_lid=None)

    # ── Background helpers ────────────────────────────────

    def _needs_deposit_bg(self, pid: str, from_lid: int) -> None:
        self._stop_clip(keep=False, reason="restarting_with_real_pid")
        self._start_clip(pid=pid, lid=from_lid)
        self._stop_clip(keep=True, reason="needs_deposit_no_storage_record")
        self.slot_ops.capture_and_save_baseline(
            lid=from_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(from_lid)
        self.alarm.resolve(f"unknown-{from_lid}", from_lid)

    def _declare_missing_bg(self, pid: str, expected_lid: int) -> None:
        self._start_clip(pid=pid, lid=expected_lid)
        self.slot_ops.capture_and_save_baseline(
            lid=expected_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._stop_clip(keep=True, reason="declared_missing")
        withdraw_result = self.slot_ops.withdraw_phone_db(pid)
        if withdraw_result["status"] != "success":
            logger.warning(
                f"[AdminOps] withdraw_phone_db failed for missing PID={pid}: "
                f"{withdraw_result['message']}."
            )
        self.slot_ops.capture_and_save_baseline(
            lid=expected_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(expected_lid)
        self.alarm.resolve(pid, expected_lid)

    def _finalize_placement_baselines_bg(self, to_lid, from_lid, same_slot) -> None:
        result = self.slot_ops.capture_and_save_baseline(
            lid=to_lid, is_occupied=True, wait_for_stable=1.5
        )
        if result["status"] != "success":
            logger.warning(
                f"[AdminOps] occupied baseline capture failed for lid={to_lid}."
            )
        self._resume_slot(to_lid)
        if from_lid is not None and from_lid != to_lid:
            result = self.slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=1.5
            )
            if result["status"] != "success":
                logger.warning(
                    f"[AdminOps] empty baseline capture failed for lid={from_lid}."
                )
            self._resume_slot(from_lid)

    def _no_qr_finalize_bg(self, from_lid: int) -> None:
        self._stop_clip(keep=True, reason="no_qr_found_unidentified_object")
        self.slot_ops.capture_and_save_baseline(
            lid=from_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(from_lid)
        self.alarm.resolve(f"unknown-{from_lid}", from_lid)

    # ── Internal slot helpers ─────────────────────────────

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

    @socketio.on("admin_force_close_session")
    def on_force_close_session(data):
        handler.handle_force_close_session(data)

    @socketio.on("admin_pre_highlight_slot")
    def on_pre_highlight_slot(data):
        handler.handle_pre_highlight_slot(data)

    logger.info("Admin resolution WebSocket handlers registered")