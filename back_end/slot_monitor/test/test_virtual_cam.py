# ============================================================
# FILE: back_end/slot_monitor/virtual_camera_test.py
# ============================================================
"""
Virtual Camera Testing System

Simulates multiple cameras for testing DVW + monitoring without physical cameras.

MODES:
1. Mock mode - No camera required, generates synthetic frames
2. Single camera mode - Uses one physical camera, simulates others
3. Hybrid mode - Mix of real and mock cameras
"""

import cv2
import numpy as np
import time
import logging
from typing import Optional, Tuple
import threading
from queue import Queue

logger = logging.getLogger(__name__)


class VirtualCamera:
    """
    Virtual camera that can operate in different modes.

    Modes:
    - mock: Generate synthetic frames (no camera needed)
    - real: Use actual camera
    - static: Use static image repeated
    """

    def __init__(
            self,
            camera_id: int,
            width: int = 1280,
            height: int = 720,
            fps: int = 30,
            mode: str = "mock",
            real_camera_id: Optional[int] = None,
            static_image_path: Optional[str] = None
    ):
        """
        Args:
            camera_id: Virtual camera ID (for identification)
            width: Frame width
            height: Frame height
            fps: Frames per second
            mode: "mock", "real", or "static"
            real_camera_id: Physical camera to use if mode="real"
            static_image_path: Image file to use if mode="static"
        """
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.fps = fps
        self.mode = mode
        self.real_camera_id = real_camera_id
        self.static_image_path = static_image_path

        self._capture = None
        self._static_frame = None
        self._running = False
        self._thread = None
        self._frame_queue = Queue(maxsize=2)
        self._frame_count = 0

        logger.info(f"VirtualCamera {camera_id} created: mode={mode}, {width}x{height}@{fps}fps")

    def start(self):
        """Start camera capture"""
        if self._running:
            logger.warning(f"VirtualCamera {self.camera_id} already running")
            return

        if self.mode == "real":
            self._start_real_camera()
        elif self.mode == "static":
            self._load_static_image()
        elif self.mode == "mock":
            pass  # Mock frames generated on demand

        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"VirtualCam-{self.camera_id}"
        )
        self._thread.start()
        logger.info(f"✅ VirtualCamera {self.camera_id} started ({self.mode} mode)")

    def stop(self):
        """Stop camera capture"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._capture:
            self._capture.release()
        logger.info(f"VirtualCamera {self.camera_id} stopped")

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        """
        Read latest frame.

        Returns:
            (success, frame)
        """
        if not self._running:
            return False, None

        if self._frame_queue.empty():
            return False, None

        frame = self._frame_queue.get()
        return True, frame

    def _start_real_camera(self):
        """Initialize real camera"""
        if self.real_camera_id is None:
            logger.error(f"VirtualCamera {self.camera_id}: real_camera_id not specified")
            return

        self._capture = cv2.VideoCapture(self.real_camera_id)
        if not self._capture.isOpened():
            logger.error(f"VirtualCamera {self.camera_id}: Failed to open camera {self.real_camera_id}")
            self._capture = None
            return

        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self._capture.set(cv2.CAP_PROP_FPS, self.fps)
        logger.info(f"Real camera {self.real_camera_id} opened for VirtualCamera {self.camera_id}")

    def _load_static_image(self):
        """Load static image"""
        if self.static_image_path is None:
            logger.warning(f"VirtualCamera {self.camera_id}: No static image path, using blank frame")
            self._static_frame = self._generate_blank_frame()
            return

        self._static_frame = cv2.imread(self.static_image_path)
        if self._static_frame is None:
            logger.warning(f"VirtualCamera {self.camera_id}: Failed to load {self.static_image_path}, using blank")
            self._static_frame = self._generate_blank_frame()
        else:
            self._static_frame = cv2.resize(self._static_frame, (self.width, self.height))
            logger.info(f"Static image loaded for VirtualCamera {self.camera_id}")

    def _capture_loop(self):
        """Main capture loop"""
        interval = 1.0 / self.fps

        while self._running:
            start_time = time.time()

            frame = self._get_frame()
            if frame is not None:
                # Drop old frame if queue is full
                if self._frame_queue.full():
                    try:
                        self._frame_queue.get_nowait()
                    except:
                        pass

                self._frame_queue.put(frame)
                self._frame_count += 1

            # Maintain FPS
            elapsed = time.time() - start_time
            sleep_time = max(0, interval - elapsed)
            time.sleep(sleep_time)

    def _get_frame(self) -> Optional[np.ndarray]:
        """Get next frame based on mode"""
        if self.mode == "real":
            if self._capture is None:
                return None
            ret, frame = self._capture.read()
            return frame if ret else None

        elif self.mode == "static":
            return self._static_frame.copy() if self._static_frame is not None else None

        elif self.mode == "mock":
            return self._generate_mock_frame()

        return None

    def _generate_mock_frame(self) -> np.ndarray:
        """Generate synthetic frame"""
        # Create gradient background
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        # Add gradient
        for y in range(self.height):
            intensity = int(255 * y / self.height)
            frame[y, :] = [intensity // 3, intensity // 2, intensity]

        # Add camera ID text
        cv2.putText(
            frame,
            f"Virtual Camera {self.camera_id}",
            (50, 50),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.5,
            (255, 255, 255),
            3
        )

        # Add timestamp
        timestamp = time.strftime("%H:%M:%S")
        cv2.putText(
            frame,
            f"Time: {timestamp}",
            (50, 100),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2
        )

        # Add frame counter
        cv2.putText(
            frame,
            f"Frame: {self._frame_count}",
            (50, 150),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.0,
            (255, 255, 255),
            2
        )

        # Add some visual noise for realism
        noise = np.random.randint(0, 20, (self.height, self.width, 3), dtype=np.uint8)
        frame = cv2.add(frame, noise)

        return frame

    def _generate_blank_frame(self) -> np.ndarray:
        """Generate blank frame"""
        frame = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        cv2.putText(
            frame,
            f"VirtualCamera {self.camera_id}",
            (self.width // 4, self.height // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            2.0,
            (128, 128, 128),
            3
        )
        return frame

    def get_metrics(self) -> dict:
        """Get camera metrics"""
        return {
            "camera_id": self.camera_id,
            "mode": self.mode,
            "running": self._running,
            "frame_count": self._frame_count,
            "queue_size": self._frame_queue.qsize()
        }


# ============================================================
# VIRTUAL CAMERA MANAGER
# ============================================================

class VirtualCameraManager:
    """
    Manages multiple virtual cameras for testing.

    Example:
        manager = VirtualCameraManager()

        # Create 3 virtual cameras
        manager.add_camera(0, mode="real", real_camera_id=0)  # Top: real camera
        manager.add_camera(1, mode="mock")                     # Bottom: mock
        manager.add_camera(2, mode="mock")                     # Extra: mock

        manager.start_all()

        # Use like normal cameras
        ret, frame = manager.read(0)  # Read from virtual camera 0
        ret, frame = manager.read(1)  # Read from virtual camera 1
    """

    def __init__(self):
        self.cameras: dict[int, VirtualCamera] = {}
        logger.info("VirtualCameraManager initialized")

    def add_camera(
            self,
            camera_id: int,
            width: int = 1280,
            height: int = 720,
            fps: int = 30,
            mode: str = "mock",
            **kwargs
    ):
        """
        Add a virtual camera.

        Args:
            camera_id: Virtual camera ID
            width: Frame width
            height: Frame height
            fps: Frames per second
            mode: "mock", "real", or "static"
            **kwargs: Additional args for VirtualCamera
        """
        if camera_id in self.cameras:
            logger.warning(f"VirtualCamera {camera_id} already exists, replacing")
            self.remove_camera(camera_id)

        camera = VirtualCamera(
            camera_id=camera_id,
            width=width,
            height=height,
            fps=fps,
            mode=mode,
            **kwargs
        )
        self.cameras[camera_id] = camera
        logger.info(f"Added VirtualCamera {camera_id} ({mode} mode)")

    def remove_camera(self, camera_id: int):
        """Remove a virtual camera"""
        if camera_id in self.cameras:
            self.cameras[camera_id].stop()
            del self.cameras[camera_id]
            logger.info(f"Removed VirtualCamera {camera_id}")

    def start_all(self):
        """Start all virtual cameras"""
        logger.info(f"Starting {len(self.cameras)} virtual cameras...")
        for camera in self.cameras.values():
            camera.start()
        logger.info("✅ All virtual cameras started")

    def stop_all(self):
        """Stop all virtual cameras"""
        logger.info("Stopping all virtual cameras...")
        for camera in self.cameras.values():
            camera.stop()
        logger.info("✅ All virtual cameras stopped")

    def read(self, camera_id: int) -> Tuple[bool, Optional[np.ndarray]]:
        """Read from a virtual camera"""
        if camera_id not in self.cameras:
            logger.error(f"VirtualCamera {camera_id} not found")
            return False, None

        return self.cameras[camera_id].read()

    def get_all_metrics(self) -> dict:
        """Get metrics from all cameras"""
        return {
            cam_id: cam.get_metrics()
            for cam_id, cam in self.cameras.items()
        }


# ============================================================
# QUICK TEST SETUP
# ============================================================

def create_test_setup_one_camera():
    """
    Test setup with ONE physical camera.

    Returns:
        VirtualCameraManager configured for testing
    """
    manager = VirtualCameraManager()

    # Camera 0 (Top): Real camera for QR scanning
    manager.add_camera(
        camera_id=0,
        mode="real",
        real_camera_id=0,  # Your physical camera
        width=1280,
        height=720,
        fps=30
    )

    # Camera 1 (Bottom): Mock camera for slot monitoring
    manager.add_camera(
        camera_id=1,
        mode="mock",
        width=1280,
        height=720,
        fps=30
    )

    logger.info("✅ Test setup created: 1 real camera + 1 mock camera")
    return manager


def create_test_setup_no_camera():
    """
    Test setup with NO physical camera (pure simulation).

    Returns:
        VirtualCameraManager configured for testing
    """
    manager = VirtualCameraManager()

    # Camera 0 (Top): Mock camera for QR scanning
    manager.add_camera(
        camera_id=0,
        mode="mock",
        width=1280,
        height=720,
        fps=30
    )

    # Camera 1 (Bottom): Mock camera for slot monitoring
    manager.add_camera(
        camera_id=1,
        mode="mock",
        width=1280,
        height=720,
        fps=30
    )

    logger.info("✅ Test setup created: 2 mock cameras (no physical cameras needed)")
    return manager


# ============================================================
# DEMO
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("VIRTUAL CAMERA DEMO")
    print("=" * 60)

    # Create manager
    manager = create_test_setup_no_camera()  # Change to create_test_setup_one_camera() if you have a camera

    # Start cameras
    manager.start_all()

    print("\nPress 'q' to quit\n")

    try:
        while True:
            # Read from both cameras
            ret0, frame0 = manager.read(0)
            ret1, frame1 = manager.read(1)

            if ret0:
                cv2.imshow("Virtual Camera 0 (Top)", frame0)
            if ret1:
                cv2.imshow("Virtual Camera 1 (Bottom)", frame1)

            # Check for quit
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break

            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\nInterrupted by user")

    finally:
        manager.stop_all()
        cv2.destroyAllWindows()
        print("\n✅ Demo complete")