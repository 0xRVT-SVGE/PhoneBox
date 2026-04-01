# ============================================================
# FILE: back_end/slot_monitor/camera/top_camera.py
# ============================================================
"""
Shared state for the top-down camera (index 2).

Overlay architecture
────────────────────
Two independent callable hooks are applied to every captured frame
before it is stored in the buffer (and streamed via WebRTC):

  _context_overlay  Callable[[np.ndarray], None] | None
      Set by ops_handler or admin_ops_handler before a tracked
      operation begins.  Draws the session context:
        • all slot ROIs as a dim reference grid
        • source slot (verify)
        • destination slot with corner brackets + pulsing
        • staging zones with occupancy colours (admin session)
      Cleared in _finalize_deposit / _finalize_verify /
      _on_tracking_failed / admin session close.

  _tracker_overlay  Callable[[np.ndarray], None] | None
      Set by PhoneTracker once CSRT tracking is live.
      Draws only the phone bounding box and QR status badge.
      Cleared by PhoneTracker when tracking ends.

  Context is drawn FIRST; tracker bbox is drawn ON TOP.
  Both assignments are atomic under CPython GIL — no lock needed.

Legacy set_rois() API
─────────────────────
admin_ops_handler still calls top_camera.set_rois(staging_rois) and
top_camera.set_rois([]) to show/clear staging zone outlines.
This method is kept for backward compatibility.  For occupancy-aware
staging overlays, admin_ops_handler should call:

    from back_end.slot_monitor.phone_tracker import make_staging_context_overlay
    top_camera.set_context_overlay(
        make_staging_context_overlay(StagingConfig.get_rois(), staged_pids)
    )
"""

import cv2
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)

CAMERA_INDEX     = 2
_RECONNECT_DELAY = 1.0


class TopCamera:

    def __init__(self):
        self._lock            = threading.Lock()
        self._frame:          Optional[np.ndarray] = None
        self._frame_event     = threading.Event()
        self._running         = False
        self._thread:         Optional[threading.Thread] = None

        # Two-hook overlay system.
        # Both read in _run() without a lock — CPython GIL makes assignments atomic.
        self._context_overlay: Optional[Callable[[np.ndarray], None]] = None
        self._tracker_overlay: Optional[Callable[[np.ndarray], None]] = None

    # ── Lifecycle ─────────────────────────────────────────

    def start(self) -> None:
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

    def stop(self) -> None:
        """Stop the capture thread and rolling buffer. No-op if never started."""
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

    # ── Context overlay (session context: grid, source, dest, staging) ────

    def set_context_overlay(self, fn: Callable[[np.ndarray], None]) -> None:
        """
        Register the session-context draw callable.
        Replaces any previously set context overlay.
        Called from ops_handler or admin_ops_handler.
        """
        self._context_overlay = fn

    def clear_context_overlay(self) -> None:
        """Remove the context overlay. Called when an operation ends."""
        self._context_overlay = None

    # ── Tracker overlay (live bounding box) ──────────────

    def set_tracker_overlay(self, fn: Callable[[np.ndarray], None]) -> None:
        """
        Register the tracker's bounding-box draw callable.
        Called from PhoneTracker once CSRT is initialised.
        """
        self._tracker_overlay = fn

    def clear_tracker_overlay(self) -> None:
        """Remove the tracker overlay. Called when tracking ends."""
        self._tracker_overlay = None

    # ── Legacy set_rois() — backward compat ──────────────

    def set_rois(self, rois: list) -> None:
        """
        Backward-compatible API used by admin_ops_handler.

        set_rois([roi, roi]) → sets a simple staging-zone context overlay.
        set_rois([])         → clears the context overlay.

        For occupancy-aware colours, admin_ops_handler should call
        set_context_overlay(make_staging_context_overlay(...)) directly.
        """
        if not rois:
            self.clear_context_overlay()
            return

        captured = list(rois)

        def _simple_staging(frame: np.ndarray) -> None:
            for i, roi in enumerate(captured):
                if not roi or len(roi) < 4:
                    continue
                x, y, w, h = roi
                color = (0, 255, 0) if i == 0 else (0, 200, 255)
                label = "STAGING 1" if i == 0 else "STAGING 2"
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                cv2.putText(
                    frame, label,
                    (x, max(y - 6, 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA,
                )

        self.set_context_overlay(_simple_staging)

    # ── Frame access ──────────────────────────────────────

    def get_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def wait_for_frame(self, timeout: float = 0.1) -> bool:
        return self._frame_event.wait(timeout=timeout)

    def clear_frame_event(self) -> None:
        self._frame_event.clear()

    # ── Capture thread ────────────────────────────────────

    def _run(self) -> None:
        cap: Optional[cv2.VideoCapture] = None

        while self._running:
            # Ensure camera is open
            if cap is None or not cap.isOpened():
                # Use CAP_DSHOW on Windows to avoid MSMF sharing conflicts.
                # MSMF does not allow two processes/threads to open the same
                # camera simultaneously; CAP_DSHOW (DirectShow) does not have
                # this restriction and avoids -1072873821 grab errors.
                cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
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

            # ── Context overlay (grid / source / dest / staging) ──
            # Read without lock — atomic under CPython GIL
            ctx_fn = self._context_overlay
            if ctx_fn is not None:
                try:
                    ctx_fn(frame)
                except Exception as e:
                    logger.warning(f"[TopCamera] Context overlay error: {e}")

            # ── Tracker overlay (bbox + QR status) ────────────────
            trk_fn = self._tracker_overlay
            if trk_fn is not None:
                try:
                    trk_fn(frame)
                except Exception as e:
                    logger.warning(f"[TopCamera] Tracker overlay error: {e}")

            # Store annotated frame
            with self._lock:
                self._frame = frame
            self._frame_event.set()

        if cap is not None:
            cap.release()
            logger.debug("[TopCamera] Camera released on stop")


# Global singleton — import this everywhere
top_camera = TopCamera()