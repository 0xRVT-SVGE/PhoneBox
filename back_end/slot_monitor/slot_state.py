# server/slot_monitor/slot_state.py

from enum import Enum, auto
import time


class BinaryState(Enum):
    OK = auto()
    ALTERED = auto()


class TxState(Enum):
    OCCUPIED = auto()
    EMPTY = auto()
    UNKNOWN = auto()


class SlotState:
    def __init__(self, slot_id: str, baseline_emb):
        self.slot_id = slot_id
        self.baseline = baseline_emb

        self.binary_state = BinaryState.OK
        self.tx_state = TxState.UNKNOWN

        self.last_dist = 0.0
        self.last_change_ts = time.time()

    def update(self, dist: float, t_minor: float, t_major: float):
        self.last_dist = dist

        if dist < t_minor:
            self.binary_state = BinaryState.OK
            self.tx_state = TxState.OCCUPIED
        elif dist >= t_major:
            self.binary_state = BinaryState.ALTERED
            self.tx_state = TxState.EMPTY
            self.last_change_ts = time.time()
        else:
            self.tx_state = TxState.UNKNOWN
