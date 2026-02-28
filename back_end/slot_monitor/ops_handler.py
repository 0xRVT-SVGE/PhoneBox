# ============================================================
# FILE: back_end/slot_monitor/ops_handler.py
# ============================================================

import logging
from flask_socketio import emit, SocketIO
from flask import request

from back_end.slot_monitor.services.operation_context import op_ctx
from back_end.slot_monitor.camera.qr_pid_reader import scan_and_validate_pid
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB

logger = logging.getLogger(__name__)

# Seconds the handler waits for the user to scan a QR code.
# Must stay well under OperationContext.ACTION_TIMEOUT (30 s).
QR_SCAN_TIMEOUT = 15.0


class DVWSocketHandler:

    def __init__(self, slot_operations: SlotOperations, socketio: SocketIO):
        self.slot_ops = slot_operations
        self.socketio = socketio

    # ============================================================
    # DEPOSIT
    # ============================================================

    def handle_deposit(self, data: dict):
        client_id = request.sid
        pid = data.get("pid")
        if not pid:
            emit("operation_error", {"status": "error", "message": "missing_pid"})
            return

        pid = str(pid)  # normalise — all PIDs are str throughout the system

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

        emit("deposit_waiting_for_qr", {
            "status": "waiting",
            "pid": pid,
            "lid": lid,
            "message": f"Scan QR for phone {pid}, then place it in slot {lid}",
        })

    # ============================================================
    # WITHDRAW
    # ============================================================

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
            "status": "waiting",
            "pid": pid,
            "lid": lid,
            "message": f"Remove phone {pid} from slot {lid}, then scan its QR code",
        })

    # ============================================================
    # VERIFY
    # ============================================================

    def handle_verify(self, data: dict):
        """
        Correct a phone that is physically in the wrong slot.

        Handles:
          - Same-slot re-baseline (false alarm recovery)
          - Simple mismatch: phone in wrong slot, target slot is empty

        Rejects (with clear error + admin redirect):
          - Phone has no active DB record (placed without deposit operation)
            → use admin resolution
          - Swap: target slot is occupied by a different phone
            → use admin resolution (step-lock required)

        Known limitation (TODO):
          On timeout/failure, op_ctx restores target_lid with is_occupied=False.
          This is correct for the simple mismatch case (target was physically empty).
          It is not reachable for the swap case — swaps are blocked before op_ctx.start().
        """
        client_id = request.sid
        pid = data.get("pid")
        original_lid = data.get("original_lid")

        if not pid or original_lid is None:
            emit("operation_error", {"status": "error", "message": "missing_parameters"})
            return

        pid = str(pid)
        original_lid = int(original_lid)

        # ── Guard 1: phone must have an active DB record ─────────────────────
        # Phones placed without a deposit operation have no storage record.
        # Verify cannot handle these — they have no "correct" slot to return to.
        # Use admin_session_start instead (it handles arbitrary slot states).
        target_lid = SlotMonitorDB.get_lid_for_pid(pid)
        if target_lid is None:
            emit("operation_error", {
                "status": "error",
                "message": "phone_not_registered_to_any_slot",
                "pid": pid,
                "detail": (
                    "This phone has no active storage record. "
                    "It was likely placed without a deposit operation. "
                    "Use admin resolution (admin_session_start) to handle it."
                ),
            })
            return

        # ── Guard 2: swap detection ───────────────────────────────────────────
        # If target_lid is occupied by a DIFFERENT phone, this is a swap.
        # Proceeding would create two active DB records at target_lid.
        # Admin resolution handles swaps safely via staging + step-lock.
        if target_lid != original_lid:
            blocking_pid = SlotMonitorDB.get_pid_for_lid(target_lid)
            if blocking_pid is not None and blocking_pid != pid:
                logger.warning(
                    f"handle_verify: swap detected — "
                    f"PID={pid} should go to lid={target_lid} "
                    f"but lid={target_lid} is occupied by PID={blocking_pid}. "
                    f"Redirecting to admin resolution."
                )
                emit("operation_error", {
                    "status": "error",
                    "message": "swap_requires_admin_resolution",
                    "pid": pid,
                    "original_lid": original_lid,
                    "target_lid": target_lid,
                    "blocking_pid": blocking_pid,
                    "detail": (
                        f"Slot {target_lid} is occupied by phone {blocking_pid}. "
                        f"This is a swap — use admin resolution (admin_session_start)."
                    ),
                })
                return

        self._pause_slot(original_lid)
        if target_lid != original_lid:
            self._pause_slot(target_lid)

        try:
            op_ctx.start(client_id, "verify", pid=pid, lid=target_lid, original_lid=original_lid)
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(original_lid, is_occupied=True)
            if target_lid != original_lid:
                self._restore_slot(target_lid, is_occupied=False)
            return

        same_slot = target_lid == original_lid
        emit("verify_waiting_for_action", {
            "status": "waiting",
            "pid": pid,
            "original_lid": original_lid,
            "target_lid": target_lid,
            "same_slot": same_slot,
            "message": (
                f"Take phone {pid} from slot {original_lid}, scan QR, place back in same slot."
                if same_slot else
                f"Take phone {pid} from slot {original_lid}, scan QR, place in slot {target_lid}."
            ),
        })

    # ============================================================
    # QR SCANNED
    # ============================================================

    def handle_qr_scanned(self, data: dict):
        client_id = request.sid

        if not op_ctx.is_active(client_id):
            emit("operation_error", {"status": "error", "message": "no_active_operation"})
            return

        op = op_ctx.get(client_id)

        scan_result = scan_and_validate_pid(camera_index=2, timeout_sec=QR_SCAN_TIMEOUT)
        if scan_result["status"] != "success":
            emit("operation_error", scan_result)
            op_ctx.clear(client_id)
            return

        scanned_pid = scan_result["pid"]   # str, consistent with op.pid
        if scanned_pid != op.pid:
            emit("operation_error", {
                "status": "error",
                "message": "pid_mismatch",
                "expected_pid": op.pid,
                "scanned_pid": scanned_pid,
            })
            op_ctx.clear(client_id)
            return

        op_ctx.qr_scanned(client_id)

        dispatch = {
            "deposit": self._complete_deposit,
            "withdraw": self._complete_withdraw,
            "verify": self._complete_verify,
        }
        dispatch[op.op_type](op)

    # ============================================================
    # COMPLETION HANDLERS
    # ============================================================

    def _complete_deposit(self, op):
        pid, lid, client_id = op.pid, op.lid, op.client_id

        # Capture baseline first — confirms phone was actually placed
        baseline_result = self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=True, wait_for_stable=2.0
        )
        if baseline_result["status"] != "success":
            emit("deposit_result", {
                "status": "error",
                "message": "baseline_capture_failed",
                "pid": pid, "lid": lid,
            })
            op_ctx.clear(client_id)
            return

        db_result = self.slot_ops.deposit_phone_db(pid, lid)
        if db_result["status"] != "success":
            emit("deposit_result", db_result)
            op_ctx.clear(client_id)
            return

        self._resume_slot(lid)
        op_ctx.complete(client_id)
        emit("deposit_result", {
            "status": "success",
            "pid": pid, "lid": lid,
            "storage_id": db_result.get("storage_id"),
        })

    def _complete_withdraw(self, op):
        pid, lid, client_id = op.pid, op.lid, op.client_id

        db_result = self.slot_ops.withdraw_phone_db(pid)
        if db_result["status"] != "success":
            emit("withdraw_result", db_result)
            op_ctx.clear(client_id)
            return

        # Capture empty-slot baseline; also sets slot.is_occupied = False
        self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=False, wait_for_stable=2.0
        )

        # Operation succeeded — lift pause without resetting slot state
        self._resume_slot(lid)
        op_ctx.complete(client_id)
        emit("withdraw_result", {
            "status": "success",
            "pid": pid, "lid": lid,
            "storage_id": db_result.get("storage_id"),
        })

    def _complete_verify(self, op):
        pid, original_lid, target_lid, client_id = (
            op.pid, op.original_lid, op.lid, op.client_id
        )
        same_slot = original_lid == target_lid

        if not same_slot:
            # At this point we know target_lid is empty (swap guard in handle_verify
            # blocked the case where it was occupied by a different phone).
            if not SlotMonitorDB.update_storage_lid(pid, target_lid):
                emit("verify_result", {"status": "error", "message": "database_update_failed"})
                op_ctx.clear(client_id)
                return

        # Capture baseline for target slot (phone is now here)
        self.slot_ops.capture_and_save_baseline(
            lid=target_lid, is_occupied=True, wait_for_stable=1.5
        )
        self._resume_slot(target_lid)

        if not same_slot:
            # Original slot is now empty — phone was moved to target_lid
            self.slot_ops.capture_and_save_baseline(
                lid=original_lid, is_occupied=False, wait_for_stable=1.5
            )
            self._resume_slot(original_lid)

        op_ctx.complete(client_id)
        emit("verify_result", {
            "status": "success",
            "pid": pid,
            "original_lid": original_lid,
            "target_lid": target_lid,
        })

    # ============================================================
    # SLOT HELPERS
    # ============================================================

    def _pause_slot(self, lid: int):
        if self.slot_ops.monitor and self.slot_ops.monitor.worker_pool:
            self.slot_ops.monitor.worker_pool.pause_slot(lid)

    def _resume_slot(self, lid: int):
        """Lift pause — slot state is already correct (successful operation)."""
        if self.slot_ops.monitor and self.slot_ops.monitor.worker_pool:
            self.slot_ops.monitor.worker_pool.resume_slot(lid)

    def _restore_slot(self, lid: int, is_occupied: bool):
        """Lift pause and reset slot state (failed/cancelled operation)."""
        if self.slot_ops.monitor and self.slot_ops.monitor.worker_pool:
            self.slot_ops.monitor.worker_pool.restore_slot(lid, is_occupied)


# ============================================================
# REGISTRATION
# ============================================================

def register_dvw_handlers(socketio: SocketIO, slot_operations: SlotOperations):
    handler = DVWSocketHandler(slot_operations, socketio)

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
                "active": True,
                "mismatch_count": len(mismatches),
                "mismatches": mismatches,
            }, to=client_id)
        else:
            emit("alarm_status", {"active": False}, to=client_id)

    @socketio.on("alarm_acknowledge")
    def on_alarm_acknowledge(data):
        client_id = request.sid
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm is None:
            emit("operation_error", {"status": "error", "message": "alarm_not_available"}, to=client_id)
            return
        password = data.get("password", "")
        result = alarm.authenticate_admin(password)
        if result["authenticated"]:
            alarm.clear()
            emit("alarm_acknowledge_result", {"status": "success"}, to=client_id)
        else:
            emit("alarm_acknowledge_result", {"status": "error", "message": "wrong_password"}, to=client_id)

    logger.info("DVW WebSocket handlers registered")