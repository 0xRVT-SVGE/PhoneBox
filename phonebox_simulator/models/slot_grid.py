"""
Slot grid model.

Builds a grid of rectangular slots from a GridConfig, plus a "staging
area" above the grid where phones are created/parked before being
deposited and after being withdrawn.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from PySide6.QtCore import QRectF

from phonebox_simulator.config import GridConfig

# Vertical space reserved above the slot grid for the staging area.
STAGING_HEIGHT = 160


@dataclass
class Slot:
    index: int
    row: int
    col: int
    rect: QRectF
    occupied_pid: Optional[str] = None


class SlotGrid:
    """A rows x cols grid of slots, generated from a GridConfig."""

    def __init__(self, config: GridConfig):
        self.config = config
        self.slots = []
        self.staging_pos: Tuple[float, float] = (0.0, 0.0)
        self._build()

    def _build(self):
        cfg = self.config
        y_offset = STAGING_HEIGHT

        idx = 0
        for r in range(cfg.rows):
            for c in range(cfg.cols):
                x = cfg.margin + c * (cfg.slot_width + cfg.gap)
                y = y_offset + cfg.margin + r * (cfg.slot_height + cfg.gap)
                rect = QRectF(x, y, cfg.slot_width, cfg.slot_height)
                self.slots.append(Slot(idx, r, c, rect))
                idx += 1

        width, _height = self._dims()
        self.staging_pos = (width / 2.0, STAGING_HEIGHT / 2.0)

    def _dims(self) -> Tuple[float, float]:
        cfg = self.config
        width = cfg.margin * 2 + cfg.cols * cfg.slot_width + max(0, cfg.cols - 1) * cfg.gap
        height = (
            STAGING_HEIGHT
            + cfg.margin * 2
            + cfg.rows * cfg.slot_height
            + max(0, cfg.rows - 1) * cfg.gap
        )
        return width, height

    @property
    def scene_size(self) -> Tuple[float, float]:
        return self._dims()

    def get(self, index: int) -> Slot:
        return self.slots[index]

    def __len__(self):
        return len(self.slots)
