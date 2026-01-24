#!/usr/bin/env python3
"""
Full system calibration script.
Captures baselines for ALL slots (occupied and empty).
Run this during initial setup.
"""

import logging
import time
import threading
import numpy as np
import cv2
from pathlib import Path
from typing import Dict, Tuple, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import slot embedding functions
from slot_embed import compute_embedding, embedding_distance


class SharedFrameBuffer:
    """
    Thread-safe shared frame buffer for multi-threaded camera access.
    """

    def __init__(self):
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_event = threading.Event()
        self._running = False
        self._capture_thread = None

    def start_capture(self, camera_id: int = 0):
        """Start background camera capture thread"""
        self._running = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            args=(camera_id,),
            daemon=True
        )
        self._capture_thread.start()

        # Wait for first frame
        logger.info("Waiting for first frame...")
        self._frame_event.wait(timeout=5.0)

        if self._latest_frame is None:
            raise RuntimeError("Failed to capture initial frame")

        logger.info("✅ Camera capture started")

    def _capture_loop(self, camera_id: int):
        """Background thread that continuously captures frames"""
        cap = cv2.VideoCapture(camera_id)

        if not cap.isOpened():
            logger.error(f"Failed to open camera {camera_id}")
            return

        # Set camera properties
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)

        # Warm up camera
        for _ in range(10):
            cap.read()

        logger.info(f"Camera {camera_id} initialized")

        while self._running:
            ret, frame = cap.read()

            if ret and frame is not None:
                with self._frame_lock:
                    self._latest_frame = frame.copy()
                    self._frame_event.set()

            time.sleep(0.01)  # ~100 FPS max

        cap.release()
        logger.info("Camera capture stopped")

    def get_frame(self) -> Optional[np.ndarray]:
        """Get the latest frame (thread-safe)"""
        with self._frame_lock:
            if self._latest_frame is not None:
                return self._latest_frame.copy()
            return None

    def stop_capture(self):
        """Stop background capture thread"""
        self._running = False
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
        logger.info("Camera released")


class CameraEmbedder:
    """
    Computes embeddings from shared camera frames for calibration.
    """

    def __init__(self, frame_buffer: SharedFrameBuffer, rois: Dict[int, Tuple[int, int, int, int]]):
        """
        Initialize camera embedder with shared frame buffer.

        Args:
            frame_buffer: SharedFrameBuffer instance
            rois: Dict mapping lid -> (x, y, w, h) ROI coordinates
        """
        self.frame_buffer = frame_buffer
        self.rois = rois

        logger.info(f"CameraEmbedder initialized with {len(rois)} ROIs")

    def compute(self, lid: int) -> np.ndarray:
        """
        Compute embedding for a specific slot from latest frame.

        Args:
            lid: Location ID (slot number)

        Returns:
            96-dimensional normalized embedding vector
        """
        if lid not in self.rois:
            raise ValueError(f"Unknown slot ID: {lid}")

        # Get latest frame from shared buffer
        frame = self.frame_buffer.get_frame()
        if frame is None:
            raise RuntimeError(f"No frame available for slot {lid}")

        # Extract ROI
        x, y, w, h = self.rois[lid]
        roi = frame[y:y + h, x:x + w]

        if roi.size == 0:
            raise ValueError(f"Invalid ROI for slot {lid}: {self.rois[lid]}")

        # Compute embedding
        return compute_embedding(roi)

    def preview_slots(self, slot_ids: list = None):
        """
        Show preview of slots with ROI boxes (for debugging/setup).
        Press 'q' to quit preview.

        Args:
            slot_ids: List of specific slots to highlight, or None for all
        """
        logger.info("Opening camera preview (press 'q' to quit)...")

        slots_to_show = slot_ids if slot_ids else list(self.rois.keys())

        while True:
            frame = self.frame_buffer.get_frame()
            if frame is None:
                time.sleep(0.1)
                continue

            # Draw ROI boxes
            display_frame = frame.copy()
            for lid in slots_to_show:
                if lid not in self.rois:
                    continue

                x, y, w, h = self.rois[lid]
                cv2.rectangle(display_frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(display_frame, f"Slot {lid}", (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)

            # Resize for display if too large
            if display_frame.shape[1] > 1280:
                scale = 1280 / display_frame.shape[1]
                new_w = int(display_frame.shape[1] * scale)
                new_h = int(display_frame.shape[0] * scale)
                display_frame = cv2.resize(display_frame, (new_w, new_h))

            cv2.imshow('Slot Preview', display_frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        cv2.destroyAllWindows()


def generate_grid_rois(
        frame_width: int,
        frame_height: int,
        rows: int,
        cols: int,
        spacing: int = 10
) -> Dict[int, Tuple[int, int, int, int]]:
    """
    Generate a grid of ROIs for slot positions.

    Args:
        frame_width: Camera frame width
        frame_height: Camera frame height
        rows: Number of rows in grid
        cols: Number of columns in grid
        spacing: Spacing between ROIs in pixels

    Returns:
        Dict mapping lid -> (x, y, w, h)
    """
    rois = {}

    # Calculate ROI dimensions
    roi_w = (frame_width - spacing * (cols + 1)) // cols
    roi_h = (frame_height - spacing * (rows + 1)) // rows

    lid = 0
    for row in range(rows):
        for col in range(cols):
            x = spacing + col * (roi_w + spacing)
            y = spacing + row * (roi_h + spacing)
            rois[lid] = (x, y, roi_w, roi_h)
            lid += 1

    logger.info(f"Generated {len(rois)} ROIs: {rows}x{cols} grid")
    logger.info(f"ROI size: {roi_w}x{roi_h}, spacing: {spacing}px")

    return rois


def calibrate_all_slots(camera_embedder: CameraEmbedder, db, rois: Dict[int, tuple]):
    """
    Calibrate baselines for ALL slots in the system.

    Args:
        camera_embedder: CameraEmbedder instance that can compute(lid)
        db: SlotMonitorDB instance
        rois: Dict mapping lid -> (x, y, w, h) for all slots
    """

    logger.info("=" * 70)
    logger.info("FULL SYSTEM CALIBRATION")
    logger.info("=" * 70)
    logger.info(f"Total slots to calibrate: {len(rois)}")
    logger.info("")

    # Get currently occupied slots from database
    occupied_slots = db.fetch_occupied_slots()
    occupied_lids = {lid: pid for lid, pid in occupied_slots}

    logger.info(f"Found {len(occupied_lids)} occupied slots in database")
    logger.info("")

    # Prompt user to prepare
    print("=" * 70)
    print("CALIBRATION PREPARATION")
    print("=" * 70)
    print(f"This will calibrate {len(rois)} slots:")
    print(f"  - {len(occupied_lids)} occupied slots (with phones)")
    print(f"  - {len(rois) - len(occupied_lids)} empty slots")
    print("")
    print("Please ensure:")
    print("  1. All phones are in their correct slots")
    print("  2. No hands or obstructions in front of camera")
    print("  3. Lighting is at normal operating conditions")
    print("  4. Camera is properly positioned")
    print("")

    response = input("Ready to begin calibration? (yes/no): ")
    if response.lower() != 'yes':
        logger.info("Calibration cancelled by user")
        return

    print("")
    logger.info("Starting calibration in 3 seconds...")
    time.sleep(3)

    # Calibrate all slots
    calibrated = 0
    failed = 0

    for lid in sorted(rois.keys()):
        is_occupied = lid in occupied_lids
        pid = occupied_lids.get(lid, None)

        status = "OCCUPIED" if is_occupied else "EMPTY"
        pid_str = f" (PID: {pid})" if pid else ""

        logger.info(f"Calibrating slot {lid:3d} - {status}{pid_str}")

        try:
            # Capture multiple samples for stability
            embeddings = []
            for i in range(5):
                emb = camera_embedder.compute(lid)
                embeddings.append(emb)
                logger.debug(f"  Sample {i + 1}/5 captured")
                time.sleep(0.2)

            # Average embeddings
            avg_emb = np.mean(embeddings, axis=0)

            # Normalize
            norm = np.linalg.norm(avg_emb)
            if norm > 1e-8:
                avg_emb = avg_emb / norm
            else:
                logger.error(f"  ❌ Invalid embedding (zero norm) for slot {lid}")
                failed += 1
                continue

            # Save to database
            db.save_baseline(lid, avg_emb.astype(np.float32))

            logger.info(f"  ✅ Baseline saved (norm: {norm:.4f})")
            calibrated += 1

        except Exception as e:
            logger.error(f"  ❌ Failed to calibrate slot {lid}: {e}")
            failed += 1
            continue

        # Small delay between slots
        time.sleep(0.1)

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info("CALIBRATION COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Total slots: {len(rois)}")
    logger.info(f"✅ Calibrated: {calibrated}")
    logger.info(f"❌ Failed: {failed}")
    logger.info(f"📊 Success rate: {calibrated / len(rois) * 100:.1f}%")
    logger.info("")

    if failed > 0:
        logger.warning(f"⚠️  {failed} slots failed calibration - please check camera view")
    else:
        logger.info("🎉 All slots calibrated successfully!")


def recalibrate_specific_slots(camera_embedder: CameraEmbedder, db, slot_lids: list):
    """
    Recalibrate specific slots (for maintenance or after phone operations).

    Args:
        camera_embedder: CameraEmbedder instance
        db: SlotMonitorDB instance
        slot_lids: List of slot IDs to recalibrate
    """

    logger.info("=" * 70)
    logger.info("SELECTIVE SLOT RECALIBRATION")
    logger.info("=" * 70)
    logger.info(f"Slots to recalibrate: {slot_lids}")
    logger.info("")

    # Get occupancy info
    occupied_slots = db.fetch_occupied_slots()
    occupied_lids = {lid: pid for lid, pid in occupied_slots}

    for lid in slot_lids:
        is_occupied = lid in occupied_lids
        pid = occupied_lids.get(lid, None)

        status = "OCCUPIED" if is_occupied else "EMPTY"
        pid_str = f" (PID: {pid})" if pid else ""

        print("")
        print(f"Recalibrating slot {lid} - {status}{pid_str}")
        print("Ensure slot is in correct state...")
        input("Press ENTER when ready...")

        try:
            # Capture samples
            embeddings = []
            for i in range(5):
                emb = camera_embedder.compute(lid)
                embeddings.append(emb)
                print(f"  Sample {i + 1}/5 captured")
                time.sleep(0.2)

            # Average and normalize
            avg_emb = np.mean(embeddings, axis=0)
            norm = np.linalg.norm(avg_emb)
            if norm > 1e-8:
                avg_emb = avg_emb / norm

            # Save
            db.save_baseline(lid, avg_emb.astype(np.float32))
            print(f"  ✅ Baseline updated")

        except Exception as e:
            logger.error(f"  ❌ Failed to recalibrate slot {lid}: {e}")


def verify_calibration(camera_embedder: CameraEmbedder, db, rois: Dict[int, tuple]):
    """
    Verify that all slots have valid baselines and check current distances.

    Args:
        camera_embedder: CameraEmbedder instance
        db: SlotMonitorDB instance
        rois: Dict of all slot ROIs
    """

    logger.info("=" * 70)
    logger.info("CALIBRATION VERIFICATION")
    logger.info("=" * 70)

    # Load baselines
    baselines = db.fetch_all_baselines()

    logger.info(f"Total slots in system: {len(rois)}")
    logger.info(f"Slots with baselines: {len(baselines)}")

    missing = set(rois.keys()) - set(baselines.keys())
    if missing:
        logger.warning(f"⚠️  Missing baselines for slots: {sorted(missing)}")

    # Check current distances
    logger.info("")
    logger.info("Checking current distances...")
    logger.info("")

    high_distance = []

    for lid in sorted(baselines.keys()):
        try:
            current_emb = camera_embedder.compute(lid)
            baseline = baselines[lid]

            # Calculate distance
            dist = embedding_distance(current_emb, baseline)

            status = "✅" if dist < 0.15 else "⚠️" if dist < 0.35 else "❌"
            logger.info(f"Slot {lid:3d}: distance={dist:.4f} {status}")

            if dist > 0.35:
                high_distance.append((lid, dist))

        except Exception as e:
            logger.error(f"Slot {lid:3d}: Failed to verify - {e}")

    # Summary
    logger.info("")
    logger.info("=" * 70)
    if high_distance:
        logger.warning(f"⚠️  {len(high_distance)} slots have high distances:")
        for lid, dist in high_distance:
            logger.warning(f"  Slot {lid}: {dist:.4f}")
        logger.warning("Consider recalibrating these slots")
    else:
        logger.info("✅ All slots verified - distances within normal range")


if __name__ == "__main__":
    print("=" * 70)
    print("SLOT CALIBRATION SCRIPT")
    print("=" * 70)
    print("")
    print("This script will calibrate baselines for your monitoring system.")
    print("")
    print("Options:")
    print("  1. Preview camera and ROIs (setup/debug)")
    print("  2. Calibrate all slots (initial setup)")
    print("  3. Recalibrate specific slots (maintenance)")
    print("  4. Verify current calibration")
    print("  5. Exit")
    print("")

    choice = input("Enter choice (1-5): ")

    if choice == '5':
        print("Exiting...")
        exit(0)

    # Setup
    print("\nInitializing camera and database...")

    frame_buffer = None

    try:
        # Import database
        from db_interface import SlotMonitorDB

        # Camera configuration
        CAMERA_ID = 0  # Default camera
        FRAME_WIDTH = 1920
        FRAME_HEIGHT = 1080
        GRID_ROWS = 4
        GRID_COLS = 5
        SPACING = 10

        # Generate ROIs (4x5 grid = 20 slots)
        rois = generate_grid_rois(FRAME_WIDTH, FRAME_HEIGHT, GRID_ROWS, GRID_COLS, SPACING)

        # Initialize shared frame buffer
        frame_buffer = SharedFrameBuffer()
        frame_buffer.start_capture(CAMERA_ID)

        # Initialize camera embedder
        embedder = CameraEmbedder(frame_buffer, rois)

        # Initialize database
        db = SlotMonitorDB()

        print("✅ Initialization complete")
        print("")

        # Execute chosen action
        if choice == '1':
            # Preview mode
            embedder.preview_slots()

        elif choice == '2':
            # Full calibration
            calibrate_all_slots(embedder, db, rois)

        elif choice == '3':
            # Selective recalibration
            slot_ids = input("Enter slot IDs to recalibrate (comma-separated): ")
            lids = [int(x.strip()) for x in slot_ids.split(',')]
            recalibrate_specific_slots(embedder, db, lids)

        elif choice == '4':
            # Verification
            verify_calibration(embedder, db, rois)

    except ImportError as e:
        print(f"❌ Failed to import required modules: {e}")
        print("\nPlease ensure the following files exist:")
        print("  - slot_embed.py (embedding functions)")
        print("  - db_interface.py (database interface)")
        print("\nSee IMPLEMENTATION_GUIDE.md for setup details")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback

        traceback.print_exc()

    finally:
        # Cleanup
        if frame_buffer is not None:
            frame_buffer.stop_capture()