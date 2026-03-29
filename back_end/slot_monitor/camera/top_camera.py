# ============================================================
# FILE: back_end/slot_monitor/camera/top_camera.py
# ============================================================
"""
Shared state for the top camera (index 2).

Changes from original:
  - _tracker_overlay: Optional callable set by PhoneTracker.
    Called on every frame after ROI drawing but before frame storage,
    so the phone bounding box and QR status appear on the WebRTC stream.
  - set_tracker_overlay(fn) / clear_tracker_overlay() — thread-safe
    assignment; reading the attribute in _run() is safe under CPython GIL.
"""

import cv2
import logging
import threading
import time
from typing import Optional, Callable

import numpy as np

logger = logging.getLogger(__name__)

CAMERA_INDEX    = 2
_RECONNECT_DELAY = 1.0


class TopCamera:

    def __init__(self):
        self._lock             = threading.Lock()
        self._frame:           Optional[np.ndarray] = None
        self._frame_event      = threading.Event()
        self._running          = False
        self._thread:          Optional[threading.Thread] = None
        self._rois:            list = []

        # Tracker overlay: callable(frame: np.ndarray) -> None
        # Set by PhoneTracker, cleared when tracker finishes.
        # Assigned atomically (CPython GIL) so no lock needed for reads.
        self._tracker_overlay: Optional[Callable[[np.ndarray], None]] = None

    # ── Lifecycle ─────────────────────────────────────────

    def start(self):
        """Start the capture thread. Idempotent."""
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name="TopCameraCapture"
        )
        self._thread.start()
        logger.info(f"[TopCamera] Started on camera index {CAMERA_INDEX}")

        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.start()
        except Exception as e:
            logger.warning(f"[TopCamera] Could not start top_rolling_buffer: {e}")

    def stop(self):
        """Stop the capture thread and rolling buffer."""
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("[TopCamera] Stopped")

        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.stop()
        except Exception as e:
            logger.warning(f"[TopCamera] Could not stop top_rolling_buffer: {e}")

    # ── ROI configuration ─────────────────────────────────

    def set_rois(self, rois: list):
        with self._lock:
            self._rois = list(rois)
        logger.info(f"[TopCamera] ROIs updated: {rois}")

    # ── Tracker overlay ───────────────────────────────────

    def set_tracker_overlay(self, fn: Callable[[np.ndarray], None]) -> None:
        """
        Register a callable that draws on each frame before storage.
        Called from PhoneTracker to inject its bounding-box annotations.
        Assignment is atomic under CPython GIL — no lock required.
        """
        self._tracker_overlay = fn

    def clear_tracker_overlay(self) -> None:
        """Remove the tracker overlay. Called when tracking ends."""
        self._tracker_overlay = None

    # ── Frame access ──────────────────────────────────────

    def get_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def wait_for_frame(self, timeout: float = 0.1) -> bool:
        return self._frame_event.wait(timeout=timeout)

    def clear_frame_event(self):
        self._frame_event.clear()

    # ── Capture thread ────────────────────────────────────

    def _run(self):
        cap: Optional[cv2.VideoCapture] = None

        while self._running:
            # Ensure camera is open
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(CAMERA_INDEX)
                if not cap.isOpened():
                    logger.warning(
                        f"[TopCamera] Cannot open camera {CAMERA_INDEX}, "
                        f"retrying in {_RECONNECT_DELAY}s"
                    )
                    cap = None
                    time.sleep(_RECONNECT_DELAY)
                    continue
                logger.debug(f"[TopCamera] Camera {CAMERA_INDEX} opened")

            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("[TopCamera] Frame read failed")
                cap.release()
                cap = None
                time.sleep(0.1)
                continue

            # Draw admin session ROI overlays
            with self._lock:
                rois = list(self._rois)

            for i, (x, y, w, h) in enumerate(rois):
                color = (0, 255, 0) if i == 0 else (0, 200, 255)
                label = "STAGING 1" if i == 0 else "STAGING 2"
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                cv2.putText(
                    frame, label, (x, max(y - 6, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA,
                )

            # Tracker overlay — drawn AFTER ROI boxes, BEFORE storage
            # Reading _tracker_overlay is safe without a lock (CPython GIL)
            fn = self._tracker_overlay
            if fn is not None:
                try:
                    fn(frame)
                except Exception as e:
                    logger.warning(f"[TopCamera] Tracker overlay error: {e}")

            # Store annotated frame
            with self._lock:
                self._frame = frame
            self._frame_event.set()

        if cap is not None:
            cap.release()
            logger.debug("[TopCamera] Camera released on stop")


# Global singleton
top_camera = TopCamera()