# ============================================================
# FILE: back_end/slot_monitor/admin/admin_ops_handler.py
# ============================================================
"""
Admin Resolution Session — WebSocket event handlers.

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

admin_stage_phone    {}           →  admin_stage_ok        {pid,
                                                             staged_count}
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
                                                             warnings}
                                     admin_operation_error {message}

───────────────────────────────────────────────────────────────────────────
Decision tree after admin_qr_scanned:

  needs_deposit=True:
    Phone physically found but no storage record.
    → Session clears this phone's in_transit state.
    → Admin takes the phone and initiates a normal `deposit` operation.

  target_occupied=True:
    Phone found, correct slot is blocked by another phone (swap).
    → Call admin_stage_phone, then handle the blocking phone, then unstage.

  target_occupied=False, needs_deposit=False:
    Simple mismatch or same-slot re-baseline.
    → Call admin_place_phone directly.

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
  9. admin_session_close {}
"""

import logging
from flask_socketio import emit, SocketIO
from flask import request

from back_end.slot_monitor.admin.resolution_session import admin_ctx, SESSION_TIMEOUT
from back_end.slot_monitor.camera.qr_pid_reader import scan_and_validate_pid
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB
from back_end.slot_monitor.alarm_controller import AlarmController

logger = logging.getLogger(__name__)

TOP_CAMERA_INDEX = 2
QR_SCAN_TIMEOUT = 15.0


# ──────────────────────────────────────────────────────────────────────────────
# Staging zone configuration
# ──────────────────────────────────────────────────────────────────────────────

class StagingConfig:
    """
    Pixel coordinates of the physical staging zone(s) on the box lid
    as seen by the top camera.

    Two ROIs defined so that during a swap each phone has a distinct spot —
    making future camera-based detection unambiguous.

    TODO: Load from DB / config file instead of hardcoding.
    TODO: Add admin UI to set these at installation time.
    TODO: Lock config changes when a session is active.
    TODO: Use ROIs for camera-based presence detection.
    """
    STAGING_ROI_1: tuple = (50, 50, 150, 150)
    STAGING_ROI_2: tuple = (250, 50, 150, 150)

    @classmethod
    def get_rois(cls) -> list:
        return [cls.STAGING_ROI_1, cls.STAGING_ROI_2]


# ──────────────────────────────────────────────────────────────────────────────
# Handler
# ──────────────────────────────────────────────────────────────────────────────

class AdminOpsHandler:

    def __init__(self, slot_ops: SlotOperations, alarm: AlarmController):
        self.slot_ops = slot_ops
        self.alarm = alarm

    # ──────────────────────────────────────────────────────
    # SESSION OPEN
    # ──────────────────────────────────────────────────────

    def handle_session_start(self, data: dict):
        """
        Authenticate admin, snapshot alarm mismatches, open session, and pause
        all affected slots so workers don't fire new alarms while admin works.
        """
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

        phone_count = SlotMonitorDB.count_stored_phones()
        if phone_count is None:
            logger.warning(
                "[AdminSession] count_stored_phones() failed — "
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
        Admin physically picks up the phone/object from from_lid.
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

        session.in_transit_from_lid = int(from_lid)
        session.in_transit_pid = None
        session.in_transit_qr_confirmed = False

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"object removed from lid={from_lid}, awaiting QR scan"
        )
        emit("admin_remove_ok", {
            "from_lid": int(from_lid),
            "message": f"Object removed from slot {from_lid}. Scan its QR code, or call admin_no_qr_found.",
        })

    # ──────────────────────────────────────────────────────
    # STEP 2a — QR SCAN (identifies what is in hand)
    # ──────────────────────────────────────────────────────

    def handle_qr_scanned(self, data: dict):
        """
        Live QR scan on the top camera to identify the object in hand.

        Outcomes:
          needs_deposit=False, target_occupied=False → place directly
          needs_deposit=False, target_occupied=True  → stage first (swap)
          needs_deposit=True                         → hand off to normal deposit;
                                                       session clears this in_transit

        target_occupied correctly excludes phones currently in staging —
        their DB records are stale until admin_place_phone updates them.
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

        scan = scan_and_validate_pid(
            camera_index=TOP_CAMERA_INDEX,
            timeout_sec=QR_SCAN_TIMEOUT,
        )
        if scan["status"] != "success":
            # QR scan failed — admin should call admin_no_qr_found if no QR exists
            emit("admin_operation_error", scan)
            return

        pid = scan["pid"]
        from_lid = session.in_transit_from_lid

        # ── Case: phone exists but no active storage record ───────────────────
        # This phone was placed without a deposit operation.
        # It doesn't have a "correct" slot — put it through normal deposit.
        # Session clears its in_transit state; admin carries the phone out and
        # initiates a deposit event through the normal ops_handler flow.
        if not SlotMonitorDB.is_phone_stored(pid):
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} found physically in lid={from_lid} "
                f"but has no active storage record. Needs normal deposit."
            )
            # Slot is now empty — capture baseline and resume
            self.slot_ops.capture_and_save_baseline(
                lid=from_lid, is_occupied=False, wait_for_stable=1.5
            )
            self._resume_slot(from_lid)
            self.alarm.resolve(f"unknown-{from_lid}", from_lid)

            # Mark outcome and clear in_transit
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

        # ── Normal case: phone has a storage record ───────────────────────────
        expected_lid = SlotMonitorDB.get_lid_for_pid(pid)

        if pid not in session.initial_mismatches:
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"PID={pid} was NOT in the initial mismatch list. "
                f"Additional problem discovered beyond the original alarm."
            )

        # Determine if target is occupied — exclude staged phones whose DB
        # records are stale (they're physically in staging, not in their slot).
        target_occupied = SlotMonitorDB.is_slot_occupied(expected_lid)
        if target_occupied:
            blocking_pid = SlotMonitorDB.get_pid_for_lid(expected_lid)
            if blocking_pid in session.staged_phones:
                # Blocking phone is staged — its slot is physically free
                target_occupied = False
                logger.info(
                    f"[AdminSession] {session.session_id} — "
                    f"lid={expected_lid} appears occupied by PID={blocking_pid} "
                    f"but that phone is staged. Treating target as free."
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
    # STEP 2b — NO QR FOUND (genuine foreign object)
    # ──────────────────────────────────────────────────────

    def handle_no_qr_found(self, data: dict):
        """
        Admin attempted QR scan but found nothing — the object in hand has no
        QR code and is not a registered phone (e.g. debris, personal item).

        The object is removed from the box. The slot is cleared and re-baselined.
        The alarm entry for this slot is resolved.

        Security note: this is an unverified claim by the admin. The object
        is physically leaving the box with no identity check.
        TODO: Require admin to photograph/log the object as evidence.
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
                "detail": "QR was already confirmed for this object — use admin_place_phone.",
            })
            return

        from_lid = session.in_transit_from_lid

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"Foreign object (no QR) removed from lid={from_lid}. "
            f"Object is leaving the box unidentified."
            # TODO: log physical evidence (photo, weight, description)
        )

        # Slot is now empty
        self.slot_ops.capture_and_save_baseline(
            lid=from_lid, is_occupied=False, wait_for_stable=1.5
        )
        self._resume_slot(from_lid)

        # Resolve the alarm entry for this slot (pid was "unknown-{from_lid}")
        self.alarm.resolve(f"unknown-{from_lid}", from_lid)

        # Mark the unknown-{lid} entry as resolved in the session
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
                f"Slot cleared and re-baselined. "
                f"Object has left the box — log physical evidence manually."
            ),
        })

    # ──────────────────────────────────────────────────────
    # STEP 2c (swap only) — STAGE PHONE
    # ──────────────────────────────────────────────────────

    def handle_stage_phone(self, data: dict):
        """
        Admin places the QR-confirmed in-hand phone in the physical staging zone.
        Clears in_transit so the hand is free to pick up the blocking phone.
        Used when target slot is occupied (swap scenario).
        """
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

        logger.info(
            f"[AdminSession] {session.session_id} — "
            f"PID={pid} staged from_lid={from_lid} staged_count={len(session.staged_phones)}"
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
        """
        Admin picks up a staged phone. No re-scan needed — identity was
        confirmed when staged. Step-lock enforced.
        """
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
        Admin places the QR-confirmed in-hand phone in to_lid.
        Updates DB, captures baselines for source and target, resolves alarm.

        Warns (does not block) if to_lid ≠ expected_lid — the admin may have
        a legitimate reason to place the phone somewhere else and the DB will
        reflect actual placement.
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

        if expected_lid is not None and to_lid != expected_lid:
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
        Declare a phone from the mismatch list as missing — it was never
        physically found after all other mismatches were resolved.

        No phone in hand. No QR scan. The admin has exhausted all physical
        checks and the phone is simply not in the box.

        The phone's DB record is withdrawn to keep counts consistent.
        Its slot (where it should have been) gets an empty baseline.

        TODO: Require supervisor remote approval.
        TODO: Attach session log as evidence.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        if session.has_phone_in_hand():
            emit("admin_operation_error", {
                "message": "phone_in_hand",
                "detail": (
                    "You have a phone in hand. "
                    "Place it or stage it before declaring another missing. "
                    "If the phone in hand is the missing one, place it — "
                    "it is physically present and should go through normal deposit."
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

        expected_lid = session.initial_mismatches[pid]

        logger.warning(
            f"[AdminSession] {session.session_id} — "
            f"!!! PHONE DECLARED MISSING: PID={pid} "
            f"expected_lid={expected_lid} — never physically found !!!"
        )

        # Withdraw from DB to keep count consistent
        withdraw_result = self.slot_ops.withdraw_phone_db(pid)
        if withdraw_result["status"] != "success":
            logger.warning(
                f"[AdminSession] {session.session_id} — "
                f"withdraw_phone_db failed for missing PID={pid}: "
                f"{withdraw_result['message']}. DB may be inconsistent."
            )

        # Slot is empty — capture baseline and resume
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
                "Supervisor approval required — not yet implemented."
            ),
        })

    # ──────────────────────────────────────────────────────
    # SESSION CLOSE
    # ──────────────────────────────────────────────────────

    def handle_session_close(self, data: dict):
        """
        Close the resolution session.

        BLOCKED if staging zone is non-empty — every phone that was staged
        must be placed or the swap is incomplete.

        Warnings issued (session closes anyway) for:
          - Phone still in hand (unconfirmed object)
          - Unresolved mismatches
          - Count invariant violation

        Still-paused slots are restored to current DB state.
        """
        session = admin_ctx.get()
        if session is None:
            emit("admin_operation_error", {"message": "no_active_session"})
            return

        # ── Hard block: staged phones must be placed first ────────────────────
        if session.staged_phones:
            emit("admin_operation_error", {
                "message": "staged_phones_must_be_placed",
                "staged": list(session.staged_phones.keys()),
                "detail": (
                    "All staged phones must be placed before closing the session. "
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
                # needs_deposit phones left the session and will re-enter via deposit;
                # they are not subtracted here
                expected = session.phone_count_at_open - len(session.declared_missing_pids)
                if current_count != expected:
                    msg = (
                        f"Phone count mismatch: expected {expected} "
                        f"(at_open={session.phone_count_at_open}, "
                        f"missing={len(session.declared_missing_pids)}), "
                        f"actual={current_count}"
                    )
                    logger.warning(f"[AdminSession] {session.session_id} — {msg}")
                    warnings.append(msg)

        # Restore any still-paused slots using DB as truth
        for lid in session.initial_mismatches.values():
            is_occ = SlotMonitorDB.is_slot_occupied(lid)
            self._restore_slot(lid, is_occupied=bool(is_occ))

        summary = admin_ctx.close().summary()
        emit("admin_session_closed", {
            "summary": summary,
            "warnings": warnings,
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