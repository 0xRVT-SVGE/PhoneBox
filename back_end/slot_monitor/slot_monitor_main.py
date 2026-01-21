import time
import threading
import logging
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict

from .alarm_controller import AlarmController
from .slot_state import SlotState

logger = logging.getLogger(__name__)


class SlotMonitor:
    """
    Main monitoring thread with parallel slot processing.
    No DB logic in processing loop - only state management.
    """

    def __init__(
            self,
            db,
            embedder,
            mismatch_threshold: float = 0.35,
            recalc_threshold: float = 0.12,
            grace_period: float = 15.0,
            interval: float = 5.0,
            workers: int = 4,
    ):
        self.db = db
        self.embedder = embedder

        # Thresholds
        self.mismatch_threshold = mismatch_threshold
        self.recalc_threshold = recalc_threshold
        self.grace_period = grace_period
        self.interval = interval

        # Runtime state
        self.slots: Dict[int, SlotState] = {}
        self.alarm = AlarmController()
        self.paused_slots: set[int] = set()

        # Threading
        self._stop_event = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=workers)

        # Metrics
        self.cycle_count = 0
        self.last_cycle_time = 0.0

        logger.info(
            f"SlotMonitor initialized: "
            f"mismatch={mismatch_threshold}, "
            f"recalc={recalc_threshold}, "
            f"grace={grace_period}s, "
            f"interval={interval}s, "
            f"workers={workers}"
        )

    # ------------------------------------------------------------
    # LIFECYCLE
    # ------------------------------------------------------------

    def initialize(self):
        """Initialize monitoring from DB state."""
        occupied = self.db.fetch_occupied_slots()
        # Returns: [(lid, pid, baseline_embed)]

        initialized = 0
        mismatches = 0

        for lid, pid, baseline in occupied:
            try:
                realtime = self.embedder.compute(lid)
                dist = float(np.linalg.norm(realtime - baseline))

                if dist > self.mismatch_threshold:
                    logger.warning(
                        f"Slot {lid} has mismatch on init: dist={dist:.3f} "
                        f"(threshold={self.mismatch_threshold})"
                    )
                    mismatches += 1

                state = SlotState(
                    lid=lid,
                    baseline_emb=realtime,
                    is_occupied=True,
                )

                self.slots[lid] = state
                self.db.save_baseline(lid, realtime)
                initialized += 1

            except Exception as e:
                logger.error(f"Failed to initialize slot {lid}: {e}")

        logger.info(
            f"Initialized {initialized} occupied slots "
            f"({mismatches} with initial mismatches)"
        )

    def start(self):
        """Start monitoring thread."""
        t = threading.Thread(target=self._scheduler_loop, daemon=True, name="SlotMonitor")
        t.start()
        logger.info("SlotMonitor thread started")

    def stop(self):
        """Stop monitoring thread."""
        logger.info("Stopping SlotMonitor...")
        self._stop_event.set()
        self.executor.shutdown(wait=False)
        logger.info("SlotMonitor stopped")

    # ------------------------------------------------------------
    # DEPOSIT / WITHDRAWAL OPERATIONS
    # ------------------------------------------------------------

    def pause_slot(self, lid: int):
        """Pause monitoring for a slot during operations."""
        self.paused_slots.add(lid)
        logger.info(f"Slot {lid} monitoring paused")

    def resume_slot(self, lid: int, baseline: np.ndarray, is_occupied: bool):
        """Resume monitoring with new baseline after operation."""
        self.paused_slots.discard(lid)

        if lid in self.slots:
            self.slots[lid].reset_baseline(baseline)
            self.slots[lid].is_occupied = is_occupied
        else:
            self.slots[lid] = SlotState(
                lid=lid,
                baseline_emb=baseline,
                is_occupied=is_occupied,
            )

        self.db.save_baseline(lid, baseline)
        logger.info(f"Slot {lid} monitoring resumed (occupied={is_occupied})")

    def remove_slot(self, lid: int):
        """Remove slot from monitoring (phone withdrawn, slot now empty)."""
        if lid in self.slots:
            del self.slots[lid]
            logger.info(f"Slot {lid} removed from monitoring")

    # ------------------------------------------------------------
    # MONITORING LOOP
    # ------------------------------------------------------------

    def _scheduler_loop(self):
        """Main monitoring loop with fixed interval."""
        next_tick = time.time()

        while not self._stop_event.is_set():
            cycle_start = time.time()

            try:
                self._run_monitor_cycle()
            except Exception as e:
                logger.error(f"Error in monitor cycle: {e}", exc_info=True)

            # Fixed interval scheduling
            next_tick += self.interval
            sleep_time = max(0, next_tick - time.time())

            self.last_cycle_time = time.time() - cycle_start
            self.cycle_count += 1

            if sleep_time > 0:
                time.sleep(sleep_time)

    def _run_monitor_cycle(self):
        """Run one monitoring cycle across all slots in parallel."""
        if not self.slots:
            return

        futures = []

        # Submit all slot computations
        for lid, state in self.slots.items():
            if lid in self.paused_slots:
                continue

            futures.append(
                self.executor.submit(
                    self._compute_slot,
                    lid,
                    state.baseline,
                )
            )

        # Process results as they complete
        for future in as_completed(futures):
            try:
                lid, realtime, dist = future.result()
                self._apply_result(lid, realtime, dist)
            except Exception as e:
                logger.error(f"Error processing slot result: {e}")

        # Log health metrics periodically
        if self.cycle_count % 10 == 0:
            self._log_health()

    def _compute_slot(self, lid: int, baseline: np.ndarray):
        """Compute embedding and distance for a single slot."""
        realtime = self.embedder.compute(lid)
        dist = float(np.linalg.norm(realtime - baseline))
        return lid, realtime, dist

    def _apply_result(self, lid: int, realtime: np.ndarray, dist: float):
        """Apply monitoring result to slot state."""
        if lid not in self.slots:
            return

        state = self.slots[lid]

        # Update state
        result = state.update_distance(
            dist=dist,
            mismatch_threshold=self.mismatch_threshold,
            recalc_threshold=self.recalc_threshold,
            grace_period=self.grace_period,
        )

        # Get phone ID from DB
        pid = self.db.get_pid_for_lid(lid)

        # Handle alarm triggers
        if result["trigger_alarm"]:
            self.alarm.trigger(pid, lid)
            logger.critical(
                f"MISMATCH ALARM: LID={lid}, PID={pid}, dist={dist:.3f}"
            )

        if result["stop_alarm"]:
            any_left = any(s.mismatch for s in self.slots.values())
            self.alarm.stop_if_clear(any_left)

        # Handle baseline adaptation
        if result["needs_recalc"]:
            logger.info(f"Adapting baseline for slot {lid} (dist={dist:.3f})")
            state.adapt_baseline(realtime)
            self.db.save_baseline(lid, realtime)

    # ------------------------------------------------------------
    # ADMIN INTERFACE
    # ------------------------------------------------------------

    def admin_login(self, password: str) -> dict:
        """Admin authentication to view alarm details."""
        return self.alarm.authenticate_admin(password)

    def admin_clear_alarms(self):
        """Admin override to clear all alarms."""
        self.alarm.clear()
        logger.info("Admin cleared all alarms")

    # ------------------------------------------------------------
    # METRICS
    # ------------------------------------------------------------

    def _log_health(self):
        """Log system health metrics."""
        total = len(self.slots)
        mismatched = sum(1 for s in self.slots.values() if s.mismatch)

        distances = [s.last_dist for s in self.slots.values()]
        avg_dist = float(np.mean(distances)) if distances else 0.0
        max_dist = float(np.max(distances)) if distances else 0.0

        alarm_status = self.alarm.get_status()

        logger.info(
            f"Health: {total} slots, {mismatched} mismatched, "
            f"avg_dist={avg_dist:.3f}, max_dist={max_dist:.3f}, "
            f"alarm={alarm_status['active']}, "
            f"cycle_time={self.last_cycle_time:.2f}s"
        )

    def get_status(self) -> dict:
        """Get current monitoring status."""
        return {
            "total_slots": len(self.slots),
            "paused_slots": len(self.paused_slots),
            "mismatched_slots": sum(1 for s in self.slots.values() if s.mismatch),
            "alarm": self.alarm.get_status(),
            "cycle_count": self.cycle_count,
            "last_cycle_time": self.last_cycle_time,
            "avg_distance": float(np.mean([s.last_dist for s in self.slots.values()])) if self.slots else 0.0
        }