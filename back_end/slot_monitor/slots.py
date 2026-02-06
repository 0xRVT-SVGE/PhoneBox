# ============================================================
# FILE: server/slot_monitor/slots.py
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

import cv2
import json
import os
from typing import Dict, Tuple, Optional

ROI_FILE = "rois_saved.json"


def generate_grid_rois(
    frame_width: int,
    frame_height: int,
    rows: int,
    cols: int,
    spacing: int = 0,
    num_lids: Optional[int] = None,
    frame: Optional[any] = None
) -> Dict[int, Tuple[int, int, int, int]]:

    def default_grid_rois(n_lids: int) -> list[list[int]]:
        rois = []
        total_spacing_x = spacing * (cols + 1)
        total_spacing_y = spacing * (rows + 1)
        cell_w = (frame_width - total_spacing_x) // cols
        cell_h = (frame_height - total_spacing_y) // rows

        lid = 0
        for r in range(rows):
            for c in range(cols):
                if lid >= n_lids:
                    return rois
                x = spacing + c * (cell_w + spacing)
                y = spacing + r * (cell_h + spacing)
                rois.append([x, y, cell_w, cell_h])
                lid += 1
        return rois

    if num_lids is None:
        num_lids = rows * cols

    # ---- Load or initialize ROIs ----
    if os.path.exists(ROI_FILE):
        try:
            with open(ROI_FILE, "r") as f:
                rois_list = json.load(f)

            if len(rois_list) != num_lids:
                print("Saved ROI count mismatch, regenerating grid")
                rois_list = default_grid_rois(num_lids)

        except Exception:
            rois_list = default_grid_rois(num_lids)
    else:
        rois_list = default_grid_rois(num_lids)

    # ---- Visualization setup ----
    if frame is None:
        frame = np.full((frame_height, frame_width, 3), 255, dtype=np.uint8)

    current = 0
    dragging = None
    RESIZE_MARGIN = 10

    def redraw():
        temp = frame.copy()
        for i, (x, y, w, h) in enumerate(rois_list):
            color = (0, 0, 255) if i == current else (0, 255, 0)
            cv2.rectangle(temp, (x, y), (x + w, y + h), color, 2)
            cv2.putText(temp, f"LID {i}", (x, y - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        cv2.imshow("ROI Editor", temp)

    def mouse_cb(event, mx, my, *_):
        nonlocal dragging
        x, y, w, h = rois_list[current]
        near_br = (x + w - RESIZE_MARGIN <= mx <= x + w and
                   y + h - RESIZE_MARGIN <= my <= y + h)

        if event == cv2.EVENT_LBUTTONDOWN:
            if near_br:
                dragging = ("resize", mx, my)
            elif x <= mx <= x + w and y <= my <= y + h:
                dragging = ("move", mx - x, my - y)

        elif event == cv2.EVENT_MOUSEMOVE and dragging:
            if dragging[0] == "move":
                dx, dy = dragging[1:]
                rois_list[current][0] = max(0, min(mx - dx, frame_width - w))
                rois_list[current][1] = max(0, min(my - dy, frame_height - h))
            else:
                rois_list[current][2] = max(10, min(mx - x, frame_width - x))
                rois_list[current][3] = max(10, min(my - y, frame_height - y))
            redraw()

        elif event == cv2.EVENT_LBUTTONUP:
            dragging = None

    cv2.namedWindow("ROI Editor")
    cv2.setMouseCallback("ROI Editor", mouse_cb)
    redraw()

    while True:
        key = cv2.waitKey(1) & 0xFF

        if key in (ord('x'), ord('X')):
            break
        elif key in (ord('e'), ord('E')):
            current = (current + 1) % num_lids
            redraw()
        elif key in (ord('a'), ord('A')):
            current = (current - 1) % num_lids
            redraw()
        elif key in (ord('r'), ord('R')):
            rois_list = default_grid_rois(num_lids)
            current = 0
            redraw()

    cv2.destroyWindow("ROI Editor")

    if len(rois_list) != num_lids:
        raise RuntimeError(
            f"ROI count mismatch: expected {num_lids}, got {len(rois_list)}"
        )

    with open(ROI_FILE, "w") as f:
        json.dump(rois_list, f, indent=2)

    return {i: tuple(r) for i, r in enumerate(rois_list)}

