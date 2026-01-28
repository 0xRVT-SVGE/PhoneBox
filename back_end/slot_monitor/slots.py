# ============================================================
# FILE: server/slot_monitor/slot.py
# ============================================================
"""
Unified slot abstraction with state, ROI extraction, and monitoring logic.
Each Slot is self-contained with embedded ROI coordinates and baseline.
"""

import time
import logging
import numpy as np
from typing import Optional, Tuple
from slot_embed import compute_embedding, embedding_distance

logger = logging.getLogger(__name__)


class Slot:
    """
    Complete slot abstraction combining state, ROI extraction, and monitoring.

    Each slot is self-contained with:
    - Physical location (ROI coordinates)
    - Visual baseline (embedding)
    - Runtime state (occupancy, mismatch tracking)
    - Monitoring logic (distance thresholds, grace periods)
    """

    def __init__(
            self,
            lid: int,
            roi_coords: Tuple[int, int, int, int],
            baseline_emb: np.ndarray,
            is_occupied: bool = False,
    ):
        """
        Initialize a slot.

        Args:
            lid: Location ID (slot number)
            roi_coords: (x, y, w, h) in camera frame
            baseline_emb: Visual embedding baseline
            is_occupied: Whether slot currently has a phone
        """
        self.lid = lid
        self.roi_coords = roi_coords  # (x, y, w, h) - cached for fast extraction

        # Visual baseline
        self.baseline: np.ndarray = baseline_emb.copy()

        # Occupancy state (from DB, not stored in Slot)
        self.is_occupied: bool = is_occupied

        # Runtime mismatch tracking
        self.mismatch: bool = False
        self.last_dist: float = 0.0

        # Grace period tracking
        self._grace_start_ts: Optional[float] = None

        # Baseline adaptation tracking
        self.distances_history: list[float] = []
        self.max_history = 10

    # ------------------------------------------------------------
    # ROI EXTRACTION
    # ------------------------------------------------------------

    def extract_roi(self, frame: np.ndarray) -> np.ndarray:
        """
        Extract this slot's ROI from a full camera frame.

        Args:
            frame: Full camera frame (BGR)

        Returns:
            ROI image (BGR)

        Raises:
            ValueError: If ROI coordinates are out of bounds
        """
        x, y, w, h = self.roi_coords

        # Validate bounds
        if x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
            raise ValueError(
                f"Slot {self.lid} ROI out of bounds: "
                f"({x},{y},{w},{h}) vs frame {frame.shape}"
            )

        return frame[y:y + h, x:x + w].copy()

    # ------------------------------------------------------------
    # EMBEDDING COMPUTATION
    # ------------------------------------------------------------

    def compute_embedding(self, frame: np.ndarray) -> np.ndarray:
        """
        Compute embedding for this slot from a camera frame.

        Args:
            frame: Full camera frame (BGR)

        Returns:
            Normalized embedding vector
        """
        roi = self.extract_roi(frame)
        return compute_embedding(roi)

    def compute_distance(self, frame: np.ndarray) -> float:
        """
        Compute distance between current frame and baseline.

        Args:
            frame: Full camera frame (BGR)

        Returns:
            Distance value (0 = identical, higher = more different)
        """
        current_emb = self.compute_embedding(frame)
        return embedding_distance(current_emb, self.baseline)

    # ------------------------------------------------------------
    # MONITORING UPDATE
    # ------------------------------------------------------------

    def update(
            self,
            frame: np.ndarray,
            mismatch_threshold: float,
            recalc_threshold: float,
            grace_period: float,
    ) -> dict:
        """
        Complete monitoring update cycle.

        Computes embedding, checks distance, updates state, and returns actions.

        Args:
            frame: Full camera frame
            mismatch_threshold: Distance threshold for alarm
            recalc_threshold: Distance threshold for baseline adaptation
            grace_period: Seconds to wait before triggering alarm

        Returns:
            {
                "trigger_alarm": bool,
                "stop_alarm": bool,
                "needs_recalc": bool,
                "distance": float,
                "embedding": np.ndarray
            }
        """
        # Compute current embedding and distance
        current_emb = self.compute_embedding(frame)
        dist = embedding_distance(current_emb, self.baseline)

        # Update state
        result = self.update_distance(
            dist=dist,
            mismatch_threshold=mismatch_threshold,
            recalc_threshold=recalc_threshold,
            grace_period=grace_period,
        )

        # Include embedding for potential baseline updates
        result["distance"] = dist
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
        Update slot state using pre-computed distance.

        Args:
            dist: Distance between current and baseline
            mismatch_threshold: Distance threshold for alarm
            recalc_threshold: Distance threshold for baseline adaptation
            grace_period: Seconds to wait before triggering alarm

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
        # Start grace period if not already started
        if self._grace_start_ts is None:
            self._grace_start_ts = now

        # Check if grace period has expired
        grace_elapsed = now - self._grace_start_ts

        if grace_elapsed >= grace_period:
            # Grace period expired → trigger alarm
            if not self.mismatch:
                self.mismatch = True
                return {
                    "trigger_alarm": True,
                    "stop_alarm": False,
                    "needs_recalc": False,
                }

        # Still within grace period
        return {
            "trigger_alarm": False,
            "stop_alarm": False,
            "needs_recalc": False,
        }

    # ------------------------------------------------------------
    # BASELINE MANAGEMENT
    # ------------------------------------------------------------

    def reset_baseline(self, new_emb: np.ndarray):
        """
        Hard baseline replacement (deposit / withdrawal / init sync).

        Clears all history and resets state.

        Args:
            new_emb: New baseline embedding
        """
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        self.mismatch = False
        self._grace_start_ts = None
        logger.debug(f"Slot {self.lid} baseline reset (hard)")

    def adapt_baseline(self, new_emb: np.ndarray):
        """
        Soft baseline adaptation (lighting drift, minor changes).

        Preserves occupancy state, only clears distance history.

        Args:
            new_emb: New baseline embedding
        """
        self.baseline = new_emb.copy()
        self.distances_history.clear()
        logger.debug(f"Slot {self.lid} baseline adapted (soft)")

    # ------------------------------------------------------------
    # INTERNAL HELPERS
    # ------------------------------------------------------------

    def _should_recalculate(self, recalc_threshold: float) -> bool:
        """
        Check if baseline should be recalculated.

        Triggers when recent distances show stable drift but no mismatch.

        Args:
            recalc_threshold: Minimum distance for recalculation

        Returns:
            True if baseline should be adapted
        """
        if len(self.distances_history) < 5:
            return False

        recent = self.distances_history[-5:]
        return all(recalc_threshold < d < self.last_dist * 1.5 for d in recent)

    # ------------------------------------------------------------
    # STATUS / DEBUG
    # ------------------------------------------------------------

    def get_status(self) -> dict:
        """
        Get current slot status for monitoring/debugging.

        Returns:
            Dictionary with slot state
        """
        return {
            "lid": self.lid,
            "roi": self.roi_coords,
            "is_occupied": self.is_occupied,
            "mismatch": self.mismatch,
            "last_distance": self.last_dist,
            "distance_history": self.distances_history.copy(),
            "grace_active": self._grace_start_ts is not None,
        }

    def __repr__(self) -> str:
        status = "OCCUPIED" if self.is_occupied else "EMPTY"
        alarm = "⚠️ MISMATCH" if self.mismatch else "✓ OK"
        return (
            f"Slot(lid={self.lid}, {status}, {alarm}, "
            f"dist={self.last_dist:.4f})"
        )


# ------------------------------------------------------------
# HELPER FUNCTIONS
# ------------------------------------------------------------

def generate_grid_rois(
        frame_width: int,
        frame_height: int,
        rows: int,
        cols: int,
        spacing: int = 10,
) -> dict[int, Tuple[int, int, int, int]]:
    """
    Generate ROI coordinates for a grid layout.

    Args:
        frame_width: Camera frame width
        frame_height: Camera frame height
        rows: Number of rows in grid
        cols: Number of columns in grid
        spacing: Pixels to leave as margin between cells

    Returns:
        Dict mapping lid -> (x, y, w, h)
    """
    rois = {}
    cell_h = frame_height // rows
    cell_w = frame_width // cols

    for i in range(rows):
        for j in range(cols):
            x1 = j * cell_w + spacing // 2
            y1 = i * cell_h + spacing // 2
            x2 = (j + 1) * cell_w - spacing // 2
            y2 = (i + 1) * cell_h - spacing // 2

            lid = i * cols + j
            rois[lid] = (x1, y1, x2 - x1, y2 - y1)  # (x, y, w, h)

    return rois