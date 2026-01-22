#!/usr/bin/env python3
"""
Full system calibration script.
Captures baselines for ALL slots (occupied and empty).
Run this during initial setup.
"""

import logging
import time
import numpy as np
from pathlib import Path
from typing import Dict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def calibrate_all_slots(camera_embedder, db, rois: Dict[int, tuple]):
    """
    Calibrate baselines for ALL slots in the system.

    Args:
        camera_embedder: Embedder instance that can compute(lid)
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


def recalibrate_specific_slots(camera_embedder, db, slot_lids: list):
    """
    Recalibrate specific slots (for maintenance or after phone operations).

    Args:
        camera_embedder: Embedder instance
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


def verify_calibration(camera_embedder, db, rois: Dict[int, tuple]):
    """
    Verify that all slots have valid baselines and check current distances.

    Args:
        camera_embedder: Embedder instance
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
            dist = float(1.0 - np.dot(current_emb, baseline))

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
    # Example usage - you need to implement camera_embedder

    print("=" * 70)
    print("SLOT CALIBRATION SCRIPT")
    print("=" * 70)
    print("")
    print("This script will calibrate baselines for your monitoring system.")
    print("")
    print("Options:")
    print("  1. Calibrate all slots (initial setup)")
    print("  2. Recalibrate specific slots (maintenance)")
    print("  3. Verify current calibration")
    print("  4. Exit")
    print("")

    choice = input("Enter choice (1-4): ")

    if choice not in ['1', '2', '3']:
        print("Exiting...")
        exit(0)

    # Setup (you need to implement these)
    print("\nInitializing camera and database...")

    # from slot_camera import SlotCamera, generate_grid_rois
    # from camera_embedder import CameraEmbedder
    # from db_interface import SlotMonitorDB

    # rois = generate_grid_rois(1920, 1080, rows=4, cols=5, spacing=10)
    # camera = SlotCamera(0, rois)
    # embedder = CameraEmbedder(camera)
    # db = SlotMonitorDB()

    print("⚠️  You need to uncomment and configure the camera setup above")
    print("See IMPLEMENTATION_GUIDE.md for details")

    # if choice == '1':
    #     calibrate_all_slots(embedder, db, rois)
    # elif choice == '2':
    #     slot_ids = input("Enter slot IDs to recalibrate (comma-separated): ")
    #     lids = [int(x.strip()) for x in slot_ids.split(',')]
    #     recalibrate_specific_slots(embedder, db, lids)
    # elif choice == '3':
    #     verify_calibration(embedder, db, rois)