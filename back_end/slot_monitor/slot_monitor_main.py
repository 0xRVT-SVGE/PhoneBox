# ============================================================
# FILE: server/slot_monitor/slot_monitor_main.py
# ============================================================

import time
import threading
import logging
from typing import Dict
import numpy as np

from .slot_camera import SlotCamera
from .slot_embed import compute_embedding, embedding_distance
from .slot_state import SlotState, BinaryState, TxState
from .db_interface import SlotMonitorDB

logger = logging.getLogger(__name__)

# Thresholds (can be configured)
T_MINOR = 0.15  # OK threshold
T_MAJOR = 0.35  # ALTERED/EMPTY threshold
T_RECALC = 0.12  # Baseline recalculation threshold
CHECK_INTERVAL = 5.0  # Check every 5 seconds


class SlotMonitor(threading.Thread):
    """Main slot monitoring thread"""

    def __init__(self, cam_index: int, rois: Dict[int, tuple], grace_period: float = 15.0):
        """
        Initialize slot monitor.

        Args:
            cam_index: Camera device index
            rois: Dictionary mapping lid -> (x, y, w, h)
            grace_period: Seconds to wait before alarming on UNKNOWN state
        """
        super().__init__(daemon=True)
        self.name = "SlotMonitor"

        self.camera = SlotCamera(cam_index, rois)
        self.db = SlotMonitorDB()
        self.grace_period = grace_period

        # Load baselines from database
        baseline_map = self.db.load_baselines()

        # Initialize slot states
        self.slots: Dict[int, SlotState] = {}
        for lid in rois.keys():
            if lid in baseline_map:
                self.slots[lid] = SlotState(lid, baseline_map[lid])
            else:
                logger.warning(f"No baseline for slot {lid}, will create on first check")

        self.running = True
        self.paused_slots = set()  # Slots to skip (during deposit/withdrawal)
        self.last_check_time = 0
        self.check_counter = 0

        logger.info(f"SlotMonitor initialized with {len(self.slots)} slots, grace_period={grace_period}s")

    def pause_slot(self, lid: int):
        """Temporarily pause monitoring for a slot (during operations)"""
        self.paused_slots.add(lid)
        logger.info(f"Slot {lid} monitoring paused")

    def resume_slot(self, lid: int, new_baseline: Optional[np.ndarray] = None):
        """Resume monitoring for a slot, optionally with new baseline"""
        if new_baseline is not None and lid in self.slots:
            self.slots[lid].reset_baseline(new_baseline)
            self.db.save_baseline(lid, new_baseline, 'manual_recalibration')

        self.paused_slots.discard(lid)
        logger.info(f"Slot {lid} monitoring resumed")

    def run(self):
        """Main monitoring loop"""
        logger.info("SlotMonitor thread started")

        while self.running:
            try:
                current_time = time.time()

                # Check every 5 seconds
                if current_time - self.last_check_time < CHECK_INTERVAL:
                    time.sleep(0.5)
                    continue

                self.last_check_time = current_time
                self.check_counter += 1

                # Read frame
                frame = self.camera.read()
                if frame is None:
                    self.db.log_system_error(
                        'camera_read_failure',
                        'Failed to read frame from camera',
                        'error'
                    )
                    time.sleep(1.0)
                    continue

                # Extract ROIs
                try:
                    rois = self.camera.extract_rois(frame)
                except Exception as e:
                    logger.error(f"ROI extraction failed: {e}")
                    self.db.log_system_error(
                        'roi_extraction_failure',
                        str(e),
                        'error'
                    )
                    continue

                # Process each slot
                for lid, roi in rois.items():
                    # Skip paused slots
                    if lid in self.paused_slots:
                        continue

                    try:
                        self._process_slot(lid, roi)
                    except Exception as e:
                        logger.error(f"Error processing slot {lid}: {e}")

                # Log health metrics every 10 checks (~50 seconds)
                if self.check_counter % 10 == 0:
                    self._log_system_health()

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                time.sleep(1.0)

        logger.info("SlotMonitor thread stopped")

    def _process_slot(self, lid: int, roi: np.ndarray):
        """Process a single slot"""
        # Skip if no slot state (no baseline)
        if lid not in self.slots:
            return

        slot = self.slots[lid]

        # Compute embedding
        try:
            emb = compute_embedding(roi)
        except Exception as e:
            logger.error(f"Failed to compute embedding for slot {lid}: {e}")
            return

        # Calculate distance
        dist = embedding_distance(emb, slot.baseline)

        # Update state
        result = slot.update(dist, T_MINOR, T_MAJOR, T_RECALC)

        # Update database
        binary_str = 'OK' if slot.binary_state == BinaryState.OK else 'ALTERED'
        tx_str = slot.tx_state.name

        self.db.update_slot_state(lid, binary_str, tx_str, dist)

        # Log state changes
        if result['state_changed']:
            self.db.log_slot_event(lid, binary_str, tx_str, dist)
            logger.info(f"Slot {lid} state changed: {binary_str}/{tx_str} (dist={dist:.3f})")

        # Recalculate baseline if needed (gradual lighting changes)
        if result['needs_recalc']:
            logger.info(f"Recalculating baseline for slot {lid} (gradual change detected)")
            self.slots[lid].reset_baseline(emb)
            self.db.save_baseline(lid, emb, 'auto_adapt')

        # Check for alarms
        if slot.should_alarm(self.grace_period):
            anomaly_type = 'unexpected_empty' if slot.binary_state == BinaryState.ALTERED else 'stuck_altered'
            severity = 'high' if slot.binary_state == BinaryState.ALTERED else 'medium'

            self.db.log_anomaly(
                lid,
                anomaly_type,
                dist,
                severity,
                f"Slot in {binary_str}/{tx_str} state for >{self.grace_period}s"
            )

    def _log_system_health(self):
        """Log system health metrics"""
        total = len(self.slots)
        altered = sum(1 for s in self.slots.values() if s.binary_state == BinaryState.ALTERED)
        occupied = sum(1 for s in self.slots.values() if s.tx_state == TxState.OCCUPIED)

        distances = [s.last_dist for s in self.slots.values()]
        avg_dist = np.mean(distances) if distances else 0.0
        max_dist = np.max(distances) if distances else 0.0

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_system_health
                            (active_slots, altered_slots, avg_distance, max_distance,
                             camera_uptime_seconds, timestamp)
                            VALUES (%s, %s, %s, %s, %s, NOW());
                            """, (total, altered, avg_dist, max_dist, int(self.camera.frame_count / 30)))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to log system health: {e}")
        finally:
            put_conn(conn)

    def stop(self):
        """Stop monitoring thread"""
        logger.info("Stopping SlotMonitor...")
        self.running = False
        self.camera.release()
