# ============================================================
# FILE: back_end/slot_monitor/slots.py
# ============================================================
"""
Unified slot abstraction — ROI loading reads from rois_bottom.json
(written by the calibration tool).
"""

import json
import os
import time
import logging
import numpy as np
from collections import deque
from typing import Optional, Tuple, Dict, List
from Backup.back_end.slot_monitor.slot_embed import compute_embedding, embedding_distance
from Backup.back_end.config import SlotMonitorConfig as _SMC

logger = logging.getLogger(__name__)

_TOOLS_DIR      = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tools"
)

# ROI file is named per box so multiple server processes on the same machine
# each have their own calibration file.  E.g. rois_bottom_year_1.json.
# Falls back to the legacy name when BOX_SLUG is not set (single-box deploy).
def _roi_file_bottom() -> str:
    from Backup.back_end.config import ServerConfig as _SVC
    slug = getattr(_SVC, "BOX_SLUG", None) or "box_1"
    return os.path.join(_TOOLS_DIR, f"rois_bottom_{slug}.json")

ROI_FILE_BOTTOM = _roi_file_bottom()


_CY_UD_AVAILABLE = False
try:
    from back_end.slot_monitor.slots_cy import cy_update_distance as _cy_ud
    _CY_UD_AVAILABLE = True
    logger.info(
        "[Slots] Cython cy_update_distance loaded — using compiled state machine"
    )
except ImportError:
    logger.debug(
        "[Slots] slots_cy not found — using pure-Python update_distance."
    )

_RECALC_MIN_SAMPLES = None

def _get_recalc_min_samples():
    global _RECALC_MIN_SAMPLES
    if _RECALC_MIN_SAMPLES is None:
        try:
            from Backup.back_end.config import SlotMonitorConfig as _SMC
            _RECALC_MIN_SAMPLES = _SMC.RECALC_MIN_SAMPLES
        except Exception:
            _RECALC_MIN_SAMPLES = 5
    return _RECALC_MIN_SAMPLES

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

        self._grace_start_ts: Optional[float] = None

        # maxlen from config — O(1) append+evict
        self.distances_history: deque = deque(maxlen=_SMC.DISTANCE_HISTORY_MAXLEN)

    # ── Pre-flag (startup mismatches) ─────────────────────

    def pre_flag_mismatch(self):
        """
        Mark this slot as already-mismatched before workers start.
        Grace period is considered expired on the very first update() call.
        """
        self.mismatch        = False
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
        """
        Run the distance → alarm state machine for one frame.

        Delegates to the compiled Cython cy_update_distance() when available
        (~7× faster), otherwise falls back to equivalent pure-Python logic.
        """
        if _CY_UD_AVAILABLE:
            # ── Cython fast path ─────────────────────────────────────────────────
            result = _cy_ud(
                dist=dist,
                mismatch_threshold=mismatch_threshold,
                recalc_threshold=recalc_threshold,
                grace_period=grace_period,
                last_dist=self.last_dist,
                current_mismatch=self.mismatch,
                grace_start_ts=self._grace_start_ts,
                distances_history=self.distances_history,
                recalc_min_samples=_get_recalc_min_samples(),
            )
            self.last_dist = result["new_last_dist"]
            self.mismatch = result["new_mismatch"]
            self._grace_start_ts = result["new_grace_ts"]
            return {
                "trigger_alarm": result["trigger_alarm"],
                "stop_alarm": result["stop_alarm"],
                "needs_recalc": result["needs_recalc"],
            }

        # ── Pure-Python fallback ─────────────────────────────────────────────────
        self.last_dist = dist
        self.distances_history.append(dist)
        now = time.time()

        # ── CASE 1: NORMAL ─────────────────────────────
        if dist < mismatch_threshold:
            stop_alarm           = self.mismatch
            self.mismatch        = False
            self._grace_start_ts = None
            return {
                "trigger_alarm": False,
                "stop_alarm":    stop_alarm,
                "needs_recalc":  self._should_recalculate(recalc_threshold),
            }

        # ── CASE 2: SUSPICIOUS ─────────────────────────
        if self._grace_start_ts is None:
            self._grace_start_ts = now

        if now - self._grace_start_ts >= grace_period:
            if not self.mismatch:
                self.mismatch = True
                return {"trigger_alarm": True, "stop_alarm": False, "needs_recalc": False}

        return {"trigger_alarm": False, "stop_alarm": False, "needs_recalc": False}

    # ── Baseline management ───────────────────────────────

    def reset_baseline(self, new_emb: np.ndarray):
        self.baseline        = new_emb.copy()
        self.distances_history.clear()
        self.mismatch        = False
        self._grace_start_ts = None
        logger.debug(f"Slot {self.lid} baseline reset (hard)")

    def adapt_baseline(self, new_emb: np.ndarray):
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        logger.debug(f"Slot {self.lid} baseline adapted (soft)")

    def _should_recalculate(self, recalc_threshold: float) -> bool:
        n = _SMC.RECALC_MIN_SAMPLES
        if len(self.distances_history) < n:
            return False
        recent = list(self.distances_history)[-n:]
        return all(recalc_threshold < d < self.last_dist * 1.5 for d in recent)

    def get_status(self) -> dict:
        return {
            "lid":              self.lid,
            "roi":              self.roi_coords,
            "is_occupied":      self.is_occupied,
            "mismatch":         self.mismatch,
            "last_distance":    self.last_dist,
            "distance_history": list(self.distances_history),
            "grace_active":     self._grace_start_ts is not None,
        }

    def __repr__(self) -> str:
        return (
            f"Slot(lid={self.lid}, "
            f"{'OCCUPIED' if self.is_occupied else 'EMPTY'}, "
            f"{'MISMATCH' if self.mismatch else 'OK'}, "
            f"dist={self.last_dist:.4f})"
        )


# B10: cache keyed by (num_lids, file_mtime) — avoids re-reading JSON on every
# call. mtime key ensures the cache busts automatically after recalibration.
_rois_cache: dict = {}


def generate_grid_rois(
    frame_width:  int,
    frame_height: int,
    rows:         int,
    cols:         int,
    spacing:      int = 0,
    num_lids:     Optional[int] = None,
    frame:        Optional[np.ndarray] = None,
    lid_list:     Optional[List[int]] = None,
) -> Dict[int, Tuple[int, int, int, int]]:
    """
    Load bottom-camera ROIs from the box-specific rois_bottom_{slug}.json.
    Falls back to an auto-generated equal-area grid if the file is missing
    or has the wrong number of entries.

    lid_list: ordered list of actual DB lid values for this box.
              When provided, ROI dict keys are real lid values (e.g. 30-59
              for Box 2) instead of enumeration indices.  Pass the result
              of AsyncSlotMonitorDB.get_box_lids().
    """
    if num_lids is None:
        num_lids = len(lid_list) if lid_list else rows * cols

    # B10: fast path — return cached result if the JSON file hasn't changed.
    # Cache key includes the first lid so two boxes with the same slot count
    # but different lid ranges don't collide.
    first_lid = lid_list[0] if lid_list else 0
    try:
        mtime = os.path.getmtime(ROI_FILE_BOTTOM) if os.path.exists(ROI_FILE_BOTTOM) else 0.0
    except OSError:
        mtime = 0.0
    cache_key = (num_lids, first_lid, mtime)
    if cache_key in _rois_cache:
        return _rois_cache[cache_key]

    def _default() -> list:
        rois      = []
        total_sx  = spacing * (cols + 1)
        total_sy  = spacing * (rows + 1)
        cw        = (frame_width  - total_sx) // cols
        ch        = (frame_height - total_sy) // rows
        lid       = 0
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
                    f"[Slots] {ROI_FILE_BOTTOM} has "
                    f"{len(data) if isinstance(data, list) else '?'} entries "
                    f"but expected {num_lids}. Using default grid."
                )
        except Exception as e:
            logger.warning(
                f"[Slots] Failed to read {ROI_FILE_BOTTOM}: {e}. Using default grid."
            )

    if rois_list is None:
        rois_list = _default()
        logger.info(f"[Slots] Using auto-generated default grid ({num_lids} lids).")

    if len(rois_list) != num_lids:
        raise RuntimeError(
            f"ROI count mismatch: expected {num_lids}, got {len(rois_list)}"
        )

    # Map ROI entries to actual lid values.
    # lid_list MUST be provided for any box whose lids don't start at 0.
    if lid_list and len(lid_list) == len(rois_list):
        result = {lid: tuple(r) for lid, r in zip(lid_list, rois_list)}
    else:
        # Backward-compatible fallback (single-box / Box 1 where lid starts at 0)
        result = {i: tuple(r) for i, r in enumerate(rois_list)}
    _rois_cache[cache_key] = result
    return result