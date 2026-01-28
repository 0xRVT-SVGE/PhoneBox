#!/usr/bin/env python3
"""
Camera-based test system for phone monitoring.
Uses refactored slot.py architecture with unified Slot class.
Tests real-time camera capture and multi-threaded slot processing.
"""

import os
import sys
import time
import logging
import threading
import numpy as np
from pathlib import Path
from typing import Dict, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import refactored modules
from slot_camera import SharedFrameBuffer, CameraCapture
from slots import Slot, generate_grid_rois
from alarm_controller import AlarmController
from db_interface import SlotMonitorDB


class MonitorWorker:
    """Worker thread for continuous monitoring of assigned slots"""

    def __init__(
        self,
        worker_id: int,
        slots: list[Slot],
        frame_buffer: SharedFrameBuffer,
        db: SlotMonitorDB,
        alarm: AlarmController,
        mismatch_threshold: float,
        recalc_threshold: float,
        grace_period: float,
        interval: float,
    ):
        self.worker_id = worker_id
        self.slots = slots  # List of Slot objects assigned to this worker
        self.frame_buffer = frame_buffer
        self.db = db
        self.alarm = alarm
        self.mismatch_threshold = mismatch_threshold
        self.recalc_threshold = recalc_threshold
        self.grace_period = grace_period
        self.interval = interval

        self._stop_event = threading.Event()
        self._thread = None
        self.cycle_count = 0
        self.total_distance = 0.0

        slot_ids = [s.lid for s in slots]
        logger.info(
            f"Worker {worker_id} initialized with {len(slots)} slots: {slot_ids}"
        )

    def start(self):
        """Start worker thread"""
        self._thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name=f"MonitorWorker-{self.worker_id}"
        )
        self._thread.start()
        logger.info(f"Worker {self.worker_id} started")

    def stop(self):
        """Stop worker thread"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        logger.info(f"Worker {self.worker_id} stopped")

    def _monitor_loop(self):
        """Main monitoring loop for this worker"""
        next_tick = time.time()

        while not self._stop_event.is_set():
            cycle_start = time.time()

            try:
                self._run_cycle()
            except Exception as e:
                logger.error(f"Worker {self.worker_id} error: {e}", exc_info=True)

            # Fixed interval scheduling
            next_tick += self.interval
            sleep_time = max(0, next_tick - time.time())

            self.cycle_count += 1

            # Log stats every 10 cycles
            if self.cycle_count % 10 == 0:
                avg_dist = (
                    self.total_distance / (self.cycle_count * len(self.slots))
                    if self.cycle_count > 0 else 0.0
                )
                logger.info(
                    f"Worker {self.worker_id}: {self.cycle_count} cycles, "
                    f"avg_dist={avg_dist:.4f}"
                )

            if sleep_time > 0:
                time.sleep(sleep_time)

    def _run_cycle(self):
        """Process all assigned slots in one cycle"""
        # Get frame once for this cycle
        frame = self.frame_buffer.get_frame()
        if frame is None:
            logger.warning(f"Worker {self.worker_id}: No frame available")
            return

        for slot in self.slots:
            try:
                self._process_slot(slot, frame)
            except Exception as e:
                logger.error(
                    f"Worker {self.worker_id} failed slot {slot.lid}: {e}"
                )

    def _process_slot(self, slot: Slot, frame: np.ndarray):
        """Process a single slot"""
        # Use unified Slot.update() method
        result = slot.update(
            frame=frame,
            mismatch_threshold=self.mismatch_threshold,
            recalc_threshold=self.recalc_threshold,
            grace_period=self.grace_period,
        )

        dist = result["distance"]
        self.total_distance += dist

        # Handle alarms
        pid = self.db.get_pid_for_lid(slot.lid) or f"unknown-{slot.lid}"

        if result["trigger_alarm"]:
            self.alarm.trigger(pid, slot.lid)
            logger.critical(
                f"Worker {self.worker_id}: ALARM! LID={slot.lid}, "
                f"PID={pid}, dist={dist:.4f}"
            )

        if result["stop_alarm"]:
            # Check if any slots still have mismatches
            any_mismatch = any(s.mismatch for s in self.slots)
            self.alarm.stop_if_clear(any_mismatch)
            logger.info(
                f"Worker {self.worker_id}: Alarm cleared for LID={slot.lid}"
            )

        if result["needs_recalc"]:
            logger.info(
                f"Worker {self.worker_id}: Baseline adaptation for LID={slot.lid}, "
                f"dist={dist:.4f}"
            )
            slot.adapt_baseline(result["embedding"])
            self.db.save_baseline(slot.lid, result["embedding"])


class CameraTestSystem:
    """Camera-based test system with parallel monitoring"""

    def __init__(
        self,
        camera_id: int = 0,
        camera_width: int = 1920,
        camera_height: int = 1080,
        grid_rows: int = 2,
        grid_cols: int = 3,
        num_workers: int = 2,
        mismatch_threshold: float = 0.35,
        recalc_threshold: float = 0.12,
        grace_period: float = 15.0,
        monitor_interval: float = 5.0,
    ):
        # Camera configuration
        self.camera_id = camera_id
        self.camera_width = camera_width
        self.camera_height = camera_height
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols

        # Monitoring configuration
        self.num_workers = num_workers
        self.mismatch_threshold = mismatch_threshold
        self.recalc_threshold = recalc_threshold
        self.grace_period = grace_period
        self.monitor_interval = monitor_interval

        # System components
        self.frame_buffer = SharedFrameBuffer()
        self.camera = None
        self.db = SlotMonitorDB()
        self.alarm = AlarmController()
        self.slots: Dict[int, Slot] = {}  # lid -> Slot
        self.workers = []

        logger.info("=" * 70)
        logger.info("CAMERA TEST SYSTEM INITIALIZED")
        logger.info("=" * 70)
        logger.info(f"Camera: {camera_id} ({camera_width}x{camera_height})")
        logger.info(f"Grid: {grid_rows}x{grid_cols} = {grid_rows * grid_cols} slots")
        logger.info(f"Workers: {num_workers}")
        logger.info(f"Mismatch threshold: {mismatch_threshold}")
        logger.info(f"Recalc threshold: {recalc_threshold}")
        logger.info(f"Grace period: {grace_period}s")
        logger.info(f"Monitor interval: {monitor_interval}s")

    def start_camera(self):
        """Initialize and start camera capture"""
        logger.info("\n" + "=" * 70)
        logger.info("STARTING CAMERA CAPTURE")
        logger.info("=" * 70)

        # Generate ROIs for grid
        rois = generate_grid_rois(
            frame_width=self.camera_width,
            frame_height=self.camera_height,
            rows=self.grid_rows,
            cols=self.grid_cols,
            spacing=10,
        )

        logger.info(f"Generated {len(rois)} ROIs")
        for lid, (x, y, w, h) in sorted(rois.items()):
            logger.info(f"  Slot {lid}: x={x}, y={y}, w={w}, h={h}")

        # Start frame buffer capture
        self.frame_buffer.start_capture(
            camera_id=self.camera_id,
            width=self.camera_width,
            height=self.camera_height,
        )

        # Create camera interface
        self.camera = CameraCapture(
            frame_buffer=self.frame_buffer,
            camera_id=self.camera_id,
            width=self.camera_width,
            height=self.camera_height,
        )

        # Store ROIs for later use
        self.rois = rois

        logger.info("✅ Camera system ready")

    def check_baselines(self):
        """
        Check existing baselines against current camera state.
        Detects changes while system was offline.
        Updates baselines to current state.
        """
        logger.info("\n" + "=" * 70)
        logger.info("CHECKING BASELINES")
        logger.info("=" * 70)

        baselines = self.db.fetch_all_baselines()

        if not baselines:
            logger.warning("⚠️  No baselines found in database")
            logger.info(
                "Run calibration first or system will initialize from current state"
            )
            return

        logger.info(f"Found {len(baselines)} baselines in database")

        # Get current frame
        frame = self.frame_buffer.get_frame()
        if frame is None:
            logger.error("Failed to get frame for baseline check")
            return

        mismatches = 0
        for lid, baseline in baselines.items():
            if lid not in self.rois:
                logger.warning(f"Slot {lid} has baseline but no ROI defined")
                continue

            try:
                # Create temporary slot for comparison
                temp_slot = Slot(
                    lid=lid,
                    roi_coords=self.rois[lid],
                    baseline_emb=baseline,
                    is_occupied=False,
                )

                # Compute distance
                dist = temp_slot.compute_distance(frame)

                if dist > self.mismatch_threshold:
                    logger.warning(
                        f"⚠️  Slot {lid}: MISMATCH DETECTED! dist={dist:.4f} "
                        f"(threshold={self.mismatch_threshold})"
                    )
                    logger.warning(
                        f"    Slot state changed while system was offline!"
                    )
                    mismatches += 1
                else:
                    logger.info(f"✓  Slot {lid}: OK (dist={dist:.4f})")

                # Update baseline to current state
                current_emb = temp_slot.compute_embedding(frame)
                self.db.save_baseline(lid, current_emb)
                logger.info(f"    → Baseline updated to current state")

            except Exception as e:
                logger.error(f"Failed to check baseline for slot {lid}: {e}")

        logger.info("")
        if mismatches > 0:
            logger.warning(
                f"⚠️  {mismatches}/{len(baselines)} slots had mismatches"
            )
            logger.warning(
                f"    Baselines updated - system will monitor from current state"
            )
        else:
            logger.info(f"✅ All {len(baselines)} baselines match current state")

    def initialize_monitoring(self):
        """Initialize slot states from database"""
        logger.info("\n" + "=" * 70)
        logger.info("INITIALIZING MONITORING")
        logger.info("=" * 70)

        # Fetch occupied slots
        occupied_slots = self.db.fetch_occupied_slots()
        occupied_lids = {lid: pid for lid, pid in occupied_slots}

        # Fetch baselines
        baselines = self.db.fetch_all_baselines()

        logger.info(f"Found {len(occupied_lids)} occupied slots in DB")
        logger.info(f"Found {len(baselines)} baselines in DB")

        # Initialize Slot objects
        for lid, baseline in baselines.items():
            if lid not in self.rois:
                logger.warning(f"Slot {lid} has baseline but no ROI defined")
                continue

            is_occupied = lid in occupied_lids

            # Create unified Slot object
            self.slots[lid] = Slot(
                lid=lid,
                roi_coords=self.rois[lid],
                baseline_emb=baseline,
                is_occupied=is_occupied,
            )

            status = "OCCUPIED" if is_occupied else "EMPTY"
            pid = occupied_lids.get(lid, "N/A")
            logger.info(
                f"  Slot {lid}: {status}" +
                (f" (PID: {pid})" if is_occupied else "")
            )

        logger.info(f"✅ Initialized {len(self.slots)} slots")

    def start_monitoring(self):
        """Start parallel monitoring workers"""
        logger.info("\n" + "=" * 70)
        logger.info("STARTING MONITORING WORKERS")
        logger.info("=" * 70)

        if not self.slots:
            logger.error("No slots initialized!")
            return

        # Distribute slots across workers
        slot_list = sorted(self.slots.values(), key=lambda s: s.lid)
        slots_per_worker = len(slot_list) // self.num_workers
        remainder = len(slot_list) % self.num_workers

        start_idx = 0
        for i in range(self.num_workers):
            # Distribute remainder slots evenly
            count = slots_per_worker + (1 if i < remainder else 0)
            end_idx = start_idx + count
            worker_slots = slot_list[start_idx:end_idx]

            worker = MonitorWorker(
                worker_id=i,
                slots=worker_slots,
                frame_buffer=self.frame_buffer,
                db=self.db,
                alarm=self.alarm,
                mismatch_threshold=self.mismatch_threshold,
                recalc_threshold=self.recalc_threshold,
                grace_period=self.grace_period,
                interval=self.monitor_interval,
            )
            worker.start()
            self.workers.append(worker)

            start_idx = end_idx

        logger.info(f"✅ Started {len(self.workers)} monitoring workers")

    def stop_monitoring(self):
        """Stop all monitoring workers"""
        logger.info("\n" + "=" * 70)
        logger.info("STOPPING MONITORING")
        logger.info("=" * 70)

        for worker in self.workers:
            worker.stop()

        self.workers.clear()
        logger.info("✅ All workers stopped")

    def stop_camera(self):
        """Stop camera capture"""
        logger.info("\n" + "=" * 70)
        logger.info("STOPPING CAMERA")
        logger.info("=" * 70)

        self.frame_buffer.stop_capture()
        logger.info("✅ Camera stopped")

    def print_status(self):
        """Print current system status"""
        logger.info("\n" + "=" * 70)
        logger.info("SYSTEM STATUS")
        logger.info("=" * 70)

        # Camera stats
        frame_count = self.frame_buffer.get_frame_count()
        logger.info(f"Frames captured: {frame_count}")

        # Slot stats
        total = len(self.slots)
        mismatched = sum(1 for s in self.slots.values() if s.mismatch)
        logger.info(f"Total slots: {total}")
        logger.info(f"Mismatched: {mismatched}")

        # Worker stats
        for worker in self.workers:
            logger.info(
                f"Worker {worker.worker_id}: {worker.cycle_count} cycles, "
                f"{len(worker.slots)} slots"
            )

        # Alarm status
        alarm_status = self.alarm.get_status()
        logger.info(f"\nAlarm active: {alarm_status['active']}")
        logger.info(f"Mismatch count: {alarm_status['mismatch_count']}")
        logger.info(f"Duration: {alarm_status['duration']:.1f}s")

        if alarm_status['mismatch_count'] > 0:
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


def print_instructions():
    """Print usage instructions"""
    print("\n" + "=" * 70)
    print("CAMERA TEST SYSTEM - REFACTORED ARCHITECTURE")
    print("=" * 70)
    print("\nNew Architecture:")
    print("  slot.py      → Unified Slot class (state + ROI + logic)")
    print("  camera.py    → Camera hardware abstraction")
    print("  embedding.py → Embedding algorithms")
    print("\nThis test requires:")
    print("\n1. Working camera connected (default: camera 0)")
    print("\n2. Database setup with:")
    print("   - locations table (for slot positions)")
    print("   - phones table (for device tracking)")
    print("   - phone_storage table (for occupied slots)")
    print("   - slot_baselines table (created automatically)")
    print("\n3. Initial calibration:")
    print("   - System will check baselines on startup")
    print("   - Warns about offline changes")
    print("   - Updates baselines to current state")
    print("\nMonitoring:")
    print("   - Multiple worker threads process slots in parallel")
    print("   - Each Slot is self-contained with ROI coords")
    print("   - Zero redundant lookups or bounds checks")
    print("   - Alarms trigger on mismatches (phone removed/swapped)")
    print("   - Baselines adapt to minor drift")
    print("\nPress Ctrl+C to stop monitoring")
    print("=" * 70 + "\n")


def main():
    """Main test program"""
    print_instructions()

    # Configuration
    CAMERA_ID = 0
    CAMERA_WIDTH = 1280
    CAMERA_HEIGHT = 720
    GRID_ROWS = 2
    GRID_COLS = 3
    NUM_WORKERS = 2  # ← Adjust this to change parallel worker count
    MISMATCH_THRESHOLD = 0.35
    RECALC_THRESHOLD = 0.12
    GRACE_PERIOD = 5.0
    MONITOR_INTERVAL = 1.0

    # Create test system
    test = CameraTestSystem(
        camera_id=CAMERA_ID,
        camera_width=CAMERA_WIDTH,
        camera_height=CAMERA_HEIGHT,
        grid_rows=GRID_ROWS,
        grid_cols=GRID_COLS,
        num_workers=NUM_WORKERS,
        mismatch_threshold=MISMATCH_THRESHOLD,
        recalc_threshold=RECALC_THRESHOLD,
        grace_period=GRACE_PERIOD,
        monitor_interval=MONITOR_INTERVAL,
    )

    try:
        # Start camera
        test.start_camera()

        # Check baselines (detects offline changes)
        test.check_baselines()

        # Initialize from DB
        test.initialize_monitoring()

        # Start monitoring
        test.start_monitoring()

        logger.info("\n" + "=" * 70)
        logger.info("MONITORING ACTIVE - Press Ctrl+C to stop")
        logger.info("=" * 70)

        # Run indefinitely
        while True:
            time.sleep(10)
            test.print_status()

    except KeyboardInterrupt:
        logger.info("\n\n⏹️  Keyboard interrupt received")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)

    finally:
        # Cleanup
        logger.info("\n" + "=" * 70)
        logger.info("SHUTTING DOWN")
        logger.info("=" * 70)

        test.stop_monitoring()
        test.stop_camera()
        test.print_status()

        logger.info("\n✅ Test complete")


if __name__ == "__main__":
    main()