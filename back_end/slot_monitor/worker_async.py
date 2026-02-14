# ============================================================
# FILE: back_end/slot_monitor/worker_async.py (DVW COMPATIBLE)
# ============================================================
"""
Async event-driven monitoring workers with DVW support.

NEW: Supports pausing individual slots during DVW operations.
"""

import asyncio
import logging
import time
import numpy as np
from typing import List, Dict
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
        if self.frames_processed == 0:
            return 0.0
        return self.total_distance / self.frames_processed

    @property
    def avg_processing_ms(self) -> float:
        if self.frames_processed == 0:
            return 0.0
        return (self.total_processing_time / self.frames_processed) * 1000


class AsyncMonitorWorker:
    """
    Async worker with DVW support.

    NEW FEATURES:
    - pause_slot(lid): Temporarily skip monitoring a slot
    - resume_slot(lid): Resume monitoring
    - Supports slot operations during DVW
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
        self._task: asyncio.Task = None

        # Metrics
        self.metrics = WorkerMetrics(worker_id=worker_id)

        # Subscriber ID
        self._subscriber_id = f"worker-{worker_id}"

        # DVW: Paused slots (lid -> True/False)
        self._paused_slots = {}

        slot_ids = [s.lid for s in slots]
        logger.info(f"AsyncWorker {worker_id} initialized: {len(slots)} slots {slot_ids}")

    def pause_slot(self, lid: int):
        """Pause monitoring for a specific slot (DVW operation)"""
        self._paused_slots[lid] = True
        logger.info(f"Worker {self.worker_id}: Slot {lid} paused")

    def resume_slot(self, lid: int):
        """Resume monitoring for a specific slot"""
        self._paused_slots.pop(lid, None)
        logger.info(f"Worker {self.worker_id}: Slot {lid} resumed")

    def is_slot_paused(self, lid: int) -> bool:
        """Check if slot is paused"""
        return self._paused_slots.get(lid, False)

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

        NEW: Skips processing if slot is paused (DVW operation)
        """
        # DVW: Skip if slot is paused
        if self.is_slot_paused(slot.lid):
            return

        # Compute embedding and distance
        result = slot.update(
            frame=frame,
            mismatch_threshold=self.mismatch_threshold,
            recalc_threshold=self.recalc_threshold,
            grace_period=self.grace_period,
        )

        dist = result["distance"]
        self.metrics.total_distance += dist

        # Get phone ID
        pid = await self.db.get_pid_for_lid(slot.lid)
        if pid is None:
            pid = f"unknown-{slot.lid}"

        # Handle alarms
        if result["trigger_alarm"]:
            self.alarm.trigger(pid, slot.lid)
            self.metrics.alarms_triggered += 1
            logger.critical(
                f"Worker {self.worker_id}: ALARM! "
                f"LID={slot.lid}, PID={pid}, dist={dist:.4f}"
            )

        if result["stop_alarm"]:
            any_mismatch = any(s.mismatch for s in self.slots)
            self.alarm.stop_if_clear(any_mismatch)
            logger.info(f"Worker {self.worker_id}: Alarm cleared for LID={slot.lid}")

        if result["needs_recalc"]:
            logger.info(
                f"Worker {self.worker_id}: Baseline adaptation "
                f"LID={slot.lid}, dist={dist:.4f}"
            )
            slot.adapt_baseline(result["embedding"])
            await self.db.save_baseline(slot.lid, result["embedding"])
            self.metrics.baselines_adapted += 1

    def _log_metrics(self):
        """Log performance metrics"""
        logger.info(
            f"Worker {self.worker_id} metrics: "
            f"frames={self.metrics.frames_processed}, "
            f"avg_dist={self.metrics.avg_distance:.4f}, "
            f"avg_time={self.metrics.avg_processing_ms:.2f}ms, "
            f"alarms={self.metrics.alarms_triggered}, "
            f"paused={len(self._paused_slots)}, "
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
    Manages async workers with DVW support.

    NEW: pause_slot/resume_slot propagate to correct worker.
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

        # LID -> worker_id mapping (for DVW operations)
        self._slot_to_worker = {}

        self._distribute_slots(
            slots=slots,
            frame_buffer=frame_buffer,
            db=db,
            alarm=alarm,
            mismatch_threshold=mismatch_threshold,
            recalc_threshold=recalc_threshold,
            grace_period=grace_period,
        )

        logger.info(f"WorkerPool created: {num_workers} workers")

    def _distribute_slots(
            self,
            slots: List[Slot],
            frame_buffer,
            db,
            alarm,
            mismatch_threshold,
            recalc_threshold,
            grace_period,
    ):
        """Distribute slots evenly across workers"""
        slot_list = sorted(slots, key=lambda s: s.lid)
        slots_per_worker = len(slot_list) // self.num_workers
        remainder = len(slot_list) % self.num_workers

        start_idx = 0
        for i in range(self.num_workers):
            count = slots_per_worker + (1 if i < remainder else 0)
            end_idx = start_idx + count
            worker_slots = slot_list[start_idx:end_idx]

            # Track which worker owns which slot
            for slot in worker_slots:
                self._slot_to_worker[slot.lid] = i

            worker = AsyncMonitorWorker(
                worker_id=i,
                slots=worker_slots,
                frame_buffer=frame_buffer,
                db=db,
                alarm=alarm,
                mismatch_threshold=mismatch_threshold,
                recalc_threshold=recalc_threshold,
                grace_period=grace_period,
            )
            self.workers.append(worker)

            start_idx = end_idx

    def pause_slot(self, lid: int):
        """Pause monitoring for a slot (DVW operation)"""
        worker_id = self._slot_to_worker.get(lid)
        if worker_id is not None and worker_id < len(self.workers):
            self.workers[worker_id].pause_slot(lid)
        else:
            logger.warning(f"Cannot pause slot {lid}: worker not found")

    def resume_slot(self, lid: int):
        """Resume monitoring for a slot"""
        worker_id = self._slot_to_worker.get(lid)
        if worker_id is not None and worker_id < len(self.workers):
            self.workers[worker_id].resume_slot(lid)
        else:
            logger.warning(f"Cannot resume slot {lid}: worker not found")

    def remove_slot(self, lid: int):
        """Remove slot from monitoring (after withdrawal)"""
        # Same as resume for now, but semantically different
        self.resume_slot(lid)

    async def start_all(self):
        """Start all workers"""
        logger.info("Starting worker pool...")
        tasks = [worker.start() for worker in self.workers]
        await asyncio.gather(*tasks)
        logger.info(f"{len(self.workers)} workers started")

    async def stop_all(self):
        """Stop all workers"""
        logger.info("Stopping worker pool...")
        tasks = [worker.stop() for worker in self.workers]
        await asyncio.gather(*tasks)
        logger.info(f"{len(self.workers)} workers stopped")

    def get_metrics(self) -> Dict:
        """Get aggregated metrics"""
        total_frames = sum(w.metrics.frames_processed for w in self.workers)
        total_distance = sum(w.metrics.total_distance for w in self.workers)
        total_alarms = sum(w.metrics.alarms_triggered for w in self.workers)
        total_errors = sum(w.metrics.errors for w in self.workers)

        return {
            "num_workers": len(self.workers),
            "total_frames_processed": total_frames,
            "avg_distance": total_distance / total_frames if total_frames > 0 else 0.0,
            "total_alarms": total_alarms,
            "total_errors": total_errors,
            "workers": [w.get_metrics() for w in self.workers],
        }