import time
import numpy as np
from typing import Optional


class SlotState:
    """
    Runtime-only state for a single slot.
    No DB logic. No enums. Pure state tracking.
    """

    def __init__(
        self,
        lid: int,
        baseline_emb: np.ndarray,
        is_occupied: bool,
    ):
        self.lid = lid

        # Authoritative baseline (from DB, then runtime-adapted)
        self.baseline: np.ndarray = baseline_emb.copy()

        # Runtime-derived occupancy (from SQL, not stored)
        self.is_occupied: bool = is_occupied

        # Runtime mismatch flag
        self.mismatch: bool = False

        # Distance tracking
        self.last_dist: float = 0.0

        # Grace-period handling
        self._grace_start_ts: Optional[float] = None

        # Baseline adaptation tracking
        self.distances_history: list[float] = []
        self.max_history = 10

    # ------------------------------------------------------------
    # MAIN UPDATE (called by monitoring thread)
    # ------------------------------------------------------------

    def update_distance(
        self,
        dist: float,
        mismatch_threshold: float,
        recalc_threshold: float,
        grace_period: float,
    ) -> dict:
        """
        Update slot runtime state using ONLY embedding distance.

        Returns:
            {
                "trigger_alarm": bool,
                "stop_alarm": bool,
                "needs_recalc": bool,
            }
        """

        self.last_dist = dist
        self.distances_history.append(dist)
        if len(self.distances_history) > self.max_history:
            self.distances_history.pop(0)

        now = time.time()

        # ----------------------------
        # CASE 1: NORMAL / STABLE
        # ----------------------------
        if dist < mismatch_threshold:
            stop_alarm = self.mismatch
            self.mismatch = False
            self._grace_start_ts = None

            needs_recalc = self._should_recalculate(recalc_threshold)

            return {
                "trigger_alarm": False,
                "stop_alarm": stop_alarm,
                "needs_recalc": needs_recalc,
            }

        # ----------------------------
        # CASE 2: SUSPICIOUS
        # ----------------------------
        if self._grace_start_ts is None:
            self._grace_start_ts = now
            return {
                "trigger_alarm": False,
                "stop_alarm": False,
                "needs_recalc": False,
            }

        # Grace expired → alarm condition
        if now - self._grace_start_ts >= grace_period:
            if not self.mismatch:
                self.mismatch = True
                return {
                    "trigger_alarm": True,
                    "stop_alarm": False,
                    "needs_recalc": False,
                }

        return {
            "trigger_alarm": False,
            "stop_alarm": False,
            "needs_recalc": False,
        }

    # ------------------------------------------------------------
    # BASELINE MANAGEMENT
    # ------------------------------------------------------------

    def reset_baseline(self, new_emb: np.ndarray):
        """Hard baseline replacement (deposit / withdrawal / init sync)."""
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        self.mismatch = False
        self._grace_start_ts = None

    def adapt_baseline(self, new_emb: np.ndarray):
        """Soft adaptation (lighting drift)."""
        self.baseline = new_emb.copy()
        self.distances_history.clear()

    # ------------------------------------------------------------
    # INTERNAL
    # ------------------------------------------------------------

    def _should_recalculate(self, recalc_threshold: float) -> bool:
        """
        Recalculate baseline if recent distances show stable drift
        but no mismatch.
        """
        if len(self.distances_history) < 5:
            return False

        recent = self.distances_history[-5:]
        return all(recalc_threshold < d for d in recent)