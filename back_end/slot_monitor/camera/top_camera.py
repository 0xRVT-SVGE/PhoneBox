# ============================================================
# FILE: back_end/slot_monitor/camera/top_camera.py
# ============================================================
"""
Shared state for the top camera (index 2).

One background thread owns cv2.VideoCapture(2) and writes annotated
frames into a shared buffer. All consumers read from the buffer:

  - AdminVideoTrack (WebRTC) — continuous stream during admin session
  - scan_and_validate_pid_from_buffer — QR decoding without opening camera
  - EvidenceRecorder — single frame capture per event

Because everyone reads from the same buffer, there is no contention
and the capture thread never needs to be paused or stopped mid-session.

Lifecycle (lazy):
    top_camera is NOT started at server startup.
    It is started by the first caller that needs it:
      - ops_handler.handle_qr_scanned()
      - admin_ops_handler.handle_session_start()
    Both call top_camera.start() which is idempotent.
    server_main calls top_camera.stop() in shutdown (no-op if never started).

    top_rolling_buffer is co-started with top_camera: the first call to
    top_camera.start() also starts top_rolling_buffer so evidence clips
    always have top-camera pre-action footage available.

ROI overlay:
    Staging zone rectangles are drawn on frames before storing in the buffer.
    All consumers receive pre-annotated frames.
    set_rois([]) clears overlays when the admin session closes.

Usage:
    top_camera.start()          # idempotent, lazy — also starts rolling buffer
    top_camera.set_rois([...])  # on admin session open
    top_camera.set_rois([])     # on admin session close
    top_camera.stop()           # on server shutdown — also stops rolling buffer
"""

import cv2
import logging
import threading
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

CAMERA_INDEX = 2
_RECONNECT_DELAY = 1.0


class TopCamera:

    def __init__(self):
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._frame_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._rois: list = []

    # ── Lifecycle ─────────────────────────────────────

    def start(self):
        """
        Start the capture thread. Idempotent — safe to call multiple times.

        Also starts top_rolling_buffer on the first call so alarm clips
        and admin evidence clips always have pre-action top-camera footage.
        """
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="TopCameraCapture"
        )
        self._thread.start()
        logger.info(f"[TopCamera] Started on camera index {CAMERA_INDEX}")

        # Co-start the rolling buffer so top-cam footage is always available
        # for alarm clips and admin evidence.  start() is idempotent on the
        # buffer side too, so repeated calls to top_camera.start() are safe.
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.start()
        except Exception as e:
            logger.warning(
                f"[TopCamera] Could not start top_rolling_buffer: {e}. "
                "Top-cam alarm clips will be empty."
            )

    def stop(self):
        """Stop the capture thread and the rolling buffer. No-op if never started."""
        if not self._running:
            return
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("[TopCamera] Stopped")

        # Stop the rolling buffer when the camera stops.
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.stop()
        except Exception as e:
            logger.warning(f"[TopCamera] Could not stop top_rolling_buffer: {e}")

    # ── ROI configuration ─────────────────────────────────

    def set_rois(self, rois: list):
        """
        Set staging zone ROIs drawn on every frame.
        Pass [] to clear overlays.
        rois: [(x, y, w, h), ...]
        """
        with self._lock:
            self._rois = list(rois)
        logger.info(f"[TopCamera] ROIs updated: {rois}")

    # ── Frame access ──────────────────────────────────────

    def get_frame(self) -> Optional[np.ndarray]:
        """Return a copy of the latest annotated frame. None if unavailable."""
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def wait_for_frame(self, timeout: float = 0.1) -> bool:
        """Block until a new frame is available. Returns True if a frame arrived."""
        return self._frame_event.wait(timeout=timeout)

    def clear_frame_event(self):
        self._frame_event.clear()

    # ── Background capture thread ─────────────────────────

    def _run(self):
        cap: Optional[cv2.VideoCapture] = None

        while self._running:
            # ── Ensure camera is open ─────────────────────
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

            # ── Capture frame ─────────────────────────────
            ret, frame = cap.read()
            if not ret or frame is None:
                logger.warning("[TopCamera] Frame read failed")
                cap.release()
                cap = None
                time.sleep(0.1)
                continue

            # ── Draw ROI overlays ─────────────────────────
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

            # ── Store frame ───────────────────────────────
            with self._lock:
                self._frame = frame
            self._frame_event.set()

        # Cleanup
        if cap is not None:
            cap.release()
            logger.debug("[TopCamera] Camera released on stop")


# Global singleton — import this everywhere
top_camera = TopCamera()