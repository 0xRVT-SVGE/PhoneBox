# ============================================================
# FILE: back_end/slot_monitor/slots.py  (generate_grid_rois section only)
# ============================================================
"""
Unified slot abstraction — ROI loading now reads from rois_bottom.json
(written by the calibration tool) instead of the old rois_saved.json.
"""

import cv2
import json
import os
import time
import logging
import numpy as np
from typing import Optional, Tuple, Dict
from back_end.slot_monitor.slot_embed import compute_embedding, embedding_distance

logger = logging.getLogger(__name__)

# Path to the bottom-camera ROI file written by roi_calibration.py
_TOOLS_DIR      = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tools"
)
ROI_FILE_BOTTOM = os.path.join(_TOOLS_DIR, "rois_bottom.json")


class Slot:
    """
    Complete slot abstraction combining state, ROI extraction, and monitoring.
    """

    def __init__(
            self,
            lid: int,
            roi_coords: Tuple[int, int, int, int],
            baseline_emb: np.ndarray,
            is_occupied: bool = False,
    ):
        self.lid         = lid
        self.roi_coords  = roi_coords
        self.baseline    = baseline_emb.copy()
        self.is_occupied = is_occupied

        self.mismatch  = False
        self.last_dist = 0.0

        # Grace period. Pre-set to far past by pre_flag_mismatch() for slots
        # that are already wrong at startup — fires alarm on first frame.
        self._grace_start_ts: Optional[float] = None

        self.distances_history: list[float] = []
        self.max_history = 10

    # ── Pre-flag (startup mismatches) ─────────────────────

    def pre_flag_mismatch(self):
        """
        Mark this slot as already-mismatched before workers start.
        Grace period is considered expired on the very first update() call.
        """
        self.mismatch = False
        self._grace_start_ts = time.time() - 1000.0   # always expired

    # ── ROI extraction ────────────────────────────────────

    def extract_roi(self, frame: np.ndarray) -> np.ndarray:
        x, y, w, h = self.roi_coords
        if x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
            raise ValueError(
                f"Slot {self.lid} ROI out of bounds: "
                f"({x},{y},{w},{h}) vs frame {frame.shape}"
            )
        return frame[y:y + h, x:x + w].copy()

    def compute_embedding(self, frame: np.ndarray) -> np.ndarray:
        return compute_embedding(self.extract_roi(frame))

    def compute_distance(self, frame: np.ndarray) -> float:
        return embedding_distance(self.compute_embedding(frame), self.baseline)

    # ── Monitoring update ─────────────────────────────────

    def update(
            self,
            frame: np.ndarray,
            mismatch_threshold: float,
            recalc_threshold: float,
            grace_period: float,
    ) -> dict:
        current_emb = self.compute_embedding(frame)
        dist = embedding_distance(current_emb, self.baseline)
        result = self.update_distance(dist, mismatch_threshold,
                                      recalc_threshold, grace_period)
        result["distance"]  = dist
        result["embedding"] = current_emb
        return result

    def update_distance(
            self,
            dist: float,
            mismatch_threshold: float,
            recalc_threshold: float,
            grace_period: float,
    ) -> dict:
        self.last_dist = dist
        self.distances_history.append(dist)
        if len(self.distances_history) > self.max_history:
            self.distances_history.pop(0)

        now = time.time()

        # ── CASE 1: NORMAL ─────────────────────────────
        if dist < mismatch_threshold:
            stop_alarm = self.mismatch
            self.mismatch         = False
            self._grace_start_ts  = None
            return {
                "trigger_alarm": False,
                "stop_alarm":    stop_alarm,
                "needs_recalc":  self._should_recalculate(recalc_threshold),
            }

        # ── CASE 2: SUSPICIOUS ─────────────────────────
        # If _grace_start_ts was pre-set 1000 s in the past by
        # pre_flag_mismatch(), grace_elapsed is immediately >= grace_period
        # and the alarm fires on the very first frame.
        if self._grace_start_ts is None:
            self._grace_start_ts = now

        if now - self._grace_start_ts >= grace_period:
            if not self.mismatch:
                self.mismatch = True
                return {"trigger_alarm": True, "stop_alarm": False, "needs_recalc": False}

        return {"trigger_alarm": False, "stop_alarm": False, "needs_recalc": False}

    # ── Baseline management ───────────────────────────────

    def reset_baseline(self, new_emb: np.ndarray):
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        self.mismatch        = False
        self._grace_start_ts = None
        logger.debug(f"Slot {self.lid} baseline reset (hard)")

    def adapt_baseline(self, new_emb: np.ndarray):
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        logger.debug(f"Slot {self.lid} baseline adapted (soft)")

    def _should_recalculate(self, recalc_threshold: float) -> bool:
        if len(self.distances_history) < 5:
            return False
        recent = self.distances_history[-5:]
        return all(recalc_threshold < d < self.last_dist * 1.5 for d in recent)

    def get_status(self) -> dict:
        return {
            "lid":              self.lid,
            "roi":              self.roi_coords,
            "is_occupied":      self.is_occupied,
            "mismatch":         self.mismatch,
            "last_distance":    self.last_dist,
            "distance_history": self.distances_history.copy(),
            "grace_active":     self._grace_start_ts is not None,
        }

    def __repr__(self) -> str:
        return (
            f"Slot(lid={self.lid}, "
            f"{'OCCUPIED' if self.is_occupied else 'EMPTY'}, "
            f"{'⚠️ MISMATCH' if self.mismatch else '✓ OK'}, "
            f"dist={self.last_dist:.4f})"
        )


# ══════════════════════════════════════════════════════════
# generate_grid_rois — loads from rois_bottom.json
# ══════════════════════════════════════════════════════════

def generate_grid_rois(
    frame_width:  int,
    frame_height: int,
    rows:         int,
    cols:         int,
    spacing:      int = 0,
    num_lids:     Optional[int] = None,
    frame:        Optional[np.ndarray] = None,
) -> Dict[int, Tuple[int, int, int, int]]:
    """
    Load bottom-camera ROIs from rois_bottom.json (written by the calibration
    tool at startup).  Falls back to an auto-generated equal-area grid if the
    file is missing or has the wrong number of entries.

    The interactive ROI editor that used to live here has been moved to
    back_end/slot_monitor/tools/roi_calibration.py which runs at startup
    before the slot monitor is created.
    """
    if num_lids is None:
        num_lids = rows * cols

    def _default() -> list:
        rois = []
        total_sx = spacing * (cols + 1)
        total_sy = spacing * (rows + 1)
        cw = (frame_width  - total_sx) // cols
        ch = (frame_height - total_sy) // rows
        lid = 0
        for r in range(rows):
            for c in range(cols):
                if lid >= num_lids:
                    break
                rois.append([
                    spacing + c * (cw + spacing),
                    spacing + r * (ch + spacing),
                    cw, ch,
                ])
                lid += 1
        return rois

    # Try to load the calibrated file first
    rois_list = None
    if os.path.exists(ROI_FILE_BOTTOM):
        try:
            with open(ROI_FILE_BOTTOM, "r") as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) == num_lids:
                rois_list = [[int(v) for v in r] for r in data]
                logger.info(
                    f"[Slots] Loaded {len(rois_list)} ROIs from {ROI_FILE_BOTTOM}"
                )
            else:
                logger.warning(
                    f"[Slots] {ROI_FILE_BOTTOM} has {len(data) if isinstance(data, list) else '?'} "
                    f"entries but expected {num_lids}. Using default grid."
                )
        except Exception as e:
            logger.warning(f"[Slots] Failed to read {ROI_FILE_BOTTOM}: {e}. Using default grid.")

    if rois_list is None:
        rois_list = _default()
        logger.info(f"[Slots] Using auto-generated default grid ({num_lids} lids).")

    if len(rois_list) != num_lids:
        raise RuntimeError(
            f"ROI count mismatch: expected {num_lids}, got {len(rois_list)}"
        )

    return {i: tuple(r) for i, r in enumerate(rois_list)}