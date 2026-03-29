# ============================================================
# FILE: back_end/slot_monitor/admin/resolution_session.py
# ============================================================
"""
Per-connection admin resolution session state.

Tracks:
  - Which phone is currently in-hand (current_pid, current_from_lid)
  - Whether that phone belongs back in the same slot (current_same_slot)
  - Which phones have been staged
  - Which phones have been resolved
  - Which phones are still pending
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ResolutionSession:

    def __init__(self, client_id: str, slot_ops, alarm, socketio):
        self.client_id  = client_id
        self.slot_ops   = slot_ops
        self.alarm      = alarm
        self.socketio   = socketio

        self._session_id   = f"res-{int(time.time())}-{client_id[:6]}"
        self._opened_at    = time.time()

        # All lids/pids that were mismatched when the session opened
        self._mismatch_map: dict[str, int] = {}  # pid → expected_lid

        # Per-phone tracking
        self._remaining:  list[str] = []   # pids not yet resolved
        self._staged:     list[str] = []   # pids currently in staging zone
        self._resolved:   list[str] = []   # pids successfully placed
        self._missing:    list[str] = []   # pids declared missing
        self._needs_dep:  list[str] = []   # pids that need normal deposit

        # Current phone in hand
        self.current_pid:       Optional[str] = None
        self.current_from_lid:  Optional[int] = None
        self.current_same_slot: bool          = False
        self._current_expected: Optional[int] = None

    # ── Open / close ──────────────────────────────────────

    def open(self):
        """Populate mismatch list from alarm state and notify client."""
        with self.alarm._lock:
            mismatches = list(self.alarm.mismatches)

        self._mismatch_map = {pid: lid for pid, lid in mismatches}
        self._remaining    = list(self._mismatch_map.keys())

        logger.info(
            f"[ResolutionSession] {self._session_id} opened — "
            f"{len(self._remaining)} mismatches"
        )

        self.socketio.emit(
            "admin_session_opened",
            {
                "session_id": self._session_id,
                "mismatches": [
                    {"pid": pid, "expected_lid": lid}
                    for pid, lid in self._mismatch_map.items()
                ],
            },
            to=self.client_id,
            namespace="/",
        )

    def close(self):
        logger.info(f"[ResolutionSession] {self._session_id} closed")

    def build_summary(self) -> dict:
        return {
            "session_id":        self._session_id,
            "duration_s":        round(time.time() - self._opened_at, 1),
            "resolved":          list(self._resolved),
            "declared_missing":  list(self._missing),
            "needs_deposit":     list(self._needs_dep),
        }

    # ── Phone tracking ────────────────────────────────────

    def record_removal(self, from_lid: int) -> bool:
        """
        Admin has physically picked up the object from from_lid.
        Returns False if nothing was expected there.
        """
        self.current_from_lid  = from_lid
        self.current_pid       = None
        self.current_same_slot = False
        self._current_expected = None
        return True   # always accept; QR scan determines validity

    def record_qr_result(
        self,
        pid: str,
        expected_lid: Optional[int],
        same_slot: bool,
    ):
        """Store QR scan result for the phone currently in hand."""
        self.current_pid       = pid
        self._current_expected = expected_lid
        self.current_same_slot = same_slot

    def record_unknown_object(self, from_lid: int):
        """No QR found — object has no identity."""
        self.current_pid       = f"unknown-{from_lid}"
        self._current_expected = from_lid
        self.current_same_slot = False

    def get_expected_lid(self, pid: str) -> Optional[int]:
        if pid == self.current_pid:
            return self._current_expected
        return self._mismatch_map.get(pid)

    def stage(self, pid: str):
        if pid not in self._staged:
            self._staged.append(pid)
        self.current_pid       = None
        self.current_from_lid  = None
        self.current_same_slot = False

    def unstage(self, pid: str):
        if pid in self._staged:
            self._staged.remove(pid)
        self.current_pid      = pid
        self.current_from_lid = self._mismatch_map.get(pid)
        self.current_same_slot = False   # unstaged phones always go to their slot

    def is_staged(self, pid: str) -> bool:
        return pid in self._staged

    def mark_resolved(self, pid: str):
        self._remaining = [p for p in self._remaining if p != pid]
        if pid not in self._resolved:
            self._resolved.append(pid)
        self.current_pid       = None
        self.current_from_lid  = None
        self.current_same_slot = False
        self._current_expected = None

    def mark_missing(self, pid: str):
        self._remaining = [p for p in self._remaining if p != pid]
        if pid not in self._missing:
            self._missing.append(pid)

    def mark_needs_deposit(self, pid: str):
        self._remaining = [p for p in self._remaining if p != pid]
        if pid not in self._needs_dep:
            self._needs_dep.append(pid)

    def remaining_pids(self) -> list:
        return list(self._remaining)

    def staged_pids(self) -> list:
        return list(self._staged)

    @property
    def session_id(self) -> str:
        return self._session_id