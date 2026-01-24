# ============================================================
# FILE: server/slot_monitor/slot_camera.py
# ============================================================

import cv2
import logging
import threading
import time
from typing import Dict, Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)


def generate_grid_rois(
        frame_width: int,
        frame_height: int,
        rows: int,
        cols: int,
        spacing: int
) -> Dict[int, Tuple[int, int, int, int]]:
    """Generate ROI coordinates for grid layout"""
    rois = {}
    cell_h = frame_height // rows
    cell_w = frame_width // cols

    for i in range(rows):
        for j in range(cols):
            x1 = j * cell_w + spacing // 2
            y1 = i * cell_h + spacing // 2
            x2 = (j + 1) * cell_w - spacing // 2
            y2 = (i + 1) * cell_h - spacing // 2

            # lid = slot index (0-based)
            lid = i * cols + j
            rois[lid] = (x1, y1, x2 - x1, y2 - y1)  # (x, y, w, h)

    return rois


class SharedFrameBuffer:
    """
    Thread-safe shared frame buffer for multi-threaded camera access.
    Only stores the latest frame (max_size = 1).

    LOCKING BEHAVIOR:
    - Multiple readers CAN read simultaneously (they each get a copy)
    - The lock only blocks during the brief moment of copying
    - Lock contention is minimal (~microseconds for frame.copy())
    - Writers (capture thread) briefly block readers during frame update
    """

    def __init__(self):
        self._frame_lock = threading.Lock()  # RLock not needed - simple read/write
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_event = threading.Event()
        self._running = False
        self._capture_thread = None
        self._frame_count = 0

    def start_capture(self, camera_id: int = 0, width: int = 1920, height: int = 1080):
        """Start background camera capture thread"""
        if self._running:
            logger.warning("Capture already running")
            return

        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(camera_id, width, height),
            daemon=True,
            name="SlotCameraCapture"
        )
        self._capture_thread.start()

        # Wait for first frame
        logger.info("Waiting for first frame...")
        if not self._frame_event.wait(timeout=5.0):
            self._running = False
            raise RuntimeError("Failed to capture initial frame within 5 seconds")

        logger.info(f"✅ Camera {camera_id} capture started ({width}x{height})")

    def _capture_loop(self, camera_id: int, width: int, height: int):
        """Background thread that continuously captures frames"""
        cap = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)

        if not cap.isOpened():
            logger.error(f"Failed to open camera {camera_id}")
            return

        # Set camera properties
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Minimize buffering

        # Verify actual resolution
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info(f"Camera {camera_id} opened: {actual_w}x{actual_h}")

        # Warm up camera (discard first few frames)
        for _ in range(10):
            cap.read()

        frame_interval = 1.0 / 30.0  # 30 FPS target

        while self._running:
            start_time = time.time()

            ret, frame = cap.read()

            if ret and frame is not None:
                # Update shared frame (brief lock)
                with self._frame_lock:
                    self._latest_frame = frame  # Store reference, not copy
                    self._frame_count += 1
                    self._frame_event.set()
            else:
                logger.warning("Failed to read frame from camera")

            # Maintain consistent frame rate
            elapsed = time.time() - start_time
            sleep_time = max(0, frame_interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        cap.release()
        logger.info("Camera capture stopped")

    def get_frame(self) -> Optional[np.ndarray]:
        """
        Get the latest frame (thread-safe).

        Returns a COPY of the frame so modifications don't affect the buffer.
        Multiple threads can call this simultaneously - each gets their own copy.

        Lock is held ONLY during the copy operation (~1-2ms for 1080p).
        """
        with self._frame_lock:
            if self._latest_frame is not None:
                return self._latest_frame.copy()
            return None

    def get_frame_count(self) -> int:
        """Get total number of frames captured"""
        with self._frame_lock:
            return self._frame_count

    def is_running(self) -> bool:
        """Check if capture is active"""
        return self._running

    def stop_capture(self):
        """Stop background capture thread"""
        if not self._running:
            return

        self._running = False
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)

        logger.info("Camera released")


class SlotCamera:
    """
    Camera interface for slot monitoring using SharedFrameBuffer.
    Multiple threads can safely extract ROIs from the same frame.
    """

    def __init__(
            self,
            frame_buffer: SharedFrameBuffer,
            rois: Dict[int, Tuple[int, int, int, int]]
    ):
        """
        Initialize slot camera with shared frame buffer.

        Args:
            frame_buffer: SharedFrameBuffer instance (already started)
            rois: Dict mapping lid -> (x, y, w, h) ROI coordinates
        """
        self.frame_buffer = frame_buffer
        self.rois = rois

        if not frame_buffer.is_running():
            raise RuntimeError("Frame buffer is not running")

        logger.info(f"SlotCamera initialized with {len(rois)} ROIs")

    def read(self) -> Optional[np.ndarray]:
        """
        Read the latest frame from shared buffer.
        Thread-safe - multiple threads can call simultaneously.
        """
        return self.frame_buffer.get_frame()

    def extract_roi(self, frame: np.ndarray, lid: int) -> Optional[np.ndarray]:
        """
        Extract a single ROI from frame.

        Args:
            frame: Full camera frame
            lid: Location ID (slot number)

        Returns:
            ROI image or None if invalid
        """
        if lid not in self.rois:
            logger.warning(f"Unknown ROI lid={lid}")
            return None

        x, y, w, h = self.rois[lid]

        # Validate coordinates
        if x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
            logger.warning(f"ROI {lid} out of bounds: ({x},{y},{w},{h}) vs frame {frame.shape}")
            return None

        return frame[y:y + h, x:x + w].copy()

    def extract_rois(self, frame: np.ndarray) -> Dict[int, np.ndarray]:
        """
        Extract all ROI regions from frame.

        Args:
            frame: Full camera frame

        Returns:
            Dict mapping lid -> ROI image
        """
        slices = {}
        for lid in self.rois.keys():
            roi = self.extract_roi(frame, lid)
            if roi is not None:
                slices[lid] = roi
        return slices

    def get_frame_count(self) -> int:
        """Get total frames captured by the buffer"""
        return self.frame_buffer.get_frame_count()

    def get_rois(self) -> Dict[int, Tuple[int, int, int, int]]:
        """Get ROI definitions"""
        return self.rois.copy()


