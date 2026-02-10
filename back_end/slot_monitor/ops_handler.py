# ============================================================
# FILE: server/socket_handlers.py
# ============================================================
"""
WebSocket handlers for Deposit/Withdraw/Verification operations.

Flow:
1. User initiates operation → handler starts operation context
2. Handler resolves LID and pauses alarms
3. User performs physical action (place/remove phone)
4. User scans QR code
5. Handler validates PID match
6. Handler completes DB mutation and baseline capture
7. Handler resumes alarms and clears context

Events:
- Client → Server: "deposit", "withdraw", "verify", "qr_scanned"
- Server → Client: operation_result, operation_error, waiting_for_qr, etc.
"""

import logging
import asyncio
from flask_socketio import emit, SocketIO
from typing import Optional

from back_end.slot_monitor.operation_context import op_ctx
from back_end.slot_monitor.qr_scanner import scan_and_validate_pid
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB

logger = logging.getLogger(__name__)


class DVWSocketHandler:
    """
    Handles Deposit/Withdraw/Verification operations via WebSocket.

    Dependencies:
    - slot_operations: SlotOperations instance
    - socketio: Flask-SocketIO instance
    """

    def __init__(self, slot_operations: SlotOperations, socketio: SocketIO):
        self.slot_ops = slot_operations
        self.socketio = socketio
        logger.info("DVWSocketHandler initialized")

    # ============================================================
    # DEPOSIT OPERATION
    # ============================================================

    def handle_deposit(self, data: dict):
        """
        Handle deposit request.

        Flow:
        1. Validate PID exists
        2. Find next free LID
        3. Pause alarms on target LID
        4. Start operation context
        5. Wait for user to scan QR and place phone

        Args:
            data: {"pid": int}
        """
        pid = data.get("pid")
        if pid is None:
            emit("operation_error", {
                "status": "error",
                "message": "missing_pid"
            })
            return

        logger.info(f"📥 Deposit request: PID={pid}")

        # Validate PID exists
        if not SlotMonitorDB.pid_exists(pid):
            emit("operation_error", {
                "status": "error",
                "message": "pid_not_found",
                "pid": pid
            })
            return

        # Check if phone is already stored
        conn = SlotMonitorDB.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE pid = %s
                                            AND retrieved_at IS NULL);
                            """, (pid,))
                already_stored = cur.fetchone()[0]

            if already_stored:
                emit("operation_error", {
                    "status": "error",
                    "message": "phone_already_stored",
                    "pid": pid
                })
                return

        finally:
            SlotMonitorDB.put_conn(conn)

        # Find next free location
        conn = SlotMonitorDB.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT l.lid
                            FROM locations l
                                     LEFT JOIN phone_storage ps ON l.lid = ps.lid
                                AND ps.retrieved_at IS NULL
                            WHERE ps.pid IS NULL
                            ORDER BY l.lid
                            LIMIT 1;
                            """)

                row = cur.fetchone()
                if not row:
                    emit("operation_error", {
                        "status": "error",
                        "message": "no_free_slots"
                    })
                    return

                lid = row[0]

        finally:
            SlotMonitorDB.put_conn(conn)

        # Pause alarms on target slot
        if self.slot_ops.monitor:
            self.slot_ops.monitor.pause_slot(lid)
            logger.info(f"⏸️  Alarms paused for LID={lid}")

        # Start operation context
        try:
            op_ctx.start("deposit", pid=pid, lid=lid)
        except RuntimeError as e:
            emit("operation_error", {
                "status": "error",
                "message": "operation_already_active"
            })
            # Resume alarms on error
            if self.slot_ops.monitor:
                self.slot_ops.monitor.resume_slot(lid, None, False)
            return

        # Notify client
        emit("deposit_waiting_for_qr", {
            "status": "waiting",
            "pid": pid,
            "lid": lid,
            "message": f"Please scan QR code for PID {pid}, then place phone in slot {lid}"
        })

        logger.info(f"Deposit operation started: PID={pid}, LID={lid}")

    # ============================================================
    # WITHDRAW OPERATION
    # ============================================================

    def handle_withdraw(self, data: dict):
        """
        Handle withdrawal request.

        Flow:
        1. Validate PID exists and is stored
        2. Find current LID
        3. Pause alarms on source LID
        4. Start operation context
        5. Wait for user to remove phone and scan QR

        Args:
            data: {"pid": int}
        """
        pid = data.get("pid")
        if pid is None:
            emit("operation_error", {
                "status": "error",
                "message": "missing_pid"
            })
            return

        logger.info(f"📤 Withdraw request: PID={pid}")

        # Get current storage location
        conn = SlotMonitorDB.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT lid
                            FROM phone_storage
                            WHERE pid = %s
                              AND retrieved_at IS NULL
                            LIMIT 1;
                            """, (pid,))

                row = cur.fetchone()
                if not row:
                    emit("operation_error", {
                        "status": "error",
                        "message": "phone_not_in_storage",
                        "pid": pid
                    })
                    return

                lid = row[0]

        finally:
            SlotMonitorDB.put_conn(conn)

        # Pause alarms on source slot
        if self.slot_ops.monitor:
            self.slot_ops.monitor.pause_slot(lid)
            logger.info(f"⏸️  Alarms paused for LID={lid}")

        # Start operation context
        try:
            op_ctx.start("withdraw", pid=pid, lid=lid)
        except RuntimeError as e:
            emit("operation_error", {
                "status": "error",
                "message": "operation_already_active"
            })
            # Resume alarms on error
            if self.slot_ops.monitor:
                self.slot_ops.monitor.resume_slot(lid, None, True)
            return

        # Notify client
        emit("withdraw_waiting_for_action", {
            "status": "waiting",
            "pid": pid,
            "lid": lid,
            "message": f"Please remove phone PID {pid} from slot {lid}, then scan QR code"
        })

        logger.info(f"Withdraw operation started: PID={pid}, LID={lid}")

    # ============================================================
    # VERIFICATION OPERATION
    # ============================================================

    def handle_verify(self, data: dict):
        """
        Handle verification request (alarm clearance).

        Flow:
        1. System detected mismatch on a slot
        2. Admin must remove phone, scan QR, and replace in calculated slot
        3. Validate PID matches expected
        4. Update storage location if needed

        Args:
            data: {"pid": int, "original_lid": int, "target_lid": int}
        """
        pid = data.get("pid")
        original_lid = data.get("original_lid")
        target_lid = data.get("target_lid")

        if pid is None or original_lid is None or target_lid is None:
            emit("operation_error", {
                "status": "error",
                "message": "missing_parameters"
            })
            return

        logger.info(f"🔍 Verify request: PID={pid}, from LID={original_lid} to LID={target_lid}")

        # Pause alarms on both slots
        if self.slot_ops.monitor:
            self.slot_ops.monitor.pause_slot(original_lid)
            self.slot_ops.monitor.pause_slot(target_lid)
            logger.info(f"⏸️  Alarms paused for LID={original_lid} and LID={target_lid}")

        # Start operation context
        try:
            op_ctx.start(
                "verify",
                pid=pid,
                lid=target_lid,
                original_lid=original_lid
            )
        except RuntimeError as e:
            emit("operation_error", {
                "status": "error",
                "message": "operation_already_active"
            })
            # Resume alarms on error
            if self.slot_ops.monitor:
                self.slot_ops.monitor.resume_slot(original_lid, None, True)
                self.slot_ops.monitor.resume_slot(target_lid, None, False)
            return

        # Notify client
        emit("verify_waiting_for_action", {
            "status": "waiting",
            "pid": pid,
            "original_lid": original_lid,
            "target_lid": target_lid,
            "message": f"Please remove phone from slot {original_lid}, scan QR, then place in slot {target_lid}"
        })

        logger.info(f"Verify operation started: PID={pid}, {original_lid} → {target_lid}")

    # ============================================================
    # QR SCANNED (Universal Handler)
    # ============================================================

    def handle_qr_scanned(self, data: dict):
        """
        Handle QR code scan event.

        Validates scanned PID matches expected PID, then completes the operation.

        This is called AFTER user has performed the physical action:
        - Deposit: Phone is already placed
        - Withdraw: Phone is already removed
        - Verify: Phone is already moved
        """
        if not op_ctx.is_active():
            emit("operation_error", {
                "status": "error",
                "message": "no_active_operation"
            })
            return

        state = op_ctx.get_state()
        logger.info(f"📷 QR scan triggered for {state['op_type']} operation")

        # Scan and validate QR code
        scan_result = scan_and_validate_pid(camera_index=0)

        if scan_result["status"] != "success":
            emit("operation_error", scan_result)
            self._cleanup_failed_operation(state)
            return

        scanned_pid = scan_result["pid"]
        expected_pid = state["expected_pid"]

        # CRITICAL: Verify PID match
        if scanned_pid != expected_pid:
            logger.error(
                f"❌ PID mismatch: expected {expected_pid}, scanned {scanned_pid}"
            )
            emit("operation_error", {
                "status": "error",
                "message": "pid_mismatch",
                "expected_pid": expected_pid,
                "scanned_pid": scanned_pid
            })
            self._cleanup_failed_operation(state)
            return

        # PID VERIFIED ✅ - Proceed with operation
        logger.info(f"✅ PID verified: {scanned_pid}")

        if state["op_type"] == "deposit":
            self._complete_deposit(state)

        elif state["op_type"] == "withdraw":
            self._complete_withdraw(state)

        elif state["op_type"] == "verify":
            self._complete_verify(state)

    # ============================================================
    # OPERATION COMPLETION HANDLERS
    # ============================================================

    def _complete_deposit(self, state: dict):
        """Complete deposit operation after QR verification."""
        pid = state["expected_pid"]
        lid = state["expected_lid"]

        logger.info(f"Completing deposit: PID={pid}, LID={lid}")

        # Step 1: Create DB record
        db_result = self.slot_ops.deposit_phone_db(pid, lid)
        if db_result["status"] != "success":
            emit("deposit_result", db_result)
            self._cleanup_failed_operation(state)
            return

        # Step 2: Capture new baseline (phone is in slot)
        baseline_result = self.slot_ops.capture_and_save_baseline(
            lid=lid,
            is_occupied=True,
            wait_for_stable=2.0
        )

        if baseline_result["status"] != "success":
            emit("deposit_result", {
                "status": "error",
                "message": "deposit_successful_but_baseline_failed",
                "pid": pid,
                "lid": lid
            })
            # Still resume monitoring with default baseline
            if self.slot_ops.monitor:
                import numpy as np
                self.slot_ops.monitor.resume_slot(lid, np.zeros(512), True)
            op_ctx.clear()
            return

        # Step 3: Success - alarms resumed by baseline capture
        emit("deposit_result", {
            "status": "success",
            "message": "Phone deposited successfully",
            "pid": pid,
            "lid": lid,
            "storage_id": db_result.get("storage_id")
        })

        op_ctx.clear()
        logger.info(f"✅ Deposit completed: PID={pid}, LID={lid}")

    def _complete_withdraw(self, state: dict):
        """Complete withdrawal operation after QR verification."""
        pid = state["expected_pid"]
        lid = state["expected_lid"]

        logger.info(f"Completing withdrawal: PID={pid}, LID={lid}")

        # Step 1: Update DB record
        db_result = self.slot_ops.withdraw_phone_db(pid)
        if db_result["status"] != "success":
            emit("withdraw_result", db_result)
            self._cleanup_failed_operation(state)
            return

        # Step 2: Capture new baseline (slot is now empty)
        baseline_result = self.slot_ops.capture_and_save_baseline(
            lid=lid,
            is_occupied=False,
            wait_for_stable=2.0
        )

        if baseline_result["status"] != "success":
            emit("withdraw_result", {
                "status": "error",
                "message": "withdrawal_successful_but_baseline_failed",
                "pid": pid,
                "lid": lid
            })
            # Remove from monitoring
            if self.slot_ops.monitor:
                self.slot_ops.monitor.remove_slot(lid)
            op_ctx.clear()
            return

        # Step 3: Success - remove from monitoring
        if self.slot_ops.monitor:
            self.slot_ops.monitor.remove_slot(lid)

        emit("withdraw_result", {
            "status": "success",
            "message": "Phone withdrawn successfully",
            "pid": pid,
            "lid": lid,
            "storage_id": db_result.get("storage_id")
        })

        op_ctx.clear()
        logger.info(f"✅ Withdrawal completed: PID={pid}, LID={lid}")

    def _complete_verify(self, state: dict):
        """Complete verification operation after QR scan."""
        pid = state["expected_pid"]
        original_lid = state["original_lid"]
        target_lid = state["expected_lid"]

        logger.info(f"Completing verification: PID={pid}, {original_lid} → {target_lid}")

        # Step 1: Update storage record to new location
        conn = SlotMonitorDB.get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            UPDATE phone_storage
                            SET lid = %s
                            WHERE pid = %s
                              AND retrieved_at IS NULL;
                            """, (target_lid, pid))
                conn.commit()

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to update storage location: {e}")
            emit("verify_result", {
                "status": "error",
                "message": "database_update_failed"
            })
            self._cleanup_failed_operation(state)
            return
        finally:
            SlotMonitorDB.put_conn(conn)

        # Step 2: Capture baseline for original slot (now empty)
        baseline_original = self.slot_ops.capture_and_save_baseline(
            lid=original_lid,
            is_occupied=False,
            wait_for_stable=1.5
        )

        # Step 3: Capture baseline for target slot (now occupied)
        baseline_target = self.slot_ops.capture_and_save_baseline(
            lid=target_lid,
            is_occupied=True,
            wait_for_stable=1.5
        )

        # Step 4: Resume monitoring (remove original, monitor target)
        if self.slot_ops.monitor:
            self.slot_ops.monitor.remove_slot(original_lid)
            # Target slot monitoring resumed by capture_and_save_baseline

        emit("verify_result", {
            "status": "success",
            "message": "Phone verified and relocated successfully",
            "pid": pid,
            "original_lid": original_lid,
            "target_lid": target_lid
        })

        op_ctx.clear()
        logger.info(f"✅ Verification completed: PID={pid}, {original_lid} → {target_lid}")

    # ============================================================
    # ERROR CLEANUP
    # ============================================================

    def _cleanup_failed_operation(self, state: dict):
        """Resume alarms and clear context on operation failure."""
        op_type = state["op_type"]
        lid = state["expected_lid"]
        original_lid = state.get("original_lid")

        logger.warning(f"Cleaning up failed {op_type} operation")

        if self.slot_ops.monitor:
            import numpy as np

            if op_type == "deposit":
                # Resume as empty (deposit failed)
                self.slot_ops.monitor.resume_slot(lid, np.zeros(512), False)

            elif op_type == "withdraw":
                # Resume as occupied (withdrawal failed)
                self.slot_ops.monitor.resume_slot(lid, np.zeros(512), True)

            elif op_type == "verify":
                # Resume both slots to previous states
                self.slot_ops.monitor.resume_slot(original_lid, np.zeros(512), True)
                self.slot_ops.monitor.resume_slot(lid, np.zeros(512), False)

        op_ctx.clear()


# ============================================================
# FLASK-SOCKETIO REGISTRATION
# ============================================================

def register_dvw_handlers(socketio: SocketIO, slot_operations: SlotOperations):
    """
    Register DVW WebSocket handlers with Flask-SocketIO.

    Usage:
        from socket_handlers import register_dvw_handlers

        app = Flask(__name__)
        socketio = SocketIO(app)
        slot_ops = SlotOperations(monitor, embedder)

        register_dvw_handlers(socketio, slot_ops)
    """
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

    logger.info("✅ DVW WebSocket handlers registered")