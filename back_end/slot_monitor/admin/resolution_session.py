# ============================================================
# FILE: back_end/slot_monitor/admin/resolution_session.py
# ============================================================
"""
Admin Resolution Session — state management.

Lifecycle:
    admin_ctx.open()   →  session created, mismatches snapshot taken
    (admin resolves phones one at a time via socket events)
    admin_ctx.close()  →  session torn down, summary returned

Step-lock:
    At most ONE phone may be in the admin's hand at a time (in_transit_pid).
    Additional phones may sit in the physical staging zone (staged_phones)
    while the admin clears a blocking slot — this is what makes safe swap
    resolution possible without ever having two phones unaccounted for.

Phone outcomes per PID:
    resolved_pids         — placed in correct slot, DB updated, baseline captured
    declared_missing_pids — never physically found; DB record withdrawn
    needs_deposit_pids    — found physically but had no storage record;
                            removed from slot, handed to normal deposit flow

Swap resolution (slot A has phone B, slot B has phone A):
    1. admin_remove_phone  {from_lid: A}
    2. admin_qr_scanned    {}   → pid=B, expected_lid=B, target_occupied=True
    3. admin_stage_phone   {}   → B staged, hand free
    4. admin_remove_phone  {from_lid: B}
    5. admin_qr_scanned    {}   → pid=A, expected_lid=A, target_occupied=False
       (target_occupied=False because B's DB record is stale — B is staged)
    6. admin_place_phone   {to_lid: A}  → A resolved ✓
    7. admin_unstage_phone {pid: B}     → B back in hand (no re-scan)
    8. admin_place_phone   {to_lid: B}  → B resolved ✓
    9. admin_session_close {}

Count invariant at close:
    DB stored count must equal phone_count_at_open − len(declared_missing_pids)
    needs_deposit phones are NOT subtracted — they leave the session and re-enter
    via normal deposit, keeping the count consistent once deposited.

TODO: Supervisor remote approval for missing declarations.
TODO: Lock staging ROI config changes during active session.
TODO: Camera-based staging zone presence detection.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

SESSION_TIMEOUT = 300.0   # 5 minutes — warning only, no forced close


@dataclass
class ResolutionSession:
    """One admin resolution session."""

    session_id: str
    client_id: str

    # Snapshot of alarm.mismatches at session open: {pid: expected_lid}
    # pid may be "unknown-{lid}" if slot had no DB record when alarm fired.
    initial_mismatches: dict

    # Phone count at open for count invariant.  -1 = DB read failed, skip check.
    phone_count_at_open: int

    started_at: float = field(default_factory=time.time)

    # ── Per-phone outcome tracking ────────────────────────
    resolved_pids: set = field(default_factory=set)
    declared_missing_pids: set = field(default_factory=set)
    needs_deposit_pids: set = field(default_factory=set)

    # PIDs whose slot was physically opened (admin_remove_phone called) at least once.
    # declare_missing is blocked until all OTHER phones have been visited,
    # preventing the admin from declaring a phone missing without checking all slots.
    visited_pids: set = field(default_factory=set)

    # ── Step-lock: at most 1 phone in hand ───────────────
    in_transit_pid: Optional[str] = None
    in_transit_from_lid: Optional[int] = None
    in_transit_qr_confirmed: bool = False

    # ── Staging zone: confirmed phones waiting for placement ─
    # {pid: from_lid}
    staged_phones: dict = field(default_factory=dict)

    # ── Helpers ──────────────────────────────────────────

    def is_expired(self) -> bool:
        return time.time() - self.started_at > SESSION_TIMEOUT

    def has_phone_in_hand(self) -> bool:
        return self.in_transit_pid is not None

    def pending_pids(self) -> set:
        """PIDs from initial_mismatches not yet given any outcome."""
        accounted = self.resolved_pids | self.declared_missing_pids | self.needs_deposit_pids
        return set(self.initial_mismatches) - accounted

    def all_clear(self) -> bool:
        return (
            not self.pending_pids()
            and not self.has_phone_in_hand()
            and not self.staged_phones
        )

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "duration_s": round(self.elapsed(), 1),
            "resolved": sorted(self.resolved_pids),
            "declared_missing": sorted(self.declared_missing_pids),
            "needs_deposit": sorted(self.needs_deposit_pids),
            "unresolved": sorted(self.pending_pids()),
            "still_staged": list(self.staged_phones.keys()),
            "phone_in_hand": self.in_transit_pid,
        }


class AdminSessionContext:
    """Thread-safe singleton manager for the active admin resolution session."""

    def __init__(self):
        self._lock = Lock()
        self._session: Optional[ResolutionSession] = None

    def open(
        self,
        client_id: str,
        initial_mismatches: dict,
        phone_count: int,
    ) -> ResolutionSession:
        with self._lock:
            if self._session is not None:
                s = self._session
                raise RuntimeError(
                    f"Session {s.session_id!r} already active "
                    f"(client={s.client_id}, elapsed={s.elapsed():.0f}s)"
                )
            session = ResolutionSession(
                session_id=uuid.uuid4().hex[:8],
                client_id=client_id,
                initial_mismatches=dict(initial_mismatches),
                phone_count_at_open=phone_count,
            )
            self._session = session

        logger.info(
            f"[AdminSession] OPEN session={session.session_id} "
            f"client={client_id} mismatches={len(initial_mismatches)} "
            f"phone_count={phone_count}"
        )
        return session

    def close(self) -> Optional[ResolutionSession]:
        with self._lock:
            session, self._session = self._session, None

        if session:
            logger.info(
                f"[AdminSession] CLOSE session={session.session_id} "
                f"resolved={len(session.resolved_pids)} "
                f"missing={len(session.declared_missing_pids)} "
                f"needs_deposit={len(session.needs_deposit_pids)} "
                f"duration={session.elapsed():.0f}s"
            )
        return session

    def get(self) -> Optional[ResolutionSession]:
        with self._lock:
            return self._session

    def is_active(self) -> bool:
        with self._lock:
            return self._session is not None


# Global singleton
admin_ctx = AdminSessionContext()