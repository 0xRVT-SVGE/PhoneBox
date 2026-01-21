#!/usr/bin/env python3
"""
Complete test suite for phone monitoring system using static images.
Tests the full pipeline: embedding → distance → state tracking → alarms
"""

import os
import time
import logging
import numpy as np
from pathlib import Path
from typing import Dict, List
import cv2

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import your modules
from slot_state import SlotState
from alarm_controller import AlarmController
from slot_embed import compute_embedding, embedding_distance


class MockEmbedder:
    """Mock embedder that reads images from a directory"""

    def __init__(self, image_dir: str):
        self.image_dir = Path(image_dir)
        self.images: Dict[int, str] = {}

    def load_slot_images(self, slot_id: int, image_path: str):
        """Register an image for a specific slot"""
        self.images[slot_id] = image_path
        logger.info(f"Loaded image for slot {slot_id}: {image_path}")

    def compute(self, lid: int) -> np.ndarray:
        """Compute embedding for a slot from its registered image"""
        if lid not in self.images:
            raise ValueError(f"No image registered for slot {lid}")

        img_path = self.image_dir / self.images[lid]
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {img_path}")

        img = cv2.imread(str(img_path))
        if img is None:
            raise ValueError(f"Failed to read image: {img_path}")

        return compute_embedding(img)


class TestMonitoringSystem:
    """Test the complete monitoring system"""

    def __init__(self, image_dir: str = "./test_images"):
        self.image_dir = Path(image_dir)
        self.embedder = MockEmbedder(image_dir)
        self.alarm = AlarmController()
        self.slots: Dict[int, SlotState] = {}

        # Test configuration
        self.mismatch_threshold = 0.35
        self.recalc_threshold = 0.12
        self.grace_period = 3.0  # Shorter for testing

        logger.info(f"Test system initialized with image_dir: {image_dir}")

    def setup_test_scenario(self, scenario: Dict):
        """
        Setup a test scenario with baseline images and test images.

        scenario = {
            "slots": {
                0: {"baseline": "slot0_empty.jpg", "test": "slot0_occupied.jpg"},
                1: {"baseline": "slot1_phone.jpg", "test": "slot1_phone.jpg"},
                2: {"baseline": "slot2_phone.jpg", "test": "slot2_empty.jpg"},
            }
        }
        """
        logger.info("=" * 60)
        logger.info("SETTING UP TEST SCENARIO")
        logger.info("=" * 60)

        for lid, config in scenario["slots"].items():
            baseline_path = config["baseline"]

            # Load baseline image and compute embedding
            self.embedder.load_slot_images(lid, baseline_path)
            baseline_emb = self.embedder.compute(lid)

            # Create slot state (assume occupied for now)
            self.slots[lid] = SlotState(
                lid=lid,
                baseline_emb=baseline_emb,
                is_occupied=True
            )

            logger.info(f"Slot {lid}: baseline set from {baseline_path}")

    def run_monitoring_cycle(self, test_images: Dict[int, str], pid_map: Dict[int, int]):
        """
        Run one monitoring cycle with test images.

        test_images = {0: "slot0_occupied.jpg", 1: "slot1_phone.jpg", ...}
        pid_map = {0: 101, 1: 102, ...}  # lid -> pid mapping
        """
        logger.info("\n" + "=" * 60)
        logger.info("RUNNING MONITORING CYCLE")
        logger.info("=" * 60)

        for lid, img_name in test_images.items():
            if lid not in self.slots:
                logger.warning(f"Slot {lid} not initialized, skipping")
                continue

            # Update embedder with test image
            self.embedder.images[lid] = img_name

            # Compute embedding
            current_emb = self.embedder.compute(lid)
            slot = self.slots[lid]

            # Calculate distance
            dist = embedding_distance(current_emb, slot.baseline)

            # Update slot state
            result = slot.update_distance(
                dist=dist,
                mismatch_threshold=self.mismatch_threshold,
                recalc_threshold=self.recalc_threshold,
                grace_period=self.grace_period
            )

            # Log results
            logger.info(f"\nSlot {lid}:")
            logger.info(f"  Image: {img_name}")
            logger.info(f"  Distance: {dist:.4f}")
            logger.info(f"  Mismatch: {slot.mismatch}")
            logger.info(f"  Trigger alarm: {result['trigger_alarm']}")
            logger.info(f"  Stop alarm: {result['stop_alarm']}")
            logger.info(f"  Needs recalc: {result['needs_recalc']}")

            # Handle alarms
            pid = pid_map.get(lid, lid)  # Use lid as pid if not mapped

            if result["trigger_alarm"]:
                self.alarm.trigger(pid, lid)
                logger.warning(f"  ⚠️  ALARM TRIGGERED for PID={pid}, LID={lid}")

            if result["stop_alarm"]:
                any_mismatch = any(s.mismatch for s in self.slots.values())
                self.alarm.stop_if_clear(any_mismatch)

    def print_alarm_status(self):
        """Print current alarm status"""
        status = self.alarm.get_status()
        logger.info("\n" + "=" * 60)
        logger.info("ALARM STATUS")
        logger.info("=" * 60)
        logger.info(f"Active: {status['active']}")
        logger.info(f"Mismatch count: {status['mismatch_count']}")
        logger.info(f"Duration: {status['duration']:.1f}s")

        if status['mismatch_count'] > 0:
            logger.info("\nMismatched slots:")
            for pid, lid in sorted(self.alarm.mismatches):
                logger.info(f"  PID={pid}, LID={lid}")

    def test_grace_period(self):
        """Test that grace period works correctly"""
        logger.info("\n" + "=" * 60)
        logger.info("TESTING GRACE PERIOD")
        logger.info("=" * 60)

        # Setup: one slot with significant distance but below grace period
        slot = self.slots[0]
        test_emb = self.embedder.compute(0)

        # Simulate high distance but within grace period
        for i in range(3):
            dist = 0.40  # Above threshold
            result = slot.update_distance(
                dist=dist,
                mismatch_threshold=self.mismatch_threshold,
                recalc_threshold=self.recalc_threshold,
                grace_period=self.grace_period
            )

            logger.info(f"Cycle {i + 1}: dist={dist:.3f}, trigger={result['trigger_alarm']}")
            time.sleep(1)

        logger.info("Grace period test complete")


def create_test_images(output_dir: str = "./test_images"):
    """
    Create sample test images for the monitoring system.
    This creates synthetic images to test the system.
    """
    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)

    logger.info(f"Creating test images in {output_dir}")

    # Create empty slot images (gray background)
    for i in range(3):
        img = np.ones((480, 640, 3), dtype=np.uint8) * 200  # Light gray
        cv2.imwrite(str(output_path / f"slot{i}_empty.jpg"), img)

    # Create occupied slot images (with a dark rectangle representing phone)
    for i in range(3):
        img = np.ones((480, 640, 3), dtype=np.uint8) * 200  # Light gray
        # Add a "phone" (dark rectangle)
        cv2.rectangle(img, (200, 150), (440, 330), (50, 50, 50), -1)
        cv2.imwrite(str(output_path / f"slot{i}_phone.jpg"), img)

    # Create different phone images (different position/color)
    for i in range(3):
        img = np.ones((480, 640, 3), dtype=np.uint8) * 200
        # Different phone position
        cv2.rectangle(img, (180, 140), (460, 340), (60, 60, 60), -1)
        cv2.imwrite(str(output_path / f"slot{i}_phone_different.jpg"), img)

    logger.info("Test images created successfully")


def main():
    """Run the complete test suite"""

    # Create test images if they don't exist
    if not Path("./test_images").exists():
        create_test_images()

    # Initialize test system
    test_system = TestMonitoringSystem()

    # ==========================================
    # TEST 1: Normal Operation (No Changes)
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 1: Normal Operation (No Changes)" + "\n")

    scenario1 = {
        "slots": {
            0: {"baseline": "slot0_phone.jpg", "test": "slot0_phone.jpg"},
            1: {"baseline": "slot1_phone.jpg", "test": "slot1_phone.jpg"},
            2: {"baseline": "slot2_empty.jpg", "test": "slot2_empty.jpg"},
        }
    }

    test_system.setup_test_scenario(scenario1)

    test_images1 = {
        0: "slot0_phone.jpg",
        1: "slot1_phone.jpg",
        2: "slot2_empty.jpg"
    }

    pid_map = {0: 101, 1: 102, 2: 103}

    test_system.run_monitoring_cycle(test_images1, pid_map)
    test_system.print_alarm_status()

    # ==========================================
    # TEST 2: Phone Removed (Should Trigger Alarm)
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 2: Phone Removed (Should Trigger Alarm)" + "\n")

    test_images2 = {
        0: "slot0_phone.jpg",
        1: "slot1_empty.jpg",  # Phone removed!
        2: "slot2_empty.jpg"
    }

    # Wait for grace period to expire
    time.sleep(test_system.grace_period + 1)

    test_system.run_monitoring_cycle(test_images2, pid_map)
    test_system.print_alarm_status()

    # ==========================================
    # TEST 3: Phone Added (Should Trigger Alarm)
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 3: Phone Added to Empty Slot (Should Trigger Alarm)" + "\n")

    test_images3 = {
        0: "slot0_phone.jpg",
        1: "slot1_empty.jpg",
        2: "slot2_phone.jpg"  # Phone added to empty slot!
    }

    time.sleep(test_system.grace_period + 1)

    test_system.run_monitoring_cycle(test_images3, pid_map)
    test_system.print_alarm_status()

    # ==========================================
    # TEST 4: Admin Clear Alarms
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 4: Admin Clear Alarms" + "\n")

    result = test_system.alarm.authenticate_admin("admin")
    logger.info(f"Admin auth result: {result}")

    test_system.alarm.clear()
    test_system.print_alarm_status()

    # ==========================================
    # TEST 5: Baseline Adaptation
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 5: Baseline Adaptation (Lighting Change)" + "\n")

    # Simulate gradual lighting change by using slightly different images
    slot = test_system.slots[0]

    for i in range(6):
        test_images_adapt = {0: "slot0_phone_different.jpg"}
        test_system.run_monitoring_cycle(test_images_adapt, {0: 101})

        if slot.last_dist > test_system.recalc_threshold:
            logger.info(f"Cycle {i + 1}: Distance {slot.last_dist:.4f} - may trigger recalc")

        time.sleep(0.5)

    logger.info("\n✅ ALL TESTS COMPLETE")


if __name__ == "__main__":
    main()