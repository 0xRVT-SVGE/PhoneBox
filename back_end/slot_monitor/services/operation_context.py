# ============================================================
# FILE: server/slot_monitor/operation_context.py
# ============================================================
"""
Manages state for active Deposit/Withdraw/Verification operations.

Design:
    - Operation records pre-operation slot occupancy at start()
    - complete() → success, no restore needed
    - clear()    → failure/cancel, restores slots to pre-operation state
    - Timeout always calls clear() — no special casing per op type
    - Alarm system handles physical reality independently
"""

import logging
import time
from typing import Optional, Literal, Dict
from threading import Lock, Thread, Event
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

OperationType = Literal["deposit", "withdraw", "verify"]


@dataclass
class Operation:
    """Single DVW operation state."""
    op_type: OperationType
    client_id: str
    pid: str
    sid: Optional[str] = None
    lid: Optional[int] = None
    original_lid: Optional[int] = None

    stage: str = "waiting_qr"
    started_at: float = field(default_factory=time.time)
    qr_scanned_at: Optional[float] = None

    # Occupancy of each slot BEFORE this operation started.
    # Restored exactly on failure/timeout — no guessing.
    #   deposit:  lid was empty   (False)
    #   withdraw: lid was occupied (True)
    #   verify:   original_lid occupied (True), target lid empty (False)
    lid_occupied_before: bool = False
    original_lid_occupied_before: bool = True

    def is_expired(self, timeout: float) -> bool:
        elapsed = time.time() - (self.qr_scanned_at or self.started_at)
        return elapsed > timeout


class OperationContext:
    """
    Thread-safe manager for active DVW operations.

    One operation per client at a time. Multiple clients can operate concurrently.

    Timeouts:
        QR_SCAN_TIMEOUT:  seconds allowed to scan QR after initiating
        ACTION_TIMEOUT:   seconds allowed to complete physical action after QR scan
        CLEANUP_INTERVAL: how often the background thread checks for expiry
    """

    QR_SCAN_TIMEOUT = 5.0
    ACTION_TIMEOUT = 5.0
    CLEANUP_INTERVAL = 5.0

    def __init__(self):
        self._lock = Lock()
        self._operations: Dict[str, Operation] = {}
        self._cleanup_thread: Optional[Thread] = None
        self._stop_cleanup = Event()
        self._worker_pool = None

    # ============================================================
    # WORKER POOL INJECTION
    # ============================================================

    def set_worker_pool(self, worker_pool):
        """Inject WorkerPool after HeadlessSlotMonitor.start()."""
        self._worker_pool = worker_pool
        logger.info("WorkerPool attached to OperationContext")

    # ============================================================
    # CLEANUP THREAD
    # ============================================================

    def start_cleanup_thread(self):
        if self._cleanup_thread is not None:
            return
        self._stop_cleanup.clear()
        self._cleanup_thread = Thread(
            target=self._cleanup_loop,
            daemon=True,
            name="OpCtxCleanup",
        )
        self._cleanup_thread.start()
        logger.info("Operation cleanup thread started")

    def stop_cleanup_thread(self):
        self._stop_cleanup.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2.0)
            self._cleanup_thread = None
        logger.info("Operation cleanup thread stopped")

    def _cleanup_loop(self):
        while not self._stop_cleanup.wait(timeout=self.CLEANUP_INTERVAL):
            self._cleanup_expired()

    def _cleanup_expired(self):
        with self._lock:
            expired = [
                client_id for client_id, op in self._operations.items()
                if (op.stage == "waiting_qr" and op.is_expired(self.QR_SCAN_TIMEOUT))
                or (op.stage == "waiting_action" and op.is_expired(self.ACTION_TIMEOUT))
            ]

        for client_id in expired:
            with self._lock:
                op = self._operations.pop(client_id, None)
            if op is None:
                continue
            logger.warning(
                f"Operation timed out: {op.op_type.upper()} "
                f"client={client_id} PID={op.pid} stage={op.stage}"
            )
            self._restore_slots(op)

    # ============================================================
    # SLOT RESTORE
    # ============================================================

    def _restore_slots(self, op: Operation):
        """
        Restore paused slots to their pre-operation state.

        deposit/withdraw: restores op.lid
        verify:           restores op.original_lid and op.lid
        """
        if self._worker_pool is None:
            logger.warning(
                "WorkerPool not injected — slots cannot be restored. "
                "Call op_ctx.set_worker_pool(monitor.worker_pool) after monitor starts."
            )
            return

        if op.op_type in ("deposit", "withdraw"):
            if op.lid is not None:
                logger.info(
                    f"Restoring slot {op.lid} "
                    f"is_occupied={op.lid_occupied_before} ({op.op_type} rolled back)"
                )
                self._worker_pool.restore_slot(op.lid, op.lid_occupied_before)

        elif op.op_type == "verify":
            if op.original_lid is not None:
                logger.info(
                    f"Restoring slot {op.original_lid} "
                    f"is_occupied={op.original_lid_occupied_before} (verify rolled back)"
                )
                self._worker_pool.restore_slot(
                    op.original_lid, op.original_lid_occupied_before
                )
            if op.lid is not None:
                logger.info(
                    f"Restoring slot {op.lid} "
                    f"is_occupied={op.lid_occupied_before} (verify rolled back)"
                )
                self._worker_pool.restore_slot(op.lid, op.lid_occupied_before)

    # ============================================================
    # OPERATION LIFECYCLE
    # ============================================================

    def start(
            self,
            client_id: str,
            op_type: OperationType,
            pid: str,
            sid: Optional[str] = None,
            lid: Optional[int] = None,
            original_lid: Optional[int] = None,
    ):
        """
        Start a new operation and record pre-operation slot occupancy.

        Occupancy inferred from op_type:
            deposit:  lid was empty
            withdraw: lid was occupied
            verify:   original_lid occupied, target lid empty

        Raises:
            RuntimeError: if this client already has an active operation.
        """
        with self._lock:
            if client_id in self._operations:
                existing = self._operations[client_id]
                raise RuntimeError(
                    f"Client {client_id} already has active {existing.op_type} "
                    f"for PID {existing.pid}"
                )

            op = Operation(
                op_type=op_type,
                client_id=client_id,
                pid=pid,
                sid=sid,
                lid=lid,
                original_lid=original_lid,
                stage="waiting_qr",
                lid_occupied_before=op_type == "withdraw",
                # original_lid_occupied_before defaults True (verify source was occupied)
            )
            self._operations[client_id] = op

        logger.info(
            f"Operation started: {op_type.upper()} "
            f"client={client_id} PID={pid} SID={sid} LID={lid} orig={original_lid}"
        )

    def qr_scanned(self, client_id: str) -> bool:
        """Advance to waiting_action after QR scan. Returns False if no active op."""
        with self._lock:
            op = self._operations.get(client_id)
            if op is None:
                return False
            op.stage = "waiting_action"
            op.qr_scanned_at = time.time()
        logger.info(
            f"QR scanned: {op.op_type} client={client_id} PID={op.pid} "
            f"→ waiting for physical action"
        )
        return True

    def complete(self, client_id: str):
        """
        Remove a successfully completed operation.
        Slot state is already correct — no restore needed.
        """
        with self._lock:
            op = self._operations.pop(client_id, None)
        if op:
            logger.info(
                f"Operation completed: {op.op_type.upper()} "
                f"client={client_id} PID={op.pid}"
            )

    def clear(self, client_id: str):
        """
        Discard a failed or cancelled operation and restore slots to
        their pre-operation state.
        """
        with self._lock:
            op = self._operations.pop(client_id, None)
        if op is None:
            return
        logger.info(
            f"Operation failed/cancelled: {op.op_type} "
            f"client={client_id} PID={op.pid}"
        )
        self._restore_slots(op)

    def clear_all(self):
        """Clear all operations on shutdown. No restore — process is exiting."""
        with self._lock:
            count = len(self._operations)
            self._operations.clear()
        if count:
            logger.info(f"Cleared {count} active operations on shutdown")

    # ============================================================
    # QUERIES
    # ============================================================

    def get(self, client_id: str) -> Optional[Operation]:
        with self._lock:
            return self._operations.get(client_id)

    def get_by_pid(self, pid: str) -> Optional[Operation]:
        """Find an active operation by PID."""
        with self._lock:
            for op in self._operations.values():
                if op.pid == pid:
                    return op
        return None

    def get_withdraw_for_lid(self, lid: int) -> Optional[Operation]:
        """
        Find an active withdraw operation for a given lid.
        Alarm controller uses this to distinguish withdraw-without-scan
        from theft (no active operation on that lid).
        """
        with self._lock:
            for op in self._operations.values():
                if op.op_type == "withdraw" and op.lid == lid:
                    return op
        return None

    def is_active(self, client_id: str) -> bool:
        with self._lock:
            return client_id in self._operations

    def get_all_operations(self) -> Dict[str, dict]:
        with self._lock:
            return {
                client_id: {
                    "op_type": op.op_type,
                    "pid": op.pid,
                    "sid": op.sid,
                    "lid": op.lid,
                    "original_lid": op.original_lid,
                    "stage": op.stage,
                    "elapsed": time.time() - op.started_at,
                }
                for client_id, op in self._operations.items()
            }


# Global singleton
op_ctx = OperationContext()