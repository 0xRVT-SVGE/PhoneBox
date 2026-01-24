#!/usr/bin/env python3
"""
Quick test suite for phone monitoring system using photos.
Uses actual database and system modules - NO custom DB helpers.
"""

import os
import sys
import time
import logging
import numpy as np
from pathlib import Path
from typing import Dict, Optional
import cv2

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import actual system modules
from slot_state import SlotState
from alarm_controller import AlarmController
from slot_embed import compute_embedding, embedding_distance
from db_interface import SlotMonitorDB


class PhotoEmbedder:
    """Embedder that uses photos from a directory"""

    def __init__(self, image_dir: str):
        self.image_dir = Path(image_dir)
        self.current_images: Dict[int, str] = {}

    def set_image(self, lid: int, image_name: str):
        """Set the current image for a slot"""
        self.current_images[lid] = image_name

    def compute(self, lid: int) -> np.ndarray:
        """Compute embedding for a slot from its current image"""
        if lid not in self.current_images:
            raise ValueError(f"No image set for slot {lid}")

        img_path = self.image_dir / self.current_images[lid]
        if not img_path.exists():
            raise FileNotFoundError(f"Image not found: {img_path}")

        img = cv2.imread(str(img_path))
        if img is None:
            raise ValueError(f"Failed to read image: {img_path}")

        return compute_embedding(img)


class QuickTestSystem:
    """Quick test system using photos and real database"""

    def __init__(self, image_dir: str = "./test_images"):
        self.image_dir = Path(image_dir)
        self.embedder = PhotoEmbedder(image_dir)
        self.db = SlotMonitorDB()
        self.alarm = AlarmController()
        self.slots: Dict[int, SlotState] = {}

        # Test configuration (more sensitive for photos)
        self.mismatch_threshold = 0.03
        self.recalc_threshold = 0.0015
        self.grace_period = 0.0  # Instant for testing

        logger.info(f"Test system initialized")
        logger.info(f"  Image dir: {image_dir}")
        logger.info(f"  Mismatch threshold: {self.mismatch_threshold}")
        logger.info(f"  Grace period: {self.grace_period}s")

    def calibrate_from_images(self, slot_configs: Dict[int, Dict]):
        """
        Calibrate baselines from images.

        IMPORTANT: Before running this, ensure:
        1. Locations exist in database (INSERT INTO locations)
        2. Phones exist in database (INSERT INTO phones)
        3. Phone storage records exist for occupied slots (INSERT INTO phone_storage)

        slot_configs = {
            0: {"image": "slot0_empty.jpg"},
            1: {"image": "slot1_phone.jpg"},
            ...
        }
        """
        logger.info("=" * 70)
        logger.info("CALIBRATING BASELINES FROM IMAGES")
        logger.info("=" * 70)
        logger.info("NOTE: Database must already have locations, phones, and phone_storage set up!")
        logger.info("")

        for lid, config in slot_configs.items():
            image = config["image"]

            logger.info(f"Calibrating slot {lid}: {image}")

            # Set image and compute embedding
            self.embedder.set_image(lid, image)
            baseline = self.embedder.compute(lid)

            # Save baseline to DB (using existing db_interface method)
            self.db.save_baseline(lid, baseline)

        logger.info(f"✅ Calibrated {len(slot_configs)} slots")

    def initialize_monitoring(self):
        """Initialize monitoring from DB (like real system)"""
        logger.info("\n" + "=" * 70)
        logger.info("INITIALIZING MONITORING FROM DATABASE")
        logger.info("=" * 70)

        # Fetch from DB (using existing db_interface methods)
        occupied_slots = self.db.fetch_occupied_slots()
        occupied_lids = {lid: pid for lid, pid in occupied_slots}

        baselines = self.db.fetch_all_baselines()

        logger.info(f"Found {len(occupied_lids)} occupied slots in DB")
        logger.info(f"Found {len(baselines)} baselines in DB")

        # Initialize slot states
        for lid, baseline in baselines.items():
            is_occupied = lid in occupied_lids

            self.slots[lid] = SlotState(
                lid=lid,
                baseline_emb=baseline,
                is_occupied=is_occupied
            )

            status = "OCCUPIED" if is_occupied else "EMPTY"
            pid = occupied_lids.get(lid, "N/A")
            logger.info(f"  Slot {lid}: {status}" + (f" (PID: {pid})" if is_occupied else ""))

        logger.info(f"✅ Initialized {len(self.slots)} slots")

    def run_monitoring_cycle(self, test_images: Dict[int, str]):
        """Run one monitoring cycle with test images"""
        logger.info("\n" + "=" * 70)
        logger.info("RUNNING MONITORING CYCLE")
        logger.info("=" * 70)

        for lid, img_name in test_images.items():
            if lid not in self.slots:
                logger.warning(f"Slot {lid} not initialized, skipping")
                continue

            slot = self.slots[lid]

            # Set current image
            self.embedder.set_image(lid, img_name)

            # Compute embedding
            current_emb = self.embedder.compute(lid)

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
            logger.info(f"  Distance: {dist:.6f}")
            logger.info(f"  Occupied: {slot.is_occupied}")
            logger.info(f"  Mismatch: {slot.mismatch}")
            logger.info(f"  Trigger: {result['trigger_alarm']}")
            logger.info(f"  Stop: {result['stop_alarm']}")
            logger.info(f"  Recalc: {result['needs_recalc']}")

            # Handle alarms (using existing db_interface method)
            pid = self.db.get_pid_for_lid(lid) or f"unknown-{lid}"

            if result["trigger_alarm"]:
                self.alarm.trigger(pid, lid)
                logger.warning(f"  🚨 ALARM TRIGGERED!")

            if result["stop_alarm"]:
                any_mismatch = any(s.mismatch for s in self.slots.values())
                self.alarm.stop_if_clear(any_mismatch)

            if result["needs_recalc"]:
                logger.info(f"  🔄 Baseline adaptation triggered")
                slot.adapt_baseline(current_emb)
                self.db.save_baseline(lid, current_emb)

    def print_alarm_status(self):
        """Print alarm status"""
        status = self.alarm.get_status()

        logger.info("\n" + "=" * 70)
        logger.info("ALARM STATUS")
        logger.info("=" * 70)
        logger.info(f"Active: {status['active']}")
        logger.info(f"Mismatch count: {status['mismatch_count']}")
        logger.info(f"Duration: {status['duration']:.1f}s")

        if status['mismatch_count'] > 0:
            logger.info("\nMismatched slots:")
            for pid, lid in sorted(self.alarm.mismatches):
                logger.info(f"  PID={pid}, LID={lid}")

    def admin_clear(self):
        """Admin clear alarms"""
        logger.info("\n" + "=" * 70)
        logger.info("ADMIN CLEAR")
        logger.info("=" * 70)

        result = self.alarm.authenticate_admin("admin")
        logger.info(f"Auth result: {result}")

        self.alarm.clear()
        logger.info("✅ Alarms cleared")


def create_test_images(output_dir: str = "./test_images"):
    """Create sample test images"""
    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)

    logger.info(f"Creating test images in {output_dir}")

    # Empty slots
    for i in range(3):
        img = np.ones((480, 640, 3), dtype=np.uint8) * 200
        cv2.imwrite(str(output_path / f"slot{i}_empty.jpg"), img)

    # Occupied slots
    for i in range(3):
        img = np.ones((480, 640, 3), dtype=np.uint8) * 200
        cv2.rectangle(img, (200, 150), (440, 330), (50, 50, 50), -1)
        cv2.imwrite(str(output_path / f"slot{i}_phone.jpg"), img)

    # Different phone
    for i in range(3):
        img = np.ones((480, 640, 3), dtype=np.uint8) * 200
        cv2.rectangle(img, (180, 140), (460, 340), (60, 60, 60), -1)
        cv2.imwrite(str(output_path / f"slot{i}_phone_different.jpg"), img)

    logger.info("✅ Test images created")


def print_setup_instructions():
    """Print instructions for setting up test data in database"""
    print("\n" + "=" * 70)
    print("DATABASE SETUP REQUIRED")
    print("=" * 70)
    print("\nBefore running calibration, ensure your database has:")
    print("\n1. Test student:")
    print("   INSERT INTO students (sid, last_name, first_name, embed)")
    print("   VALUES ('E0001', 'Test', 'Student', ARRAY[0.0]);")
    print("\n2. Test locations:")
    print("   INSERT INTO locations (lid, x, y) VALUES (0, 1, 1);")
    print("   INSERT INTO locations (lid, x, y) VALUES (1, 2, 1);")
    print("   INSERT INTO locations (lid, x, y) VALUES (2, 3, 1);")
    print("\n3. Test phones:")
    print("   INSERT INTO phones (pid, sid, model, imei) VALUES")
    print("   ('87246c44-84bf-4112-a347-d6fe30c18d15', 'E0001', 'Test Phone 1', 'TEST001');")
    print("   INSERT INTO phones (pid, sid, model, imei) VALUES")
    print("   ('9f74aca1-556e-4212-aafd-5f1f48db319a', 'E0001', 'Test Phone 2', 'TEST002');")
    print("\n4. Phone storage (for occupied slots):")
    print("   INSERT INTO phone_storage (pid, lid, stored_at) VALUES")
    print("   ('87246c44-84bf-4112-a347-d6fe30c18d15', 0, NOW());")
    print("   INSERT INTO phone_storage (pid, lid, stored_at) VALUES")
    print("   ('9f74aca1-556e-4212-aafd-5f1f48db319a', 1, NOW());")
    print("\n5. Then run calibration to save baselines")
    print("=" * 70 + "\n")


def main():
    """Main test program"""

    # Create test images if needed
    if not Path("./test_images").exists():
        create_test_images()

    # Initialize test system
    test = QuickTestSystem()

    # Check if we need calibration
    baselines = test.db.fetch_all_baselines()

    if not baselines:
        logger.info("\n" + "🔧 NO BASELINES FOUND IN DATABASE" + "\n")
        print_setup_instructions()

        response = input("Have you set up the database? (yes/no): ")
        if response.lower() != 'yes':
            logger.info("Please set up database first. Exiting...")
            return

        # Run calibration with images
        slot_configs = {
            0: {"image": "slot0_phone.jpg"},
            1: {"image": "slot1_phone.jpg"},
            2: {"image": "slot2_empty.jpg"}
        }

        test.calibrate_from_images(slot_configs)
    else:
        logger.info(f"\n✅ Found {len(baselines)} baselines in database\n")

    # Initialize monitoring
    test.initialize_monitoring()

    # ==========================================
    # TEST 1: Normal Operation
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 1: Normal Operation (No Changes)")

    test.run_monitoring_cycle({
        0: "slot0_phone.jpg",
        1: "slot1_phone.jpg",
        2: "slot2_empty.jpg"
    })

    test.print_alarm_status()

    # ==========================================
    # TEST 2: Phone Removed
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 2: Phone Removed from Occupied Slot")

    test.run_monitoring_cycle({
        0: "slot0_phone.jpg",
        1: "slot1_empty.jpg",  # Phone removed!
        2: "slot2_empty.jpg"
    })

    test.print_alarm_status()

    # ==========================================
    # TEST 3: Phone Added
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 3: Phone Added to Empty Slot")

    test.run_monitoring_cycle({
        0: "slot0_phone.jpg",
        1: "slot1_empty.jpg",
        2: "slot2_phone.jpg"  # Phone added!
    })

    test.print_alarm_status()

    # ==========================================
    # TEST 4: Admin Clear
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 4: Admin Clear Alarms")

    test.admin_clear()
    test.print_alarm_status()

    # ==========================================
    # TEST 5: Baseline Adaptation
    # ==========================================
    logger.info("\n\n" + "🧪 TEST 5: Baseline Adaptation")

    slot = test.slots[0]

    for i in range(6):
        test.run_monitoring_cycle({0: "slot0_phone_different.jpg"})

        if slot.last_dist > test.recalc_threshold:
            logger.info(f"  Cycle {i + 1}: Distance {slot.last_dist:.6f} - may adapt")

    logger.info("\n✅ ALL TESTS COMPLETE")

    # Final summary
    logger.info("\n" + "=" * 70)
    logger.info("TEST SUMMARY")
    logger.info("=" * 70)
    logger.info(f"Slots monitored: {len(test.slots)}")
    logger.info(f"Baselines in DB: {len(test.db.fetch_all_baselines())}")
    logger.info(f"Occupied slots in DB: {len(test.db.fetch_occupied_slots())}")
    logger.info("\nTo recalibrate:")
    logger.info("  Run SQL: DELETE FROM slot_baselines;")
    logger.info("  Then run this script again")


if __name__ == "__main__":
    main()