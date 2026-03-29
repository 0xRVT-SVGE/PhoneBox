# ============================================================
# FILE: back_end/slot_monitor/admin/admin_ops_handler.py
# ============================================================
"""
Admin Resolution WebSocket handlers.

All possible QR scan outcomes after the admin picks up a phone
from slot `from_lid`:

┌──────────────────────────────────────────────────────────────────────┐
│ Case │ expected_lid vs from_lid │ Target state    │ Action           │
├──────────────────────────────────────────────────────────────────────┤
│  1   │ same                     │ (n/a — empty)   │ Place back.      │
│      │                          │                  │ No DB update.    │
│      │                          │                  │ Recapture only.  │
├──────────────────────────────────────────────────────────────────────┤
│  2   │ different                │ Empty            │ Walk to target,  │
│      │                          │                  │ place, update    │
│      │                          │                  │ storage_lid in   │
│      │                          │                  │ DB.              │
├──────────────────────────────────────────────────────────────────────┤
│  3   │ different                │ Occupied (wrong  │ Stage current    │
│      │                          │ phone)           │ phone, handle    │
│      │                          │                  │ blocker, unstage │
│      │                          │                  │ and place.       │
├──────────────────────────────────────────────────────────────────────┤
│  4   │ No QR on object          │ —                │ Remove it.       │
│      │                          │                  │ Evidence kept.   │
├──────────────────────────────────────────────────────────────────────┤
│  5   │ PID has no storage       │ —                │ needs_deposit    │
│      │ record (rare edge case)  │                  │ flag; handled    │
│      │                          │                  │ outside session. │
└──────────────────────────────────────────────────────────────────────┘

Case 1 fix:
    Before this fix, the backend queried the DB to check whether the
    target slot is occupied.  For case 1 the DB still shows the phone
    in its slot (it hasn't been formally withdrawn yet), so the DB
    returns "occupied", and target_occupied=True was sent to the
    frontend, which incorrectly routed to stagingNeeded.

    Fix: when expected_lid == from_lid the slot is physically empty
    (the admin is holding the phone).  We skip the occupancy query,
    always return target_occupied=False and same_slot=True.  The
    frontend shows "place it back" and the place handler skips the
    DB storage_lid update (the lid hasn't changed).
"""

import json
import logging
import os
from typing import Optional

from flask import request
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)

_ADMIN_DIR        = os.path.dirname(os.path.abspath(__file__))
_STAGING_ROI_FILE = os.path.join(_ADMIN_DIR, "staging_rois.json")

# Safe fallback if the file hasn't been created yet
_FALLBACK_STAGING_ROIS = [(50, 50, 150, 150), (250, 50, 150, 150)]


class StagingConfig:
    """
    Pixel coordinates of the two physical staging zones on the box lid
    as seen by the top-down camera.

    Written by:  back_end/slot_monitor/admin/staging_calibration.py
    Read by:     admin_ops_handler (to overlay zones on the WebRTC stream)
                 top_camera.set_rois() during an admin session

    Completely separate from rois_top.json, which stores slot destination
    ROIs used by the phone tracker.
    """

    _rois: Optional[list] = None   # cached after first load

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
                        f"[StagingConfig] Loaded 2 staging ROIs "
                        f"from {_STAGING_ROI_FILE}"
                    )
                    return cls._rois
                logger.warning(
                    f"[StagingConfig] {_STAGING_ROI_FILE} does not contain "
                    f"exactly 2 entries — using fallback."
                )
            except Exception as e:
                logger.warning(
                    f"[StagingConfig] Failed to load {_STAGING_ROI_FILE}: {e} "
                    "— using fallback."
                )

        logger.warning(
            f"[StagingConfig] {_STAGING_ROI_FILE} not found. "
            "Run staging_calibration.py to create it. Using fallback ROIs."
        )
        cls._rois = list(_FALLBACK_STAGING_ROIS)
        return cls._rois

    @classmethod
    def invalidate(cls) -> None:
        """Call after staging_calibration.py regenerates the file at runtime."""
        cls._rois = None

# ══════════════════════════════════════════════════════════
# Handler
# ══════════════════════════════════════════════════════════

class AdminResolutionHandler:

    def __init__(self, socketio: SocketIO, slot_operations, alarm_controller):
        self.socketio   = socketio
        self.slot_ops   = slot_operations
        self.alarm      = alarm_controller

        # Per-session state — keyed by socket client_id
        self._sessions: dict = {}   # client_id → ResolutionSession

    # ── Session open / close ──────────────────────────────

    def handle_session_start(self, data: dict):
        from back_end.slot_monitor.admin.resolution_session import ResolutionSession
        from back_end.slot_monitor.camera.top_camera import top_camera

        client_id = request.sid
        password  = data.get("password", "")

        if not self.alarm.authenticate_admin(password)["authenticated"]:
            emit("admin_session_error", {
                "message": "wrong_password",
            })
            return

        if client_id in self._sessions:
            # Stale session — clean up first
            self._sessions.pop(client_id).close()

        top_camera.start()
        top_camera.set_rois(StagingConfig.get_rois())

        session = ResolutionSession(
            client_id=client_id,
            slot_ops=self.slot_ops,
            alarm=self.alarm,
            socketio=self.socketio,
        )
        self._sessions[client_id] = session
        self.alarm.silence()
        session.open()

    def handle_session_close(self, data: dict):
        from back_end.slot_monitor.camera.top_camera import top_camera
        client_id = request.sid
        session   = self._sessions.pop(client_id, None)
        if session:
            session.close()
        top_camera.set_rois([])

    # ── Admin picks up phone from a slot ──────────────────

    def handle_remove_phone(self, data: dict):
        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        from_lid = int(data.get("from_lid", -1))
        if from_lid < 0:
            emit("admin_operation_error", {"message": "missing_from_lid"})
            return

        ok = session.record_removal(from_lid)
        if not ok:
            emit("admin_operation_error", {"message": "no_object_removed"})
            return

        emit("admin_remove_ok", {"from_lid": from_lid})

    # ── QR scanned ───────────────────────────────────────

    def handle_qr_scanned(self, data: dict):
        from back_end.slot_monitor.camera.top_camera import top_camera
        from back_end.slot_monitor.camera.qr_pid_reader import (
            scan_and_validate_pid_from_buffer
        )

        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        from_lid = session.current_from_lid
        if from_lid is None:
            emit("admin_operation_error", {"message": "no_object_removed"})
            return

        scan_result = scan_and_validate_pid_from_buffer(
            top_camera,
            timeout_sec=15.0,
        )

        if scan_result["status"] != "success":
            emit("admin_operation_error", scan_result)
            return

        pid = scan_result["pid"]
        self._dispatch_qr_result(session, pid, from_lid)

    def _dispatch_qr_result(self, session, pid: str, from_lid: int):
        """
        Core routing after a successful QR scan.

        Case 1 — same slot:
            expected_lid == from_lid.
            The admin is holding the phone in their hand; the slot is
            physically empty.  We NEVER query the DB for occupancy here —
            the DB still shows the phone stored, and that query would
            incorrectly return "occupied", sending the admin into the
            staging flow.
            Action: tell frontend to place it back.  No storage_lid change.

        Case 2 — different slot, target empty:
            expected_lid != from_lid and the target slot has no phone.
            Action: place in target. Backend will update storage_lid on place.

        Case 3 — different slot, target occupied:
            expected_lid != from_lid and another phone is in the target.
            Action: stage current phone, handle blocker, unstage.

        Case 5 — phone has no storage record:
            needs_deposit=True.  Rare; the phone exists in the phones
            table but has no active storage row.
        """
        from back_end.slot_monitor.db_interface import SlotMonitorDB

        expected_lid = SlotMonitorDB.get_lid_for_pid(pid)

        # Case 5 — no storage record
        if expected_lid is None:
            session.record_qr_result(pid=pid, expected_lid=None, same_slot=False)
            emit("admin_qr_result", {
                "pid":          pid,
                "expected_lid": None,
                "needs_deposit": True,
                "target_occupied": False,
                "same_slot":    False,
                "message": (
                    f"Phone {pid} has no active storage record. "
                    "It should be deposited via the normal DVW flow."
                ),
            })
            return

        # Case 1 — same slot
        if expected_lid == from_lid:
            session.record_qr_result(pid=pid, expected_lid=expected_lid, same_slot=True)
            emit("admin_qr_result", {
                "pid":            pid,
                "expected_lid":   expected_lid,
                "needs_deposit":  False,
                "target_occupied": False,   # slot is empty — admin is holding it
                "same_slot":      True,     # no DB update needed on place
            })
            return

        # Cases 2 & 3 — different slot
        target_occupied = SlotMonitorDB.is_slot_occupied(expected_lid)
        session.record_qr_result(pid=pid, expected_lid=expected_lid, same_slot=False)
        emit("admin_qr_result", {
            "pid":            pid,
            "expected_lid":   expected_lid,
            "needs_deposit":  False,
            "target_occupied": target_occupied,
            "same_slot":      False,
        })

    # ── No QR found ───────────────────────────────────────

    def handle_no_qr(self, data: dict):
        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        from_lid = session.current_from_lid
        if from_lid is None:
            emit("admin_operation_error", {"message": "no_object_removed"})
            return

        session.record_unknown_object(from_lid)
        emit("admin_no_qr_result", {
            "from_lid": from_lid,
            "message":  "Unidentified object removed. Evidence recorded permanently.",
        })

    # ── Stage / unstage ───────────────────────────────────

    def handle_stage_phone(self, data: dict):
        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        pid = session.current_pid
        if pid is None:
            emit("admin_operation_error", {"message": "no_phone_to_stage"})
            return

        session.stage(pid)
        emit("admin_stage_ok", {"pid": pid})

    def handle_unstage_phone(self, data: dict):
        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        pid = data.get("pid")
        if not pid or not session.is_staged(pid):
            emit("admin_operation_error", {"message": "pid_not_staged"})
            return

        session.unstage(pid)
        expected_lid = session.get_expected_lid(pid)
        emit("admin_unstage_ok", {"pid": pid, "expected_lid": expected_lid})

    # ── Place ─────────────────────────────────────────────

    def handle_place_phone(self, data: dict):
        """
        Admin confirms phone is placed in to_lid.

        same_slot case (Case 1):
            same_slot=True is stored in session.  We skip the
            storage_lid DB update because the phone never left its slot
            in DB terms.  We recapture the baseline and resolve the alarm
            mismatch for this lid.

        different slot case (Cases 2 & 3):
            Update storage_lid in DB, recapture baseline for both lids,
            resolve alarm mismatches.
        """
        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        to_lid = int(data.get("to_lid", -1))
        if to_lid < 0:
            emit("admin_operation_error", {"message": "missing_to_lid"})
            return

        pid       = session.current_pid
        from_lid  = session.current_from_lid
        same_slot = session.current_same_slot

        if pid is None or from_lid is None:
            emit("admin_operation_error", {"message": "no_active_phone"})
            return

        # ── DB update ────────────────────────────────────
        if not same_slot:
            # Move the storage record to the new slot
            from back_end.slot_monitor.db_interface import SlotMonitorDB
            ok = SlotMonitorDB.update_storage_lid(pid, to_lid)
            if not ok:
                emit("admin_operation_error", {"message": "db_update_failed"})
                return

        # ── Baseline recapture ────────────────────────────
        self.slot_ops.capture_and_save_baseline(
            lid=to_lid, is_occupied=True, wait_for_stable=1.5
        )
        if not same_slot and from_lid != to_lid:
            self.slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=1.5
            )

        # ── Alarm resolution ──────────────────────────────
        self.alarm.resolve(pid=pid, lid=to_lid)
        if not same_slot:
            # from_lid mismatch is also resolved (slot now empty as expected)
            self.alarm.resolve(pid=f"unknown-{from_lid}", lid=from_lid)

        session.mark_resolved(pid)

        remaining = session.remaining_pids()
        staged    = session.staged_pids()

        emit("admin_place_result", {
            "pid":       pid,
            "to_lid":    to_lid,
            "same_slot": same_slot,
            "remaining": remaining,
            "staged":    staged,
        })

    # ── Declare missing ───────────────────────────────────

    def handle_declare_missing(self, data: dict):
        from back_end.slot_monitor.db_interface import SlotMonitorDB
        client_id = request.sid
        session   = self._sessions.get(client_id)
        if not session:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        pid = data.get("pid")
        if not pid:
            emit("admin_operation_error", {"message": "missing_pid"})
            return

        lid = session.get_expected_lid(pid)
        SlotMonitorDB.withdraw_phone(pid)
        self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=False, wait_for_stable=1.0
        )
        self.alarm.resolve(pid=pid, lid=lid)
        session.mark_resolved(pid)

        emit("admin_missing_result", {
            "pid":       pid,
            "lid":       lid,
            "remaining": session.remaining_pids(),
            "staged":    session.staged_pids(),
        })

    # ── Session summary / close ───────────────────────────

    def handle_session_close(self, data: dict):
        from back_end.slot_monitor.camera.top_camera import top_camera
        client_id = request.sid
        session   = self._sessions.pop(client_id, None)
        if not session:
            emit("admin_session_error", {"message": "no_active_session"})
            return

        summary       = session.build_summary()
        evidence_kept = not self.alarm.get_status()["active"]

        if not self.alarm.get_status()["active"]:
            self.alarm.clear()

        top_camera.set_rois([])
        emit("admin_session_closed", {
            "summary":       summary,
            "evidence_kept": evidence_kept,
        })


# ══════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════

def register_admin_handlers(socketio: SocketIO, slot_operations, alarm_controller):
    handler = AdminResolutionHandler(socketio, slot_operations, alarm_controller)

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
    def on_no_qr(data):
        handler.handle_no_qr(data)

    @socketio.on("admin_stage_phone")
    def on_stage(data):
        handler.handle_stage_phone(data)

    @socketio.on("admin_unstage_phone")
    def on_unstage(data):
        handler.handle_unstage_phone(data)

    @socketio.on("admin_place_phone")
    def on_place(data):
        handler.handle_place_phone(data)

    @socketio.on("admin_declare_missing")
    def on_declare_missing(data):
        handler.handle_declare_missing(data)

    @socketio.on("admin_session_close")
    def on_session_close(data):
        handler.handle_session_close(data)

    logger.info("Admin resolution WebSocket handlers registered")