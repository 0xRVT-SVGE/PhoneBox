# ============================================================
# FILE: server/slot_monitor/operation_context.py
# ============================================================
"""
Manages state for active Deposit/Withdraw/Verification operations.

Ensures:
- Only one operation active at a time
- PID/LID tracking across websocket events
- Clean state transitions
"""

import logging
from typing import Optional, Literal
from threading import Lock

logger = logging.getLogger(__name__)

OperationType = Literal["deposit", "withdraw", "verify"]


class OperationContext:
    """
    Thread-safe singleton for tracking active DVW operations.

    Workflow:
    1. User initiates operation → start()
    2. QR scanned → validate against expected_pid
    3. Operation completes → clear()
    """

    def __init__(self):
        self._lock = Lock()
        self.active: bool = False
        self.op_type: Optional[OperationType] = None
        self.expected_pid: Optional[int] = None
        self.expected_lid: Optional[int] = None  # For withdraw/verify
        self.original_lid: Optional[int] = None  # For verify (where phone came from)

    def start(
            self,
            op_type: OperationType,
            pid: int,
            lid: Optional[int] = None,
            original_lid: Optional[int] = None
    ):
        """
        Start a new operation.

        Args:
            op_type: "deposit" | "withdraw" | "verify"
            pid: Expected phone ID
            lid: Expected location ID (for withdraw/verify)
            original_lid: Original location (for verify only)

        Raises:
            RuntimeError: If an operation is already active
        """
        with self._lock:
            if self.active:
                raise RuntimeError(
                    f"Operation already active: {self.op_type} for PID {self.expected_pid}"
                )

            self.active = True
            self.op_type = op_type
            self.expected_pid = pid
            self.expected_lid = lid
            self.original_lid = original_lid

            logger.info(
                f"Operation started: {op_type.upper()} - "
                f"PID={pid}, LID={lid}, OriginalLID={original_lid}"
            )

    def clear(self):
        """Clear operation state."""
        with self._lock:
            if self.active:
                logger.info(f"Operation cleared: {self.op_type} for PID {self.expected_pid}")

            self.active = False
            self.op_type = None
            self.expected_pid = None
            self.expected_lid = None
            self.original_lid = None

    def get_state(self) -> dict:
        """Get current operation state (thread-safe)."""
        with self._lock:
            return {
                "active": self.active,
                "op_type": self.op_type,
                "expected_pid": self.expected_pid,
                "expected_lid": self.expected_lid,
                "original_lid": self.original_lid
            }

    def is_active(self) -> bool:
        """Check if any operation is active."""
        with self._lock:
            return self.active


# Global singleton
op_ctx = OperationContext()