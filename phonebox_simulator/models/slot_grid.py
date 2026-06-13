"""
Slot grid model.

The phone enters from the RIGHT side of the scene, travels left through
a dedicated transit lane above the slot grid, then drops straight down
into the target slot.

Layout (Y increases downward):
  ┌─────────────────────────────────────────┐  ← y = 0
  │  [STAGING]  transit lane                │  ← phone waits here before deposit
  ├─────────────────────────────────────────┤  ← y = TRANSIT_HEIGHT
  │                                         │
  │   slot grid  (rows × cols)              │
  │                                         │
  └─────────────────────────────────────────┘

staging_pos : left side of the transit lane, where freshly created phones wait.
entry_pos   : right-edge centre of the transit lane, where deposit animation starts.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from PySide6.QtCore import QRectF

from phonebox_simulator.config import GridConfig

# Vertical height of the transit lane above the slot grid.
TRANSIT_HEIGHT = 100


@dataclass
class Slot:
    index: int
    row: int
    col: int
    rect: QRectF
    occupied_pid: Optional[str] = None


class SlotGrid:
    """A rows × cols grid of slots generated from a GridConfig."""

    def __init__(self, config: GridConfig):
        self.config = config
        self.slots: list[Slot] = []
        self._build()

    # ------------------------------------------------------------------
    def _build(self):
        cfg = self.config
        idx = 0
        for r in range(cfg.rows):
            for c in range(cfg.cols):
                x = cfg.margin + c * (cfg.slot_width + cfg.gap)
                y = TRANSIT_HEIGHT + cfg.margin + r * (cfg.slot_height + cfg.gap)
                rect = QRectF(x, y, cfg.slot_width, cfg.slot_height)
                self.slots.append(Slot(idx, r, c, rect))
                idx += 1

    # ------------------------------------------------------------------
    def _dims(self) -> Tuple[float, float]:
        cfg = self.config
        w = cfg.margin * 2 + cfg.cols * cfg.slot_width + max(0, cfg.cols - 1) * cfg.gap
        h = (
            TRANSIT_HEIGHT
            + cfg.margin * 2
            + cfg.rows * cfg.slot_height
            + max(0, cfg.rows - 1) * cfg.gap
        )
        return w, h

    @property
    def scene_size(self) -> Tuple[float, float]:
        return self._dims()

    @property
    def entry_pos(self) -> Tuple[float, float]:
        """Right-edge of the transit lane — where the deposit animation begins."""
        w, _ = self._dims()
        return (w + 80.0, TRANSIT_HEIGHT / 2.0)

    @property
    def staging_pos(self) -> Tuple[float, float]:
        """
        Left side of the transit lane — where a newly created phone waits
        visibly before the operator clicks Deposit.  The phone parks here
        so the top camera can confirm QR readability before insertion.
        """
        cfg = self.config
        # Park at the left margin, centred vertically in the transit lane.
        return (cfg.margin + 80.0, TRANSIT_HEIGHT / 2.0)

    @property
    def transit_y(self) -> float:
        """Y coordinate of the transit lane centre."""
        return TRANSIT_HEIGHT / 2.0

    def get(self, index: int) -> Slot:
        return self.slots[index]

    def slot_label(self, index: int) -> str:
        """Human-readable label matching the on-screen grid annotation."""
        s = self.slots[index]
        return f"R{s.row + 1}C{s.col + 1}"

    def __len__(self):
        return len(self.slots)
