# ============================================================
# FILE: back_end/slot_monitor/camera/camera_async.py
# ============================================================
"""
Async event-driven camera system for slot monitoring.

Multi-subscriber bug fix
─────────────────────────
The original code used a single shared asyncio.Event for all workers.
When 4 workers all await the same Event, the first one to resume calls
.clear() — the other 3 see the event already cleared and go back to
waiting, missing that frame entirely.  Under heavy load this means
workers fall behind by one frame per update, effectively halving their
throughput at 4-worker scale.

Fix: per-subscriber Future broadcast.  _set_frame_event() resolves
ALL pending futures simultaneously.  Each worker gets its own Future
so no worker can starve another.
"""

import cv2
import asyncio
import logging
import threading
import time
from typing import List, Optional
import numpy as np

logger = logging.getLogger(__name__)


class AsyncFrameBuffer:
    """
    Event-driven frame buffer that notifies all subscribers simultaneously.
    """

    def __init__(self, max_subscribers: int = 100):
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_count = 0

        # Per-subscriber Future broadcast — replaces the shared asyncio.Event.
        # _waiters holds one unresolved Future per currently-blocked worker.
        # _set_frame_event() resolves ALL of them at once; no worker starves.
        self._waiters: List[asyncio.Future] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        self._running = False
        self._capture_thread = None

        self._subscribers: List[str] = []
        self._max_subscribers = max_subscribers

        self._notification_latency_ms = 0.0

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """
        Set the asyncio event loop for frame notifications.
        MUST be called before start_capture() if using async workers.

        Args:
            loop: The asyncio event loop to use for notifications
        """
        self._loop = loop
        logger.info("Event loop attached to AsyncFrameBuffer")

    def start_capture(
        self,
        camera_id: int = 0,
        width: int = 1920,
        height: int = 1080,
        fps: int = 30,
    ):
        if self._loop is None:
            raise RuntimeError(
                "Event loop not set. Call set_event_loop() before start_capture()"
            )
        if self._running:
            logger.warning("Capture already running")
            return

        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(camera_id, width, height, fps),
            daemon=True,
            name="AsyncCameraCapture"
        )
        self._capture_thread.start()

        logger.info("Waiting for first frame...")
        start = time.time()
        while self._frame_count == 0 and time.time() - start < 5.0:
            time.sleep(0.01)

        if self._frame_count == 0:
            self._running = False
            raise RuntimeError("Failed to capture initial frame within 5 seconds")

        logger.info(f"Async camera {camera_id} started ({width}x{height} @ {fps}fps)")

    def _capture_loop(self, camera_id: int, width: int, height: int, fps: int):
        """
        Background camera capture thread.
        Notifies async workers via event when new frame arrives.
        """
        cap = cv2.VideoCapture(camera_id)

        if not cap.isOpened():
            logger.error(f"Failed to open camera {camera_id}")
            return

        # Configure camera
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = int(cap.get(cv2.CAP_PROP_FPS))
        logger.info(f"Camera {camera_id} opened: {actual_w}x{actual_h} @ {actual_fps}fps")

        for _ in range(10):   # warm up
            cap.read()

        frame_interval = 1.0 / fps

        while self._running:
            loop_start = time.time()
            ret, frame = cap.read()

            if ret and frame is not None:
                # Make frame read-only (enables zero-copy sharing)
                frame.flags.writeable = False

                # Update frame buffer
                with self._frame_lock:
                    self._latest_frame = frame
                    self._frame_count += 1

                # Notify async workers (thread-safe)
                self._notify_frame_ready()

            else:
                logger.warning("Failed to read frame from camera")

            # Maintain target FPS
            elapsed = time.time() - loop_start
            sleep_time = max(0, frame_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        cap.release()
        logger.info("Async camera capture stopped")

    def _notify_frame_ready(self):
        """
        Notify async workers that a new frame is ready.
        Thread-safe: called from camera thread, notifies async event loop.
        """
        if self._loop is None:
            return
        t0 = time.time()
        self._loop.call_soon_threadsafe(self._set_frame_event)
        self._notification_latency_ms = (time.time() - t0) * 1000

    def _set_frame_event(self):
        """
        Called in the event loop thread.

        Atomically swaps the waiters list and resolves every pending Future.
        All workers wake up simultaneously — no worker starves another.
        This replaces the old shared asyncio.Event whose single .clear()
        call after the first worker resumed caused the remaining workers to
        miss the frame.
        """
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)

    async def wait_for_frame(self, subscriber_id: str = "unknown") -> np.ndarray:
        """
        Wait for next frame (async, no polling).

        This is the KEY method that eliminates polling:
        - Blocks asynchronously until new frame arrives
        - Zero CPU usage while waiting
        - Immediate wake-up on new frame

        Args:
            subscriber_id: ID for tracking/debugging

        Returns:
            Latest frame (read-only, zero-copy)

        Usage:
            while True:
                frame = await buffer.wait_for_frame("worker-1")
                process(frame)  # Runs immediately on new frame
        """
        if subscriber_id not in self._subscribers:
            if len(self._subscribers) < self._max_subscribers:
                self._subscribers.append(subscriber_id)
            else:
                logger.warning(
                    f"Max subscribers ({self._max_subscribers}) reached, "
                    f"ignoring {subscriber_id}"
                )

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        self._waiters.append(fut)
        await fut

        with self._frame_lock:
            if self._latest_frame is None:
                raise RuntimeError("No frame available")
            return self._latest_frame

    def get_frame_sync(self) -> Optional[np.ndarray]:
        """
        Get latest frame synchronously (for non-async code).

        Returns:
            Copy of latest frame, or None
        """
        with self._frame_lock:
            return self._latest_frame.copy() if self._latest_frame is not None else None

    def get_frame_count(self) -> int:
        """Get total frames captured"""
        with self._frame_lock:
            return self._frame_count

    def get_metrics(self) -> dict:
        """
        Get performance metrics.

        Returns:
            Dict with performance stats
        """
        with self._frame_lock:
            return {
                "frame_count":              self._frame_count,
                "active_subscribers":       len(self._subscribers),
                "notification_latency_ms":  self._notification_latency_ms,
                "running":                  self._running,
            }

    def is_running(self) -> bool:
        return self._running

    def stop_capture(self):
        if not self._running:
            return
        self._running = False
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
        logger.info("Async camera released")


class AsyncCameraCapture:
    """High-level async camera interface wrapping AsyncFrameBuffer."""

    def __init__(
        self,
        frame_buffer: AsyncFrameBuffer,
        camera_id: int = 0,
        width: int = 1920,
        height: int = 1080,
    ):
        self.frame_buffer = frame_buffer
        self.camera_id    = camera_id
        self.width        = width
        self.height       = height

        if not frame_buffer.is_running():
            raise RuntimeError("Frame buffer is not running")

        logger.info(f"AsyncCameraCapture initialized (cam {camera_id}, {width}x{height})")

    async def read(self, subscriber_id: str = "unknown") -> np.ndarray:
        """
        Read next frame asynchronously (event-driven, no polling).

        Args:
            subscriber_id: ID for tracking

        Returns:
            Latest frame (read-only)
        """
        return await self.frame_buffer.wait_for_frame(subscriber_id)

    def read_sync(self) -> Optional[np.ndarray]:
        """
        Read latest frame synchronously.

        Returns:
            Copy of latest frame
        """
        return self.frame_buffer.get_frame_sync()

    def get_frame_count(self) -> int:
        """Get total frames captured"""
        return self.frame_buffer.get_frame_count()

    def get_metrics(self) -> dict:
        """Get performance metrics"""
        return self.frame_buffer.get_metrics()

    def get_dimensions(self) -> tuple[int, int]:
        """Get frame dimensions"""
        return (self.width, self.height)