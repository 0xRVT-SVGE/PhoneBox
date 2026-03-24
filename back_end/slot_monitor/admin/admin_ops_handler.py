# ============================================================
# FILE: back_end/slot_monitor/admin/admin_ops_handler.py
# ============================================================
"""
Admin Resolution Session — WebSocket event handlers.

Evidence model:
    An EvidenceRecorder is created when the session opens and a photo is
    captured at every significant admin action. On clean session close all
    evidence is deleted automatically. On flagged close it is kept permanently.

    A session is flagged (evidence kept) if:
        - admin_no_qr_found was called  (unidentified object left the box)
        - admin_declare_missing was called  (phone never physically found)
        - session closed with any warnings

Socket protocol
───────────────────────────────────────────────────────────────────────────
CLIENT → SERVER                      SERVER → CLIENT
───────────────────────────────────  ──────────────────────────────────────
admin_session_start  {password}   →  admin_session_opened  {session_id,
                                                             mismatches,
                                                             phone_count,
                                                             staging_rois}
                                     admin_session_error   {message}

admin_remove_phone   {from_lid}   →  admin_remove_ok       {from_lid}
                                     admin_operation_error {message}

admin_qr_scanned     {}           →  admin_qr_result       {pid,
                                                             expected_lid,
                                                             target_occupied,
                                                             needs_deposit}
                                     admin_operation_error {message}

admin_no_qr_found    {}           →  admin_no_qr_result    {from_lid}
                                     admin_operation_error {message}

admin_stage_phone    {}           →  admin_stage_ok        {pid, staged_count}
                                     admin_operation_error {message}

admin_unstage_phone  {pid}        →  admin_unstage_ok      {pid, from_lid}
                                     admin_operation_error {message}

admin_place_phone    {to_lid}     →  admin_place_result    {pid, from_lid,
                                                             to_lid,
                                                             remaining,
                                                             staged}
                                     admin_operation_error {message}

admin_declare_missing {pid}       →  admin_missing_result  {pid,
                                                             expected_lid,
                                                             warning}
                                     admin_operation_error {message}

admin_session_close  {}           →  admin_session_closed  {summary,
                                                             warnings,
                                                             evidence_kept}
                                     admin_operation_error {message}

───────────────────────────────────────────────────────────────────────────
Swap resolution (slot A has phone B, slot B has phone A):

  1. admin_remove_phone  {from_lid: A}
  2. admin_qr_scanned    {}  → pid=B, expected=B_slot, target_occupied=True
  3. admin_stage_phone   {}  → B staged, hand free
  4. admin_remove_phone  {from_lid: B}
  5. admin_qr_scanned    {}  → pid=A, expected=A_slot, target_occupied=False
     (False because B is staged — its stale DB record is excluded)
  6. admin_place_phone   {to_lid: A_slot} → A resolved ✓
  7. admin_unstage_phone {pid: B}         → B back in hand (no re-scan)
  8. admin_place_phone   {to_lid: B_slot} → B resolved ✓
  9. admin_session_close {}               → evidence deleted (clean)
"""

import logging
from flask_socketio import emit, SocketIO
from flask import request
import json
import os

from back_end.slot_monitor.admin.resolution_session import admin_ctx, SESSION_TIMEOUT
from back_end.slot_monitor.admin.evidence_recorder import EvidenceRecorder
from back_end.slot_monitor.camera.qr_pid_reader import scan_and_validate_pid_from_buffer
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB
from back_end.slot_monitor.alarm_controller import AlarmController
from back_end.slot_monitor.camera.top_camera import top_camera

logger = logging.getLogger(__name__)

TOP_CAMERA_INDEX = 2
QR_SCAN_TIMEOUT = 15.0


# ──────────────────────────────────────────────────────────────────────────────
# Staging zone configuration
# ──────────────────────────────────────────────────────────────────────────────

_TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),  # .../admin/
    "..", "tools",  # .../tools/
)
_ROI_FILE_TOP = os.path.normpath(
    os.path.join(_TOOLS_DIR, "rois_top.json")
)

# Fallback staging coordinates (used when rois_top.json is absent)
_FALLBACK_ROIS = [(50, 50, 150, 150), (250, 50, 150, 150)]


class StagingConfig:
    """
    Pixel coordinates of the physical storage slots on the box lid
    as seen by the top camera.

    Loaded from rois_top.json (written by the ROI calibration tool at
    startup).  Each entry is (x, y, w, h).

    The top camera draws these rectangles on every streamed frame so the
    admin can see slot boundaries during a resolution session.
    """

    _rois: list | None = None  # cached after first load

    @classmethod
    def get_rois(cls) -> list:
        if cls._rois is not None:
            return cls._rois

        if os.path.exists(_ROI_FILE_TOP):
            try:
                with open(_ROI_FILE_TOP, "r") as f:
                    data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    cls._rois = [tuple(int(v) for v in r) for r in data]
                    logger.info(
                        f"[StagingConfig] Loaded {len(cls._rois)} ROIs "
                        f"from {_ROI_FILE_TOP}"
                    )
                    return cls._rois
            except Exception as e:
                logger.warning(
                    f"[StagingConfig] Failed to load {_ROI_FILE_TOP}: {e}. "
                    f"Using fallback ROIs."
                )

        logger.warning(
            f"[StagingConfig] {_ROI_FILE_TOP} not found. "
            f"Using fallback staging ROIs."
        )
        cls._rois = list(_FALLBACK_ROIS)
        return cls._rois

    @classmethod
    def invalidate(cls) -> None:
        """Call this if rois_top.json is regenerated while the server is running."""
        cls._rois = None


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

class AdminOpsHandler:

    def __init__(self, slot_ops: SlotOperations, alarm: AlarmController):
        self.slot_ops = slot_ops
        self.alarm = alarm
        self._recorder: EvidenceRecorder | None = None

    # ── Evidence shortcuts ────────────────────────────────────────────────────

    def _start_clip(self, pid: str, lid: int):
        """Start recording a clip for the phone now in hand."""
        if self._recorder:
            self._recorder.start_clip(pid, lid)

    def _stop_clip(self, keep: bool, reason: str = ""):
        """Stop the current clip and keep or delete it."""
        if self._recorder:
            self._recorder.stop_clip(keep=keep, reason=reason)

    # ──────────────────────────────────────────────────────
    # SESSION OPEN
    # ──────────────────────────────────────────────────────

    def handle_session_start(self, data: dict):
        """
        Authenticate admin, snapshot alarm mismatches, open session, start
        evidence recorder, and pause all affected slots.
        """
        # Outer try-except ensures Flutter ALWAYS gets a response.
        # Flask-SocketIO swallows unhandled exceptions silently — without this
        # the client hangs on 'opening session' forever.
        try:
            self._handle_session_start(data)
        except Exception as exc:
            logger.error(
                f"[AdminSession] handle_session_start unhandled error: {exc}",
                exc_info=True,
            )
            # Roll back session if it was partially opened
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

        # count_stored_phones() was added recently — guard against AttributeError
        # if the method doesn't exist yet in db_interface.py, or any DB error.
        try:
            phone_count = SlotMonitorDB.count_stored_phones()
            if phone_count is None:
                phone_count = -1
        except Exception as e:
            logger.warning(
                f"[AdminSession] count_stored_phones() failed ({e}) — "
                "count invariant check will be skipped at session close."
            )
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

        # Lazy-start top camera — idempotent if WebRTC or DVW already started it
        top_camera.start()
        top_camera.set_rois(StagingConfig.get_rois())

        # Switch top rolling buffer to full fps for the session
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(True)
        except Exception:
            pass

        # Start evidence recorder — guard against missing DB table (migration 004)
        self._recorder = EvidenceRecorder(session.session_id)
        try:
            self._recorder.start()
        except Exception as e:
            logger.warning(
                f"[AdminSession] EvidenceRecorder.start() failed: {e}. "
                "Evidence will not be recorded for this session. "
                "Check that migration 004 has been applied."
            )
            self._recorder = None

        for lid in initial_mismatches.values():
            self._pause_slot(lid)

        emit("admin_session_opened", {
            "session_id": session.session_id,
            "mismatches": [
                {"pid": pid, "expected_lid": lid}
                for pid, lid in initial_mismatches.items()
            ],
            "phone_count": phone_count,
            "staging_rois": StagingConfig.get_rois(),
        })

    # ──────────────────────────────────────────────────────
    # STEP 1 — REMOVE PHONE FROM SLOT
    # ──────────────────────────────────────────────────────

    def handle_remove_phone(self, data: dict):
        """
        Admin physically picks up the object from from_lid.
        Step-lock: rejected if another phone is already in hand.
        Must be followed by admin_qr_scanned or admin_no_qr_found.
        """
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
                    f"Place or stage it before removing another."
                ),
            })
            return

        from_lid = data.get("from_lid")
        if from_lid is None:
            emit("admin_operation_error", {"message": "missing_from_lid"})
            return

        from_lid = int(from_lid)
        session.in_transit_from_lid = from_lid
        session.in_transit_pid = None
        session.in_transit_qr_confirmed = False

        # Mark the phone at this lid as visited the moment the slot is opened.
        # This is what unlocks declare_missing for any given phone — all others
        # must have been visited at least once first.
        pid_at_lid = SlotMonitorDB.get_pid_for_lid(from_lid)
        if pid_at_lid:
            session.visited_pids.add(pid_at_lid)
        else:
            # Slot may hold an unregistered phone — mark by lid sentinel
            session.visited_pids.add(f"unknown-{from_lid}")

        # Start recording as soon as the phone leaves its slot
        # PID unknown yet — use a placeholder; updated when QR confirms it
        self._start_clip(pid=f"pending-lid{from_lid}", lid=from_lid)

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"object removed from lid={from_lid}, awaiting QR scan"
        )
        emit("admin_remove_ok", {
            "from_lid": from_lid,
            "message": (
                f"Object removed from slot {from_lid}. "
                f"Scan its QR code, or call admin_no_qr_found."
            ),
        })

    # ──────────────────────────────────────────────────────
    # STEP 2a — QR SCAN
    # ──────────────────────────────────────────────────────

    def handle_qr_scanned(self, data: dict):
        """
        Live QR scan on the top camera to identify the object in hand.

        Outcomes:
          needs_deposit=True                → no storage record; slot cleared;
                                              admin takes phone to normal deposit
          target_occupied=True              → swap; stage first
          target_occupied=False             → place directly
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

        # Read from the shared top_camera buffer — no need to pause
        # the capture thread or stall the WebRTC stream
        scan = scan_and_validate_pid_from_buffer(
            top_camera,
            timeout_sec=QR_SCAN_TIMEOUT,
        )
        if scan["status"] != "success":
            # QR scan failed — clip continues recording (phone still in hand)
            emit("admin_operation_error", scan)
            return

        pid = scan["pid"]
        from_lid = session.in_transit_from_lid

        # ── Case: phone exists but no active storage record ───────────────────
        if not SlotMonitorDB.is_phone_stored(pid):
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} found in lid={from_lid} but has no storage record. "
                f"Needs normal deposit."
            )
            # Restart clip under the real PID now that we know it,
            # then keep it — this is an anomalous state
            self._stop_clip(keep=False, reason="restarting_with_real_pid")
            self._start_clip(pid=pid, lid=from_lid)
            self._stop_clip(keep=True, reason="needs_deposit_no_storage_record")

            self.slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=1.5
            )
            self._resume_slot(from_lid)
            self.alarm.resolve(f"unknown-{from_lid}", from_lid)

            session.needs_deposit_pids.add(pid)
            session.in_transit_pid = None
            session.in_transit_from_lid = None
            session.in_transit_qr_confirmed = False

            emit("admin_qr_result", {
                "pid": pid,
                "needs_deposit": True,
                "message": (
                    f"Phone {pid} is not in the storage system. "
                    f"Slot {from_lid} has been cleared. "
                    f"Initiate a normal deposit operation for this phone."
                ),
            })
            return

        # ── Normal case ───────────────────────────────────────────────────────
        # Now that we know the real PID, restart the clip under it
        self._stop_clip(keep=False, reason="restarting_with_real_pid")
        self._start_clip(pid=pid, lid=from_lid)

        expected_lid = SlotMonitorDB.get_lid_for_pid(pid)

        if pid not in session.initial_mismatches:
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} was NOT in the initial mismatch list. "
                f"Additional problem discovered beyond the original alarm."
            )

        # target_occupied excludes phones currently staged (their DB records are stale)
        target_occupied = SlotMonitorDB.is_slot_occupied(expected_lid)
        if target_occupied:
            blocking_pid = SlotMonitorDB.get_pid_for_lid(expected_lid)
            if blocking_pid in session.staged_phones:
                target_occupied = False
                logger.info(
                    f"[AdminSession] {session.session_id} — "
                    f"lid={expected_lid} DB-occupied by staged PID={blocking_pid}; "
                    f"treating as physically free."
                )

        session.in_transit_pid = pid
        session.in_transit_qr_confirmed = True

        emit("admin_qr_result", {
            "pid": pid,
            "expected_lid": expected_lid,
            "target_occupied": target_occupied,
            "needs_deposit": False,
            "message": (
                f"Phone {pid}: target slot {expected_lid} is occupied — "
                f"stage this phone first, then clear the target."
                if target_occupied else
                f"Phone {pid}: place it in slot {expected_lid}."
            ),
        })

    # ──────────────────────────────────────────────────────
    # STEP 2b — NO QR FOUND (foreign object)
    # ──────────────────────────────────────────────────────

    def handle_no_qr_found(self, data: dict):
        """
        Admin attempted QR scan but the object has no QR code.

        Evidence is captured and PERMANENTLY KEPT — unidentified object
        left the box with no identity check.
        Slot is cleared and re-baselined.

        TODO: Require admin to add a written description.
        TODO: Supervisor notification.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        if session.in_transit_from_lid is None:
            emit("admin_operation_error", {
                "message": "no_object_removed",
                "detail": "Call admin_remove_phone before reporting no QR.",
            })
            return

        if session.in_transit_qr_confirmed:
            emit("admin_operation_error", {
                "message": "qr_already_confirmed",
                "detail": "QR confirmed — use admin_place_phone instead.",
            })
            return

        from_lid = session.in_transit_from_lid

        # Keep the clip — unidentified object left the box
        self._stop_clip(keep=True, reason="no_qr_found_unidentified_object")

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"Foreign object (no QR) removed from lid={from_lid}. "
            f"Evidence KEPT permanently."
        )

        self.slot_ops.capture_and_save_baseline(
            lid=from_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(from_lid)
        self.alarm.resolve(f"unknown-{from_lid}", from_lid)

        unknown_pid = f"unknown-{from_lid}"
        if unknown_pid in session.initial_mismatches:
            session.resolved_pids.add(unknown_pid)

        session.in_transit_pid = None
        session.in_transit_from_lid = None
        session.in_transit_qr_confirmed = False

        emit("admin_no_qr_result", {
            "from_lid": from_lid,
            "message": (
                f"Foreign object removed from slot {from_lid}. "
                f"Slot cleared. Evidence permanently recorded."
            ),
        })

    # ──────────────────────────────────────────────────────
    # STEP 2c (swap only) — STAGE PHONE
    # ──────────────────────────────────────────────────────

    def handle_stage_phone(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        if not session.in_transit_qr_confirmed or session.in_transit_pid is None:
            emit("admin_operation_error", {
                "message": "qr_not_confirmed",
                "detail": "Scan QR before staging.",
            })
            return

        pid = session.in_transit_pid
        from_lid = session.in_transit_from_lid

        session.staged_phones[pid] = from_lid
        session.in_transit_pid = None
        session.in_transit_from_lid = None
        session.in_transit_qr_confirmed = False

        # Phone is in staging — stop clip for now (no outcome yet).
        # A new clip starts when the phone is retrieved from staging.
        self._stop_clip(keep=False, reason="staged_awaiting_resolution")

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} staged from_lid={from_lid} "
            f"staged_count={len(session.staged_phones)}"
        )
        emit("admin_stage_ok", {
            "pid": pid,
            "staged_count": len(session.staged_phones),
            "message": f"Phone {pid} is in staging. Hand is free.",
        })

    # ──────────────────────────────────────────────────────
    # STEP 2d (swap only) — UNSTAGE PHONE
    # ──────────────────────────────────────────────────────

    def handle_unstage_phone(self, data: dict):
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "step_lock_violated",
                "detail": "Place the current phone before retrieving one from staging.",
            })
            return

        pid = str(data.get("pid", ""))
        if pid not in session.staged_phones:
            emit("admin_operation_error", {
                "message": "phone_not_in_staging",
                "pid": pid,
                "staged": list(session.staged_phones.keys()),
            })
            return

        from_lid = session.staged_phones.pop(pid)
        session.in_transit_pid = pid
        session.in_transit_from_lid = from_lid
        session.in_transit_qr_confirmed = True

        # Phone back in hand — restart recording
        self._start_clip(pid=pid, lid=from_lid)

        emit("admin_unstage_ok", {
            "pid": pid,
            "from_lid": from_lid,
            "message": f"Phone {pid} retrieved from staging. Place it in its target slot.",
        })

    # ──────────────────────────────────────────────────────
    # STEP 3 — PLACE PHONE IN SLOT
    # ──────────────────────────────────────────────────────

    def handle_place_phone(self, data: dict):
        """
        Admin places QR-confirmed phone in to_lid.
        Warns if to_lid ≠ expected_lid and records the deviation in evidence.
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

        pid = session.in_transit_pid
        from_lid = session.in_transit_from_lid
        expected_lid = session.initial_mismatches.get(pid)

        self._check_timeout(session)

        deviated = expected_lid is not None and to_lid != expected_lid
        if deviated:
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} placed in lid={to_lid} but expected lid={expected_lid}. "
                f"DB updated to reflect actual placement."
            )

        self._pause_slot(to_lid)

        if from_lid != to_lid:
            if not SlotMonitorDB.update_storage_lid(pid, to_lid):
                logger.error(
                    f"[AdminSession] {session.session_id} — "
                    f"DB update failed for PID={pid} to lid={to_lid}"
                )
                # Keep clip — DB failure is an anomaly
                self._stop_clip(keep=True, reason="db_update_failed")
                emit("admin_operation_error", {"message": "db_update_failed"})
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
                    f"empty baseline capture failed for lid={from_lid}: {result['message']}."
                )
            self._resume_slot(from_lid)

        if from_lid is not None:
            self.alarm.resolve(pid, from_lid)
        self.alarm.resolve(pid, to_lid)

        session.resolved_pids.add(pid)
        session.in_transit_pid = None
        session.in_transit_from_lid = None
        session.in_transit_qr_confirmed = False

        # Phone placed correctly — delete the clip
        self._stop_clip(keep=False, reason="placed_successfully")

        remaining = sorted(session.pending_pids())
        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} placed lid={to_lid}. remaining={remaining} "
            f"staged={list(session.staged_phones.keys())}"
        )
        emit("admin_place_result", {
            "pid": pid,
            "from_lid": from_lid,
            "to_lid": to_lid,
            "remaining": remaining,
            "staged": list(session.staged_phones.keys()),
        })

    # ──────────────────────────────────────────────────────
    # DECLARE MISSING
    # ──────────────────────────────────────────────────────

    def handle_declare_missing(self, data: dict):
        """
        Declare a phone from the mismatch list as missing — never physically found.
        No phone in hand. Evidence is PERMANENTLY KEPT.

        TODO: Require supervisor remote approval.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "phone_in_hand",
                "detail": (
                    "You have a phone in hand. Place it first. "
                    "If the phone in hand is the one you thought was missing, "
                    "it exists — put it through normal deposit."
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
                "pid": pid,
                "detail": "Only phones from the initial alarm list can be declared missing.",
            })
            return

        if pid in session.resolved_pids or pid in session.declared_missing_pids:
            emit("admin_operation_error", {
                "message": "pid_already_resolved",
                "pid": pid,
            })
            return

        # ── Guard: all OTHER phones must have been visited (lid removed) ──────
        # "Visited" = the slot was opened via admin_remove_phone at least once.
        other_pending = set(session.pending_pids()) - {pid}
        unvisited = other_pending - session.visited_pids
        if unvisited:
            emit("admin_operation_error", {
                "message": "must_visit_all_others_first",
                "pid": pid,
                "unvisited_lids": [session.initial_mismatches[p] for p in unvisited],
                "detail": (
                    "Check all other mismatched slots before declaring this phone missing. "
                    "It may have been misplaced by a previous action."
                ),
            })
            return

        expected_lid = session.initial_mismatches[pid]

        # Short clip of the empty slot — visual evidence of absence.
        # capture_and_save_baseline waits 1.5s for a stable frame,
        # giving the clip enough footage before we stop it.
        self._start_clip(pid=pid, lid=expected_lid)
        self.slot_ops.capture_and_save_baseline(
            lid=expected_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._stop_clip(keep=True, reason="declared_missing")

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"!!! PHONE DECLARED MISSING: PID={pid} "
            f"expected_lid={expected_lid} — Evidence KEPT permanently !!!"
        )

        withdraw_result = self.slot_ops.withdraw_phone_db(pid)
        if withdraw_result["status"] != "success":
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"withdraw_phone_db failed for missing PID={pid}: "
                f"{withdraw_result['message']}. DB may be inconsistent."
            )

        self.slot_ops.capture_and_save_baseline(
            lid=expected_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(expected_lid)
        self.alarm.resolve(pid, expected_lid)

        session.declared_missing_pids.add(pid)

        emit("admin_missing_result", {
            "pid": pid,
            "expected_lid": expected_lid,
            "warning": (
                "Phone declared missing — DB record withdrawn. "
                "Evidence permanently recorded. "
                "Supervisor approval required — not yet implemented."
            ),
        })

    # ──────────────────────────────────────────────────────
    # SESSION CLOSE
    # ──────────────────────────────────────────────────────

    def handle_session_close(self, data: dict):
        """
        Close the resolution session.

        BLOCKED if staging zone is non-empty.

        Evidence deleted if session is clean (no flagged events, no warnings).
        Evidence kept permanently otherwise.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        # Hard block — staged phones must be placed first
        if session.staged_phones:
            emit("admin_operation_error", {
                "message": "staged_phones_must_be_placed",
                "staged": list(session.staged_phones.keys()),
                "detail": (
                    "All staged phones must be placed before closing. "
                    "Call admin_unstage_phone then admin_place_phone for each."
                ),
            })
            return

        warnings = []

        if session.has_phone_in_hand():
            msg = (
                f"Session closed with object still in hand "
                f"(from_lid={session.in_transit_from_lid}, "
                f"pid={session.in_transit_pid!r}, "
                f"qr_confirmed={session.in_transit_qr_confirmed})"
            )
            logger.warning(f"[AdminSession] {session.session_id} — {msg}")
            warnings.append(msg)

        remaining = sorted(session.pending_pids())
        if remaining:
            msg = f"Session closed with unresolved mismatches: {remaining}"
            logger.warning(f"[AdminSession] {session.session_id} — {msg}")
            warnings.append(msg)

        # Count invariant
        if session.phone_count_at_open >= 0:
            current_count = SlotMonitorDB.count_stored_phones()
            if current_count is None:
                logger.warning(
                    f"[AdminSession] {session.session_id} — "
                    f"count_stored_phones() failed; invariant check skipped."
                )
            else:
                expected = (
                    session.phone_count_at_open
                    - len(session.declared_missing_pids)
                )
                if current_count != expected:
                    msg = (
                        f"Phone count mismatch: expected {expected} "
                        f"(at_open={session.phone_count_at_open}, "
                        f"missing={len(session.declared_missing_pids)}), "
                        f"actual={current_count}"
                    )
                    logger.warning(f"[AdminSession] {session.session_id} — {msg}")
                    warnings.append(msg)

        # Restore still-paused slots
        for lid in session.initial_mismatches.values():
            is_occ = SlotMonitorDB.is_slot_occupied(lid)
            self._restore_slot(lid, is_occupied=bool(is_occ))

        # If a clip was still recording when the session closed (phone in hand,
        # or any other mid-step close), keep it — it's an anomaly.
        if self._recorder:
            self._recorder.stop_current_clip_if_active(
                keep=True, reason="session_closed_with_active_clip"
            )

        if self._recorder:
            outcome = "clean" if not warnings and not self._recorder.is_flagged else "flagged"
            self._recorder.close(outcome=outcome, warnings=warnings)
            evidence_kept = self._recorder.is_flagged or bool(warnings)
            self._recorder = None
        else:
            evidence_kept = False

        # Clear ROI overlay and return to idle fps — session is over
        top_camera.set_rois([])
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

        summary = admin_ctx.close().summary()
        emit("admin_session_closed", {
            "summary": summary,
            "warnings": warnings,
            "evidence_kept": evidence_kept,
        })

    # ──────────────────────────────────────────────────────
    # INTERNAL HELPERS
    # ──────────────────────────────────────────────────────

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
                f"[AdminSession] {session.session_id} — "
                f"session exceeded {SESSION_TIMEOUT:.0f}s timeout "
                f"(elapsed={session.elapsed():.0f}s). "
                f"phone_in_hand={session.in_transit_pid!r} "
                f"staged={list(session.staged_phones.keys())} "
                f"pending={sorted(session.pending_pids())}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# REGISTRATION
# ──────────────────────────────────────────────────────────────────────────────

def register_admin_handlers(
    socketio: SocketIO,
    slot_operations: SlotOperations,
    alarm: AlarmController,
) -> None:
    handler = AdminOpsHandler(slot_operations, alarm)

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