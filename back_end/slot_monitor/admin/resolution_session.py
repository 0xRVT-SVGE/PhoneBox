# ============================================================
# FILE: back_end/slot_monitor/admin/resolution_session.py
# ============================================================
"""
Admin resolution session state.

Exports used by admin_ops_handler:
    admin_ctx      — AdminSessionContext singleton
    SESSION_TIMEOUT — seconds before a session is considered expired

AdminSessionContext manages a single active ResolutionSession.
At most one session can be open at a time (school system assumption).

ResolutionSession holds all mutable per-session state that
admin_ops_handler reads and writes directly on the session object.
"""

import logging
import time
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)

# How long (seconds) an admin session may stay open before it is
# considered expired.  _check_timeout in admin_ops_handler logs a
# warning when this is exceeded but does NOT forcibly close the session —
# the admin must close it explicitly.
SESSION_TIMEOUT = 1800.0   # 30 minutes


# ══════════════════════════════════════════════════════════════════════════════
# ResolutionSession — per-session mutable state
# ══════════════════════════════════════════════════════════════════════════════

class ResolutionSession:
    """
    All state for one admin resolution session.

    admin_ops_handler reads and writes these attributes directly —
    they are intentionally public (no properties) for simplicity.

    Attribute reference
    ───────────────────
    session_id          str        "res-{ts}-{client_id[:6]}"
    client_id           str        WebSocket SID of the admin

    initial_mismatches  dict       {pid: expected_lid}  — snapshot at open time
    phone_count_at_open int        total phones in storage when session opened
                                   (-1 if DB query failed)

    staged_phones       dict       {pid: from_lid}  — phones currently in the
                                   physical staging area (not in any slot)

    visited_pids        set        PIDs whose slot the admin has physically
                                   opened (for "must visit all before missing")

    resolved_pids       set        PIDs successfully placed in correct slot
    declared_missing_pids set      PIDs declared missing (DB record withdrawn)
    needs_deposit_pids  set        PIDs found in box but with no DB record

    in_transit_pid          str|None   PID of phone currently in admin's hand
    in_transit_from_lid     int|None   lid it was removed from
    in_transit_qr_confirmed bool       True after admin_qr_scanned succeeds
    current_same_slot       bool       True when expected_lid == from_lid
    """

    def __init__(
        self,
        client_id:          str,
        initial_mismatches: Dict[str, int],
        phone_count:        int,
    ):
        self.session_id   = f"res-{int(time.time())}-{client_id[:6]}"
        self.client_id    = client_id
        self._opened_at   = time.time()

        # Snapshot — never mutated after open
        self.initial_mismatches:    Dict[str, int] = dict(initial_mismatches)
        self.phone_count_at_open:   int            = phone_count

        # Per-phone tracking — mutated throughout the session
        self.staged_phones:          Dict[str, int] = {}
        self.visited_pids:           Set[str]       = set()
        self.resolved_pids:          Set[str]       = set()
        self.declared_missing_pids:  Set[str]       = set()
        self.needs_deposit_pids:     Set[str]       = set()

        # Current phone in hand
        self.in_transit_pid:          Optional[str] = None
        self.in_transit_from_lid:     Optional[int] = None
        self.in_transit_qr_confirmed: bool          = False
        self.current_same_slot:       bool          = False

        logger.info(
            f"[ResolutionSession] {self.session_id} opened — "
            f"{len(self.initial_mismatches)} mismatches, "
            f"phone_count={phone_count}"
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    def has_phone_in_hand(self) -> bool:
        """True if the admin has physically removed a phone and not yet placed it."""
        return self.in_transit_from_lid is not None

    def pending_pids(self) -> list:
        """
        PIDs from the initial mismatch list that are not yet resolved,
        declared missing, or flagged for deposit.
        Also includes staged phones that haven't been placed yet.
        """
        handled = self.resolved_pids | self.declared_missing_pids | self.needs_deposit_pids
        pending = [
            pid for pid in self.initial_mismatches
            if pid not in handled
        ]
        return pending

    def is_expired(self) -> bool:
        return (time.time() - self._opened_at) > SESSION_TIMEOUT

    def elapsed(self) -> float:
        return time.time() - self._opened_at

    def summary(self) -> dict:
        return {
            "session_id":         self.session_id,
            "duration_s":         round(self.elapsed(), 1),
            "resolved":           sorted(self.resolved_pids),
            "declared_missing":   sorted(self.declared_missing_pids),
            "needs_deposit":      sorted(self.needs_deposit_pids),
            "unresolved":         sorted(self.pending_pids()),
        }


# ══════════════════════════════════════════════════════════════════════════════
# AdminSessionContext — singleton session manager
# ══════════════════════════════════════════════════════════════════════════════

class AdminSessionContext:
    """
    Thread-safe manager for the single active ResolutionSession.

    At most one session may be open at a time.
    admin_ops_handler imports the module-level `admin_ctx` singleton.
    """

    def __init__(self):
        self._session: Optional[ResolutionSession] = None

    def is_active(self) -> bool:
        return self._session is not None

    def open(
        self,
        client_id:          str,
        initial_mismatches: Dict[str, int],
        phone_count:        int,
    ) -> ResolutionSession:
        """
        Open a new session.
        Raises RuntimeError if a session is already active.
        """
        if self._session is not None:
            raise RuntimeError(
                f"Session {self._session.session_id} is already active. "
                "Close it before opening a new one."
            )
        self._session = ResolutionSession(
            client_id          = client_id,
            initial_mismatches = initial_mismatches,
            phone_count        = phone_count,
        )
        return self._session

    def get(self) -> Optional[ResolutionSession]:
        """Return the active session, or None if no session is open."""
        return self._session

    def close(self) -> ResolutionSession:
        """
        Close the active session and return it (for summary()).
        Raises RuntimeError if no session is active.
        """
        if self._session is None:
            raise RuntimeError("No active session to close.")
        session = self._session
        self._session = None
        logger.info(f"[AdminSessionContext] Session {session.session_id} closed")
        return session


# ── Module-level singleton ─────────────────────────────────────────────────────
admin_ctx = AdminSessionContext()