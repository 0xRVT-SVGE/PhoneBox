# ============================================================
# FILE: back_end/slot_monitor/worker_async.py
# ============================================================
"""
Async event-driven monitoring workers with DVW support.
"""

import asyncio
import logging
import time
import numpy as np
from typing import List, Dict, Optional
from dataclasses import dataclass

from back_end.slot_monitor.slots import Slot
from back_end.slot_monitor.alarm_controller import AlarmController

logger = logging.getLogger(__name__)


@dataclass
class WorkerMetrics:
    """Performance metrics for a worker"""
    worker_id: int
    frames_processed: int = 0
    total_distance: float = 0.0
    total_processing_time: float = 0.0
    alarms_triggered: int = 0
    baselines_adapted: int = 0
    errors: int = 0

    @property
    def avg_distance(self) -> float:
        return self.total_distance / self.frames_processed if self.frames_processed else 0.0

    @property
    def avg_processing_ms(self) -> float:
        return (self.total_processing_time / self.frames_processed) * 1000 if self.frames_processed else 0.0


class AsyncMonitorWorker:
    """
    Async worker with DVW support.

    Slot control:
        pause_slot(lid)                  — suspend monitoring during a DVW operation
        resume_slot(lid)                 — lift pause after SUCCESSFUL operation
                                           (slot state already correct, baseline already captured)
        restore_slot(lid, is_occupied)   — lift pause after FAILED/TIMED-OUT operation
                                           (resets is_occupied + mismatch + grace timer;
                                            distances_history is intentionally preserved so
                                            the alarm system can re-trigger naturally if the
                                            physical state warrants it)
    """

    def __init__(
            self,
            worker_id: int,
            slots: List[Slot],
            frame_buffer,
            db,
            alarm: AlarmController,
            mismatch_threshold: float,
            recalc_threshold: float,
            grace_period: float,
    ):
        self.worker_id = worker_id
        self.slots = slots
        self.frame_buffer = frame_buffer
        self.db = db
        self.alarm = alarm
        self.mismatch_threshold = mismatch_threshold
        self.recalc_threshold = recalc_threshold
        self.grace_period = grace_period

        # Runtime state
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Metrics
        self.metrics = WorkerMetrics(worker_id=worker_id)

        # Subscriber ID
        self._subscriber_id = f"worker-{worker_id}"

        # lid → True means monitoring is suspended for that slot
        self._paused_slots: Dict[int, bool] = {}

        # O(1) lid → Slot lookup
        self._slot_map: Dict[int, Slot] = {s.lid: s for s in slots}

        logger.info(f"AsyncWorker {worker_id} initialized: slots={[s.lid for s in slots]}")

    # --------------------------------------------------------
    # DVW SLOT CONTROL
    # --------------------------------------------------------

    def pause_slot(self, lid: int):
        """Suspend monitoring for a slot during a DVW operation."""
        self._paused_slots[lid] = True
        logger.info(f"Worker {self.worker_id}: slot {lid} paused")

    def resume_slot(self, lid: int):
        """
        Lift pause after a SUCCESSFUL operation.

        The slot's is_occupied and baseline are already correct because
        the operation updated them. Do not touch slot state here.
        """
        self._paused_slots.pop(lid, None)
        logger.info(f"Worker {self.worker_id}: slot {lid} resumed")

    def restore_slot(self, lid: int, is_occupied: bool):
        """
        Lift pause after a FAILED or TIMED-OUT operation.

        Resets:
            is_occupied      → recorded pre-operation value
            mismatch         → False (cleared so alarm logic starts clean)
            _grace_start_ts  → None  (stale timer would fire immediately)

        Does NOT clear distances_history — history is preserved so the
        monitoring loop can re-trigger an alarm naturally if the physical
        state of the slot still warrants it (e.g. phone was taken during
        a failed verify, slot really is empty, alarm should re-fire).

        Args:
            lid:         Location ID of the slot to restore.
            is_occupied: The occupancy the slot had before the operation.
        """
        self._paused_slots.pop(lid, None)

        slot = self._slot_map.get(lid)
        if slot is None:
            logger.warning(
                f"Worker {self.worker_id}: cannot restore slot {lid} — not in this worker"
            )
            return

        slot.is_occupied = is_occupied
        slot.mismatch = False
        slot._grace_start_ts = None
        # distances_history intentionally NOT cleared — see docstring
        logger.info(
            f"Worker {self.worker_id}: slot {lid} restored (is_occupied={is_occupied})"
        )

    def is_slot_paused(self, lid: int) -> bool:
        """Check if slot is paused"""
        return self._paused_slots.get(lid, False)

    # --------------------------------------------------------
    # LIFECYCLE
    # --------------------------------------------------------

    async def start(self):
        """Start worker task"""
        if self._running:
            logger.warning(f"Worker {self.worker_id} already running")
            return

        self._running = True
        self._task = asyncio.create_task(
            self._monitor_loop(),
            name=f"AsyncWorker-{self.worker_id}"
        )
        logger.info(f"AsyncWorker {self.worker_id} started")

    async def stop(self):
        """Stop worker task"""
        if not self._running:
            return

        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info(f"AsyncWorker {self.worker_id} stopped")

    # --------------------------------------------------------
    # MONITORING LOOP
    # --------------------------------------------------------

    async def _monitor_loop(self):
        """Main monitoring loop (event-driven)"""
        logger.info(f"Worker {self.worker_id} entering event-driven loop")

        try:
            while self._running:
                frame = await self.frame_buffer.wait_for_frame(self._subscriber_id)
                await self._process_frame(frame)

                if self.metrics.frames_processed % 100 == 0:
                    self._log_metrics()

        except asyncio.CancelledError:
            logger.info(f"Worker {self.worker_id} cancelled")
            raise
        except Exception as e:
            logger.error(f"Worker {self.worker_id} fatal error: {e}", exc_info=True)
            self.metrics.errors += 1

    async def _process_frame(self, frame: np.ndarray):
        """Process all assigned slots"""
        start_time = time.perf_counter()

        for slot in self.slots:
            try:
                await self._process_slot(slot, frame)
            except Exception as e:
                logger.error(f"Worker {self.worker_id} failed slot {slot.lid}: {e}")
                self.metrics.errors += 1

        elapsed = time.perf_counter() - start_time
        self.metrics.frames_processed += 1
        self.metrics.total_processing_time += elapsed

    async def _process_slot(self, slot: Slot, frame: np.ndarray):
        """
        Process a single slot.

        Skips processing if slot is paused (DVW operation in progress).
        """
        if self.is_slot_paused(slot.lid):
            return

        result = slot.update(
            frame=frame,
            mismatch_threshold=self.mismatch_threshold,
            recalc_threshold=self.recalc_threshold,
            grace_period=self.grace_period,
        )

        dist = result["distance"]
        self.metrics.total_distance += dist

        pid = await self.db.get_pid_for_lid(slot.lid)
        if pid is None:
            pid = f"unknown-{slot.lid}"

        if result["trigger_alarm"]:
            self.alarm.trigger(pid, slot.lid)
            self.metrics.alarms_triggered += 1
            logger.critical(
                f"Worker {self.worker_id}: ALARM! LID={slot.lid} PID={pid} dist={dist:.4f}"
            )

        if result["stop_alarm"]:
            # resolve() removes this specific (pid, lid) pair and stops the
            # alarm only if no other mismatches remain across the entire set.
            self.alarm.resolve(pid, slot.lid)
            logger.info(f"Worker {self.worker_id}: mismatch resolved for LID={slot.lid}")

        if result["needs_recalc"]:
            slot.adapt_baseline(result["embedding"])
            await self.db.save_baseline(slot.lid, result["embedding"])
            self.metrics.baselines_adapted += 1
            logger.info(
                f"Worker {self.worker_id}: baseline adapted LID={slot.lid} dist={dist:.4f}"
            )

    def _log_metrics(self):
        """Log performance metrics"""
        logger.info(
            f"Worker {self.worker_id}: frames={self.metrics.frames_processed} "
            f"avg_dist={self.metrics.avg_distance:.4f} "
            f"avg_time={self.metrics.avg_processing_ms:.2f}ms "
            f"alarms={self.metrics.alarms_triggered} "
            f"paused={len(self._paused_slots)} "
            f"errors={self.metrics.errors}"
        )

    def get_metrics(self) -> Dict:
        """Get current metrics"""
        return {
            "worker_id": self.metrics.worker_id,
            "frames_processed": self.metrics.frames_processed,
            "avg_distance": self.metrics.avg_distance,
            "avg_processing_ms": self.metrics.avg_processing_ms,
            "alarms_triggered": self.metrics.alarms_triggered,
            "baselines_adapted": self.metrics.baselines_adapted,
            "paused_slots": len(self._paused_slots),
            "errors": self.metrics.errors,
        }


class WorkerPool:
    """
    Manages a pool of AsyncMonitorWorker instances.

    All slot control calls (pause / resume / restore) route through here.
    WorkerPool finds the owning worker by lid and delegates.
    """

    def __init__(
            self,
            num_workers: int,
            slots: List[Slot],
            frame_buffer,
            db,
            alarm: AlarmController,
            mismatch_threshold: float,
            recalc_threshold: float,
            grace_period: float,
    ):
        self.num_workers = num_workers
        self.workers: List[AsyncMonitorWorker] = []
        self._slot_to_worker: Dict[int, int] = {}

        self._distribute_slots(
            slots=slots,
            frame_buffer=frame_buffer,
            db=db,
            alarm=alarm,
            mismatch_threshold=mismatch_threshold,
            recalc_threshold=recalc_threshold,
            grace_period=grace_period,
        )
        logger.info(f"WorkerPool created: {num_workers} workers, {len(slots)} slots")

    def _distribute_slots(self, slots, frame_buffer, db, alarm,
                          mismatch_threshold, recalc_threshold, grace_period):
        slot_list = sorted(slots, key=lambda s: s.lid)
        slots_per_worker = len(slot_list) // self.num_workers
        remainder = len(slot_list) % self.num_workers
        start = 0
        for i in range(self.num_workers):
            count = slots_per_worker + (1 if i < remainder else 0)
            worker_slots = slot_list[start:start + count]
            for slot in worker_slots:
                self._slot_to_worker[slot.lid] = i
            self.workers.append(AsyncMonitorWorker(
                worker_id=i, slots=worker_slots, frame_buffer=frame_buffer,
                db=db, alarm=alarm,
                mismatch_threshold=mismatch_threshold,
                recalc_threshold=recalc_threshold,
                grace_period=grace_period,
            ))
            start += count

    def _get_worker(self, lid: int) -> Optional[AsyncMonitorWorker]:
        idx = self._slot_to_worker.get(lid)
        if idx is None:
            logger.warning(f"WorkerPool: slot {lid} not found in any worker")
            return None
        return self.workers[idx]

    def pause_slot(self, lid: int):
        w = self._get_worker(lid)
        if w:
            w.pause_slot(lid)

    def resume_slot(self, lid: int):
        """Lift pause after successful operation — slot state already correct."""
        w = self._get_worker(lid)
        if w:
            w.resume_slot(lid)

    def restore_slot(self, lid: int, is_occupied: bool):
        """
        Lift pause after failed/timed-out operation.
        Resets is_occupied, mismatch, and grace timer.
        Preserves distances_history so alarm can re-trigger naturally.
        """
        w = self._get_worker(lid)
        if w:
            w.restore_slot(lid, is_occupied)

    async def start_all(self):
        logger.info("Starting worker pool...")
        await asyncio.gather(*[w.start() for w in self.workers])
        logger.info(f"{len(self.workers)} workers started")

    async def stop_all(self):
        logger.info("Stopping worker pool...")
        await asyncio.gather(*[w.stop() for w in self.workers])
        logger.info(f"{len(self.workers)} workers stopped")

    def get_metrics(self) -> Dict:
        total_frames = sum(w.metrics.frames_processed for w in self.workers)
        total_distance = sum(w.metrics.total_distance for w in self.workers)
        return {
            "num_workers": len(self.workers),
            "total_frames_processed": total_frames,
            "avg_distance": total_distance / total_frames if total_frames else 0.0,
            "total_alarms": sum(w.metrics.alarms_triggered for w in self.workers),
            "total_errors": sum(w.metrics.errors for w in self.workers),
            "workers": [w.get_metrics() for w in self.workers],
        }