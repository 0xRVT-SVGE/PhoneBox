# ============================================================
# FILE: server/slot_monitor/slot_state.py
# ============================================================

from enum import Enum, auto
import time
import numpy as np


class BinaryState(Enum):
    OK = auto()
    ALTERED = auto()


class TxState(Enum):
    OCCUPIED = auto()
    EMPTY = auto()
    UNKNOWN = auto()


class SlotState:
    """Track state of a single storage slot"""

    def __init__(self, lid: int, baseline_emb: np.ndarray):
        self.lid = lid
        self.baseline = baseline_emb.copy()

        self.binary_state = BinaryState.OK
        self.tx_state = TxState.UNKNOWN

        self.last_dist = 0.0
        self.last_change_ts = time.time()
        self.unknown_since = None  # Track when UNKNOWN state started

        # For adaptive baseline updates
        self.distances_history = []  # Track recent distances
        self.max_history = 10  # Keep last 10 measurements

    def update(self, dist: float, t_minor: float, t_major: float, t_recalc: float = 0.12):
        """
        Update slot state based on distance.

        Args:
            dist: Current embedding distance
            t_minor: Threshold for OK state (e.g., 0.15)
            t_major: Threshold for ALTERED/EMPTY state (e.g., 0.35)
            t_recalc: Threshold for triggering recalculation (e.g., 0.12)

        Returns:
            dict with 'state_changed' and 'needs_recalc' flags
        """
        old_binary = self.binary_state
        old_tx = self.tx_state

        self.last_dist = dist
        self.distances_history.append(dist)
        if len(self.distances_history) > self.max_history:
            self.distances_history.pop(0)

        # State determination
        if dist < t_minor:
            self.binary_state = BinaryState.OK
            self.tx_state = TxState.OCCUPIED
            self.unknown_since = None

        elif dist >= t_major:
            self.binary_state = BinaryState.ALTERED
            self.tx_state = TxState.EMPTY
            self.unknown_since = None
            if old_binary != BinaryState.ALTERED:
                self.last_change_ts = time.time()

        else:  # Between t_minor and t_major - UNKNOWN zone
            self.tx_state = TxState.UNKNOWN
            if self.unknown_since is None:
                self.unknown_since = time.time()

        # Check if baseline recalculation needed
        # Gradual changes (lighting, someone standing in front) that are consistent
        needs_recalc = False
        if len(self.distances_history) >= 5:
            # If last 5 readings are all above recalc threshold but below major
            recent_5 = self.distances_history[-5:]
            if all(t_recalc < d < t_major for d in recent_5):
                needs_recalc = True
                logger.info(f"Slot {self.lid}: Baseline recalculation needed (consistent elevated distance)")

        state_changed = (old_binary != self.binary_state or old_tx != self.tx_state)

        return {
            'state_changed': state_changed,
            'needs_recalc': needs_recalc
        }

    def should_alarm(self, grace_period: float) -> bool:
        """
        Check if alarm should trigger.

        Args:
            grace_period: Seconds to wait before alarming on UNKNOWN state
        """
        # Immediate alarm for ALTERED state
        if self.binary_state == BinaryState.ALTERED:
            return True

        # Alarm if UNKNOWN for too long (e.g., hand blocking camera)
        if self.tx_state == TxState.UNKNOWN and self.unknown_since:
            if time.time() - self.unknown_since > grace_period:
                return True

        return False

    def reset_baseline(self, new_emb: np.ndarray):
        """
        Completely replace baseline (used after deposit/withdrawal operations).

        Args:
            new_emb: New embedding to set as baseline
        """
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        logger.info(f"Slot {self.lid}: Baseline reset")
