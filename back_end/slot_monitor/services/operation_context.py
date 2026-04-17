# ============================================================
# FILE: back_end/slot_monitor/services/operation_context.py
# ============================================================
"""
Manages state for active Deposit/Withdraw/Verification operations.

Changes from original:
  - Operation.cancel_event   — threading.Event set by clear() to unblock
                                any blocking scan or tracker thread
  - Operation.background_frame — top-cam frame captured at op start,
                                  used as background reference for tracking
  - Operation.stage "tracking" — tracker running; excluded from auto-expiry
                                  since the tracker owns its own timeout
  - op_ctx.set_tracking()    — advances stage to "tracking"
  - cancel_event is set in clear() AND _cleanup_expired() so the QR scan
    loop and tracker both exit immediately on cancel or timeout
"""

import logging
import time
import threading
from typing import Optional, Dict, Any
from threading import Lock, Thread, Event
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

OperationType = str   # "deposit" | "withdraw" | "verify"


@dataclass
class Operation:
    op_type:      str
    client_id:    str
    pid:          str
    sid:          Optional[str]  = None
    lid:          Optional[int]  = None
    original_lid: Optional[int]  = None

    stage:           str   = "waiting_qr"
    started_at:      float = field(default_factory=time.time)
    qr_scanned_at:   Optional[float] = None

    lid_occupied_before:          bool = False
    original_lid_occupied_before: bool = True

    # Set by clear() / _cleanup_expired() to unblock the QR scan loop
    # and the phone tracker thread immediately.
    cancel_event: threading.Event = field(default_factory=threading.Event)

    # Top-cam frame captured at operation start (before the phone arrives).
    # Used as background reference for frame-diff phone detection.
    # None if top_camera had no frame yet at start time.
    background_frame: Any = None   # Optional[np.ndarray]

    def is_expired(self, timeout: float) -> bool:
        elapsed = time.time() - (self.qr_scanned_at or self.started_at)
        return elapsed > timeout


class OperationContext:
    """
    Thread-safe manager for active DVW operations.
    One operation per client at a time.
    """

    QR_SCAN_TIMEOUT  = 20.0   # seconds allowed to scan QR after initiating
    ACTION_TIMEOUT   = 40.0   # seconds allowed to place after QR scan
                               # (covers tracker detection + placement time)
    CLEANUP_INTERVAL = 5.0

    def __init__(self):
        self._lock              = Lock()
        self._operations:  Dict[str, Operation] = {}
        self._cleanup_thread:  Optional[Thread] = None
        self._stop_cleanup     = Event()
        self._worker_pool      = None

    # ── Worker pool injection ────────────────────────────

    def set_worker_pool(self, worker_pool):
        self._worker_pool = worker_pool
        logger.info("WorkerPool attached to OperationContext")

    # ── Cleanup thread ───────────────────────────────────

    def start_cleanup_thread(self):
        if self._cleanup_thread is not None:
            return
        self._stop_cleanup.clear()
        self._cleanup_thread = Thread(
            target=self._cleanup_loop, daemon=True, name="OpCtxCleanup"
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
                cid for cid, op in self._operations.items()
                if (
                    (op.stage == "waiting_qr"     and op.is_expired(self.QR_SCAN_TIMEOUT))
                    or
                    (op.stage == "waiting_action" and op.is_expired(self.ACTION_TIMEOUT))
                    # "tracking" is intentionally excluded — PhoneTracker owns its timeout
                )
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
            op.cancel_event.set()   # unblock any blocking scan/tracker
            self._restore_slots(op)

    # ── Slot restore ─────────────────────────────────────

    def _restore_slots(self, op: Operation):
        if self._worker_pool is None:
            logger.warning("WorkerPool not injected — slots cannot be restored.")
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
                self._worker_pool.restore_slot(
                    op.original_lid, op.original_lid_occupied_before
                )
            if op.lid is not None:
                self._worker_pool.restore_slot(op.lid, op.lid_occupied_before)

    # ── Lifecycle ─────────────────────────────────────────

    def start(
        self,
        client_id:    str,
        op_type:      str,
        pid:          str,
        sid:          Optional[str] = None,
        lid:          Optional[int] = None,
        original_lid: Optional[int] = None,
    ):
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
                lid_occupied_before=(op_type == "withdraw"),
            )
            self._operations[client_id] = op

        logger.info(
            f"Operation started: {op_type.upper()} "
            f"client={client_id} PID={pid} SID={sid} LID={lid} orig={original_lid}"
        )

    def qr_scanned(self, client_id: str) -> bool:
        with self._lock:
            op = self._operations.get(client_id)
            if op is None:
                return False
            op.stage          = "waiting_action"
            op.qr_scanned_at  = time.time()
        logger.info(f"QR scanned: {op.op_type} client={client_id} PID={op.pid}")
        return True

    def set_tracking(self, client_id: str) -> bool:
        """
        Advance stage to "tracking". The cleanup thread ignores this stage —
        PhoneTracker manages its own timeout and calls clear() when done.
        """
        with self._lock:
            op = self._operations.get(client_id)
            if op is None:
                return False
            op.stage = "tracking"
        logger.info(f"Operation stage tracking: client={client_id}")
        return True

    def complete(self, client_id: str):
        with self._lock:
            op = self._operations.pop(client_id, None)
        if op:
            logger.info(
                f"Operation completed: {op.op_type.upper()} "
                f"client={client_id} PID={op.pid}"
            )

    def clear(self, client_id: str):
        """
        Discard a failed/cancelled operation.
        Sets cancel_event BEFORE restoring slots so any blocking scan
        or tracker thread exits immediately.
        """
        with self._lock:
            op = self._operations.pop(client_id, None)
        if op is None:
            return
        op.cancel_event.set()   # unblock QR scan loop and tracker
        logger.info(
            f"Operation failed/cancelled: {op.op_type} "
            f"client={client_id} PID={op.pid}"
        )
        self._restore_slots(op)

    def clear_all(self):
        with self._lock:
            count = len(self._operations)
            ops = list(self._operations.values())
            self._operations.clear()
        for op in ops:
            op.cancel_event.set()
        if count:
            logger.info(f"Cleared {count} active operations on shutdown")

    # ── Queries ───────────────────────────────────────────

    def get(self, client_id: str) -> Optional[Operation]:
        with self._lock:
            return self._operations.get(client_id)

    def get_by_pid(self, pid: str) -> Optional[Operation]:
        with self._lock:
            for op in self._operations.values():
                if op.pid == pid:
                    return op
        return None

    def get_withdraw_for_lid(self, lid: int) -> Optional[Operation]:
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
                cid: {
                    "op_type":     op.op_type,
                    "pid":         op.pid,
                    "sid":         op.sid,
                    "lid":         op.lid,
                    "original_lid":op.original_lid,
                    "stage":       op.stage,
                    "elapsed":     time.time() - op.started_at,
                }
                for cid, op in self._operations.items()
            }


# Global singleton
op_ctx = OperationContext()