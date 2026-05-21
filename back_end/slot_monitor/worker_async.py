# ============================================================
# FILE: back_end/slot_monitor/worker_async.py
# ============================================================
"""
Async event-driven monitoring workers with DVW support.

Opt #10 — Concurrent slot embedding via ThreadPoolExecutor
──────────────────────────────────────────────────────────
Previously every slot's embedding was computed serially inside a for-loop:
    for slot in self.slots:
        await self._process_slot(slot, frame)   # one at a time

This wastes wall-clock time: slot.update() calls compute_embedding() which
calls cv2.calcHist, cv2.dct, np.linalg.norm — all heavy C extensions that
RELEASE the GIL while running.

Fix applied here:
  1. Each AsyncMonitorWorker owns a ThreadPoolExecutor (max_workers = min(4, len(slots)))
  2. Inside _process_slot, slot.update() is offloaded via run_in_executor so
     the C-extension work runs in a real OS thread without holding the GIL.
  3. _process_frame now fires all slot tasks concurrently with asyncio.gather,
     so all slots in the worker compute their embeddings in parallel.

Thread safety: each slot object is owned exclusively by one worker, so there
are no cross-slot data races. The event-loop-sensitive follow-up code (alarm
trigger, DB writes) still runs on the single asyncio event loop thread.

False-positive suppression (unchanged)
──────────────────────────────────────
During any DVW or admin resolution operation the operator's hand and phone
move over the box, causing transient embedding changes on non-target slots.
_any_operation_active() checks op_ctx and admin_ctx before forwarding a
trigger_alarm event to AlarmController.
"""

import asyncio
import logging
import time
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional
from dataclasses import dataclass

from back_end.slot_monitor.slots import Slot
from back_end.slot_monitor.alarm_controller import AlarmController

logger = logging.getLogger(__name__)

# ── Lazy singletons for operation-active checks ──────────────────────────────
_op_ctx    = None
_admin_ctx = None

def _get_op_ctx():
    global _op_ctx
    if _op_ctx is None:
        try:
            from back_end.slot_monitor.services.operation_context import op_ctx
            _op_ctx = op_ctx
        except Exception:
            pass
    return _op_ctx

def _get_admin_ctx():
    global _admin_ctx
    if _admin_ctx is None:
        try:
            from back_end.slot_monitor.admin.resolution_session import admin_ctx
            _admin_ctx = admin_ctx
        except Exception:
            pass
    return _admin_ctx


@dataclass
class WorkerMetrics:
    """Performance metrics for a worker"""
    worker_id: int
    frames_processed: int = 0
    total_distance: float = 0.0
    total_processing_time: float = 0.0
    alarms_triggered: int = 0
    alarms_suppressed: int = 0
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
    Async worker with DVW support and concurrent slot embedding (Opt #10).

    Slot control:
        pause_slot(lid)                  — suspend monitoring during a DVW operation
        resume_slot(lid)                 — lift pause after SUCCESSFUL operation
        restore_slot(lid, is_occupied)   — lift pause after FAILED/TIMED-OUT operation
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

        self._running = False
        self._task: Optional[asyncio.Task] = None

        self.metrics = WorkerMetrics(worker_id=worker_id)

        self._subscriber_id = f"worker-{worker_id}"

        self._paused_slots: Dict[int, bool] = {}
        self._slot_map: Dict[int, Slot] = {s.lid: s for s in slots}

        # Opt #10: ThreadPoolExecutor for concurrent embedding computation.
        # numpy/OpenCV release the GIL during C-extension calls, so threads
        # genuinely run in parallel for the compute-heavy parts.
        # Cap at 4 threads to avoid excessive context-switching overhead.
        _n_threads = min(4, max(1, len(slots)))
        self._embed_executor = ThreadPoolExecutor(
            max_workers=_n_threads,
            thread_name_prefix=f"SlotEmbed-W{worker_id}",
        )

        logger.info(
            f"AsyncWorker {worker_id} initialized: "
            f"slots={[s.lid for s in slots]} "
            f"embed_threads={_n_threads}"
        )

    # --------------------------------------------------------
    # DVW SLOT CONTROL
    # --------------------------------------------------------

    def pause_slot(self, lid: int):
        self._paused_slots[lid] = True
        logger.info(f"Worker {self.worker_id}: slot {lid} paused")

    def resume_slot(self, lid: int):
        self._paused_slots.pop(lid, None)
        logger.info(f"Worker {self.worker_id}: slot {lid} resumed")

    def restore_slot(self, lid: int, is_occupied: bool):
        """
        Lift pause after a FAILED or TIMED-OUT operation.
        Resets is_occupied, mismatch, and grace timer.
        Preserves distances_history so alarm can re-trigger naturally.
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
        logger.info(
            f"Worker {self.worker_id}: slot {lid} restored (is_occupied={is_occupied})"
        )

    def is_slot_paused(self, lid: int) -> bool:
        return self._paused_slots.get(lid, False)

    # --------------------------------------------------------
    # OPERATION-ACTIVE CHECK  (false-positive suppression)
    # --------------------------------------------------------

    @staticmethod
    def _any_operation_active() -> bool:
        try:
            ctx = _get_op_ctx()
            if ctx is not None and ctx.get_all_operations():
                return True
            actx = _get_admin_ctx()
            if actx is not None and actx.is_active():
                return True
        except Exception:
            pass
        return False

    # --------------------------------------------------------
    # LIFECYCLE
    # --------------------------------------------------------

    async def start(self):
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
        if not self._running:
            return

        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Opt #10: shut down the thread pool cleanly
        self._embed_executor.shutdown(wait=False)

        logger.info(f"AsyncWorker {self.worker_id} stopped")

    # --------------------------------------------------------
    # MONITORING LOOP
    # --------------------------------------------------------

    async def _monitor_loop(self):
        logger.info(f"Worker {self.worker_id} entering event-driven loop")

        try:
            while self._running:
                frame = await self.frame_buffer.wait_for_frame(self._subscriber_id)
                await self._process_frame(frame)

                if self.metrics.frames_processed % 1000 == 0:
                    self._log_metrics()

        except asyncio.CancelledError:
            logger.info(f"Worker {self.worker_id} cancelled")
            raise
        except Exception as e:
            logger.error(f"Worker {self.worker_id} fatal error: {e}", exc_info=True)
            self.metrics.errors += 1

    async def _process_frame(self, frame: np.ndarray):
        """
        Opt #10: run all slot tasks concurrently with asyncio.gather.

        Each _process_slot offloads slot.update() to the ThreadPoolExecutor,
        so all slots compute their embeddings in parallel OS threads.
        The event loop is free between awaits — no blocking.
        """
        start_time = time.perf_counter()

        tasks = [
            self._process_slot(slot, frame)
            for slot in self.slots
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Worker {self.worker_id} slot error: {r}")
                self.metrics.errors += 1

        elapsed = time.perf_counter() - start_time
        self.metrics.frames_processed += 1
        self.metrics.total_processing_time += elapsed

    async def _process_slot(self, slot: Slot, frame: np.ndarray):
        """
        Process a single slot.

        Opt #10: slot.update() is offloaded to the ThreadPoolExecutor so the
        CPU-bound embedding computation runs in a real OS thread. Since
        numpy/OpenCV release the GIL, multiple slots run truly in parallel.

        False-positive suppression: if a NEW alarm trigger would fire while
        any DVW/admin operation is active, the trigger is suppressed.
        """
        if self.is_slot_paused(slot.lid):
            return

        loop = asyncio.get_event_loop()

        # Opt #10: run the CPU-bound embedding in a thread, not the event loop
        result = await loop.run_in_executor(
            self._embed_executor,
            slot.update,
            frame,
            self.mismatch_threshold,
            self.recalc_threshold,
            self.grace_period,
        )

        self.metrics.total_distance += result["distance"]

        # Fast path: most frames are normal
        if not (result["trigger_alarm"] or result["stop_alarm"] or result["needs_recalc"]):
            return

        # False-positive suppression
        if result["trigger_alarm"] and self._any_operation_active():
            self.metrics.alarms_suppressed += 1
            logger.debug(
                f"Worker {self.worker_id}: alarm suppressed (operation active) "
                f"LID={slot.lid} dist={result['distance']:.4f}"
            )
            return

        pid = await self.db.get_pid_for_lid(slot.lid) or f"unknown-{slot.lid}"

        if result["trigger_alarm"]:
            self.alarm.trigger(pid, slot.lid)
            self.metrics.alarms_triggered += 1
            logger.critical(
                f"Worker {self.worker_id}: ALARM! LID={slot.lid} PID={pid} "
                f"dist={result['distance']:.4f}"
            )

        if result["stop_alarm"]:
            self.alarm.resolve(pid, slot.lid)
            logger.info(f"Worker {self.worker_id}: mismatch resolved for LID={slot.lid}")

        if result["needs_recalc"]:
            slot.adapt_baseline(result["embedding"])
            await self.db.save_baseline(slot.lid, result["embedding"])
            self.metrics.baselines_adapted += 1
            logger.info(
                f"Worker {self.worker_id}: baseline adapted LID={slot.lid} "
                f"dist={result['distance']:.4f}"
            )

    def _log_metrics(self):
        logger.info(
            f"Worker {self.worker_id}: frames={self.metrics.frames_processed} "
            f"avg_dist={self.metrics.avg_distance:.4f} "
            f"avg_time={self.metrics.avg_processing_ms:.2f}ms "
            f"alarms={self.metrics.alarms_triggered} "
            f"suppressed={self.metrics.alarms_suppressed} "
            f"paused={len(self._paused_slots)} "
            f"errors={self.metrics.errors}"
        )

    def get_metrics(self) -> Dict:
        return {
            "worker_id":          self.metrics.worker_id,
            "frames_processed":   self.metrics.frames_processed,
            "avg_distance":       self.metrics.avg_distance,
            "avg_processing_ms":  self.metrics.avg_processing_ms,
            "alarms_triggered":   self.metrics.alarms_triggered,
            "alarms_suppressed":  self.metrics.alarms_suppressed,
            "baselines_adapted":  self.metrics.baselines_adapted,
            "paused_slots":       len(self._paused_slots),
            "errors":             self.metrics.errors,
        }


class WorkerPool:
    """
    Manages a pool of AsyncMonitorWorker instances.
    All slot control calls route through here.
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
        w = self._get_worker(lid)
        if w:
            w.resume_slot(lid)

    def restore_slot(self, lid: int, is_occupied: bool):
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
        total_frames   = sum(w.metrics.frames_processed  for w in self.workers)
        total_distance = sum(w.metrics.total_distance    for w in self.workers)
        return {
            "num_workers":            len(self.workers),
            "total_frames_processed": total_frames,
            "avg_distance":           total_distance / total_frames if total_frames else 0.0,
            "total_alarms":           sum(w.metrics.alarms_triggered  for w in self.workers),
            "total_suppressed":       sum(w.metrics.alarms_suppressed for w in self.workers),
            "total_errors":           sum(w.metrics.errors            for w in self.workers),
            "workers":                [w.get_metrics() for w in self.workers],
        }