# ============================================================
# FILE: back_end/slot_monitor/headless_slot_monitor.py
# ============================================================
"""
Headless Async Slot Monitor (Production Mode).
No visualization window — runs as background service.
"""

import asyncio
import logging
import math
import signal
import sys
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)

from back_end.slot_monitor.camera.camera_async import AsyncFrameBuffer, AsyncCameraCapture
from back_end.slot_monitor.worker_async import WorkerPool
from back_end.slot_monitor.slots import Slot, generate_grid_rois
from back_end.slot_monitor.alarm_controller import AlarmController
from back_end.slot_monitor.db_interface import AsyncSlotMonitorDB
from back_end.config import (
    CameraConfig        as _CC,
    DatabaseConfig      as _DC,
    SlotMonitorConfig   as _SMC,
    CameraProcessConfig as _CPC,
)
from back_end.secrets import Secrets

# Opt #1: use multi-process camera when CameraProcessConfig.ENABLED is True.
if _CPC.ENABLED:
    from back_end.camera_process import SharedFrameBuffer as _FrameBufferClass
    _USING_PROCESS = True
else:
    _FrameBufferClass = AsyncFrameBuffer   # type: ignore[assignment]
    _USING_PROCESS = False



class HeadlessSlotMonitor:

    def __init__(
            self,
            stop_event: threading.Event,
            camera_id:     int   = _CC.BOTTOM_CAM_INDEX,
            camera_width:  int   = _CC.BOTTOM_CAM_WIDTH,
            camera_height: int   = _CC.BOTTOM_CAM_HEIGHT,
            camera_fps:    int   = _CC.BOTTOM_CAM_FPS,

            grid_rows: int = None,
            grid_cols: int = None,

            num_workers:        int   = _SMC.NUM_WORKERS,
            mismatch_threshold: float = _SMC.MISMATCH_THRESHOLD,
            recalc_threshold:   float = _SMC.RECALC_THRESHOLD,
            grace_period:       float = _SMC.GRACE_PERIOD,

            db_host:     str = _DC.ASYNC_HOST,
            db_port:     int = _DC.ASYNC_PORT,
            db_name:     str = _DC.ASYNC_DATABASE,
            db_user:     str = Secrets.DB_USER,
            db_password: str = Secrets.DB_PASSWORD,

            socketio=None,
    ):
        self._stop_event = stop_event

        self.camera_id     = camera_id
        self.camera_width  = camera_width
        self.camera_height = camera_height
        self.camera_fps    = camera_fps
        self.grid_rows     = grid_rows
        self.grid_cols     = grid_cols

        self.num_workers        = num_workers
        self.mismatch_threshold = mismatch_threshold
        self.recalc_threshold   = recalc_threshold
        self.grace_period       = grace_period

        self.db_config = {
            "host":     db_host,
            "port":     db_port,
            "database": db_name,
            "user":     db_user,
            "password": db_password,
        }
        self.socketio = socketio

        self.frame_buffer: Optional[AsyncFrameBuffer]  = None
        self.camera:       Optional[AsyncCameraCapture] = None
        self.db:           Optional[AsyncSlotMonitorDB] = None
        self.alarm:        Optional[AlarmController]    = None
        self.worker_pool:  Optional[WorkerPool]         = None
        self.slots:  Dict[int, Slot] = {}
        self.rois:   Dict            = {}

        self.loop:    Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread]          = None
        self._setup_complete = threading.Event()

        logger.info("=" * 70)
        logger.info("HEADLESS SLOT MONITOR (PRODUCTION MODE)")
        logger.info("=" * 70)
        logger.info(
            f"Camera: {camera_id} ({camera_width}x{camera_height} @ {camera_fps}fps)"
        )
        logger.info(
            f"Workers: {num_workers} | "
            f"mismatch={mismatch_threshold} recalc={recalc_threshold}"
        )

    # ── Setup ─────────────────────────────────────────────

    async def _setup(self):
        logger.info("=" * 70)
        logger.info("SLOT MONITOR SETUP")
        logger.info("=" * 70)
        self.loop = asyncio.get_running_loop()
        await self._setup_database()
        await self._setup_camera()
        await self._check_baselines()
        await self._initialize_monitoring()
        await self._create_workers()

        from back_end.slot_monitor.services.operation_context import op_ctx
        op_ctx.set_worker_pool(self.worker_pool)

        logger.info("Setup complete")

    async def _setup_database(self):
        logger.info("Connecting to database...")
        self.db = AsyncSlotMonitorDB(**self.db_config)
        await self.db.connect()
        if not await self.db.is_healthy():
            raise RuntimeError("Database connection failed")
        logger.info(
            f"Database ready (pool: {self.db.min_pool_size}-{self.db.max_pool_size})"
        )

    async def _setup_camera(self):
        logger.info("Setting up camera...")
        logger.info(
            "[SlotMonitor] camera backend: %s (capture: %s)",
            "multi-process (Opt #1)" if _USING_PROCESS else "threading (default)",
            _CC.BOTTOM_CAM_BACKEND,
        )
        self.frame_buffer = _FrameBufferClass()

        self.frame_buffer.set_event_loop(self.loop)

        self.frame_buffer.start_capture(
            camera_id = self.camera_id,
            width     = self.camera_width,
            height    = self.camera_height,
            fps       = self.camera_fps,
            backend   = _CC.resolve_backend(_CC.BOTTOM_CAM_BACKEND),
        )

        self.camera = AsyncCameraCapture(
            frame_buffer = self.frame_buffer,
            camera_id    = self.camera_id,
            width        = self.camera_width,
            height       = self.camera_height,
        )

        frame = self.frame_buffer.get_frame_sync()
        if frame is None:
            raise RuntimeError("Failed to get initial camera frame")

        if self.grid_rows is None or self.grid_cols is None:
            num_lids = await self.db.get_num_lid()
            cols     = math.ceil(math.sqrt(num_lids))
            rows     = math.ceil(num_lids / cols)
            self.grid_rows, self.grid_cols = rows, cols
            logger.info(f"Auto grid: {rows}x{cols} for {num_lids} slots")

        # Fetch actual lid values so ROI dict is keyed by real DB lids.
        # For Box 1 (lids 0-29) this is equivalent to enumerate; for Box 2+
        # (e.g. lids 30-59) it prevents a silent lid-key mismatch.
        box_lids = await self.db.get_box_lids()
        if not box_lids:
            raise RuntimeError(
                f"No locations found for box_id={self.db._box_id}. "
                "Run migrations/0001_multi_box.sql and populate the locations table."
            )

        self.rois = generate_grid_rois(
            frame_width  = self.camera_width,
            frame_height = self.camera_height,
            rows         = self.grid_rows,
            cols         = self.grid_cols,
            spacing      = _SMC.GRID_SPACING,
            num_lids     = len(box_lids),
            frame        = frame,
            lid_list     = box_lids,
        )
        logger.info(f"Camera ready — {len(self.rois)} ROIs generated (lids {box_lids[0]}-{box_lids[-1]})")


    async def _check_baselines(self):
        logger.info("Checking baselines...")
        baselines = await self.db.fetch_all_baselines()
        if not baselines:
            logger.warning("No baselines found — will initialize from current state")
            return

        frame = self.frame_buffer.get_frame_sync()
        if frame is None:
            logger.error("No frame available for baseline check")
            return

        mismatches = 0
        updated    = {}
        for lid, baseline in baselines.items():
            if lid not in self.rois:
                continue
            try:
                temp = Slot(
                    lid          = lid,
                    roi_coords   = self.rois[lid],
                    baseline_emb = baseline,
                    is_occupied  = False,
                )
                dist = temp.compute_distance(frame)
                if dist > self.mismatch_threshold:
                    logger.warning(f"  Slot {lid}: MISMATCH (dist={dist:.4f})")
                    mismatches += 1
                else:
                    logger.info(f"  Slot {lid}: OK (dist={dist:.4f})")
                    if dist > self.recalc_threshold:
                        updated[lid] = temp.compute_embedding(frame)
            except Exception as e:
                logger.error(f"  Slot {lid}: check failed — {e}")

        if updated:
            await self.db.save_baselines_batch(updated)
            logger.info(f"Updated {len(updated)} baselines")
        if mismatches:
            logger.critical(
                f"{mismatches} SLOT(S) WITH MISMATCHES — "
                "MANUAL INTERVENTION REQUIRED"
            )

    async def _initialize_monitoring(self):
        logger.info("Initializing slot states from database...")
        occupied_slots = await self.db.fetch_occupied_slots()
        occupied_lids  = {lid: pid for lid, pid in occupied_slots}
        baselines      = await self.db.fetch_all_baselines()
        logger.info(
            f"{len(occupied_lids)} occupied slots, {len(baselines)} baselines"
        )

        for lid, baseline in baselines.items():
            if lid not in self.rois:
                logger.warning(
                    f"  Slot {lid}: baseline in DB but no ROI — skipping "
                    "(re-run roi_calibration to fix)"
                )
                continue
            is_occupied     = lid in occupied_lids
            self.slots[lid] = Slot(
                lid          = lid,
                roi_coords   = self.rois[lid],
                baseline_emb = baseline,
                is_occupied  = is_occupied,
            )
            pid_info = f" (PID: {occupied_lids[lid]})" if is_occupied else ""
            logger.info(
                f"  Slot {lid}: {'OCCUPIED' if is_occupied else 'EMPTY'}{pid_info}"
            )

        logger.info(f"Initialized {len(self.slots)} / {len(self.rois)} slots")

        if not self.slots:
            raise RuntimeError(
                f"No calibrated baselines found for box_id={self.db._box_id}. "
                "Run embed_calibration.py (with PHONEBOX_BOX_SLUG set correctly) "
                "before starting the monitor."
            )

    async def _create_workers(self):
        logger.info(f"Creating {self.num_workers} workers...")
        self.alarm = AlarmController()
        if self.socketio:
            self.alarm.set_socketio(self.socketio)

        self.worker_pool = WorkerPool(
            num_workers        = self.num_workers,
            slots              = list(self.slots.values()),
            frame_buffer       = self.frame_buffer,
            db                 = self.db,
            alarm              = self.alarm,
            mismatch_threshold = self.mismatch_threshold,
            recalc_threshold   = self.recalc_threshold,
            grace_period       = self.grace_period,
        )
        logger.info(f"{self.num_workers} workers ready")

    # ── Run ───────────────────────────────────────────────

    async def _run(self):
        logger.info("=" * 70)
        logger.info("MONITORING ACTIVE")
        logger.info("=" * 70)

        await self.worker_pool.start_all()

        status_task = asyncio.create_task(self._status_reporter())
        stop_task   = asyncio.create_task(self._watch_stop_event())

        done, pending = await asyncio.wait(
            {status_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _watch_stop_event(self):
        """Poll the shared threading.Event without blocking the async loop."""
        while not self._stop_event.is_set():
            await asyncio.sleep(_SMC.STOP_POLL_INTERVAL)
        logger.info("Stop event received — shutting down slot monitor")

    async def _status_reporter(self):
        try:
            while True:
                await asyncio.sleep(_SMC.STATUS_REPORT_INTERVAL)
                await self._log_status()
        except asyncio.CancelledError:
            pass

    async def _log_status(self):
        logger.info("=" * 70)
        logger.info("SLOT MONITOR STATUS")
        logger.info("=" * 70)
        if self.camera:
            try:
                m = self.camera.get_metrics()
                logger.info(
                    f"Camera: {m['frame_count']} frames, "
                    f"{m['active_subscribers']} subscribers"
                )
            except Exception as e:
                logger.warning(f"Camera metrics unavailable: {e}")
        if self.worker_pool:
            try:
                m = self.worker_pool.get_metrics()
                logger.info(
                    f"Workers: {m['total_frames_processed']} frames, "
                    f"avg_dist={m['avg_distance']:.4f}"
                )
                logger.info(
                    f"Alarms: {m['total_alarms']}, "
                    f"Errors: {m['total_errors']}"
                )
            except Exception as e:
                logger.warning(f"Worker metrics unavailable: {e}")
        if self.alarm:
            try:
                s = self.alarm.get_status()
                if s['active']:
                    logger.warning(
                        f"ALARM ACTIVE: {s['mismatch_count']} mismatches"
                    )
            except Exception as e:
                logger.warning(f"Alarm status unavailable: {e}")

    # ── Shutdown ──────────────────────────────────────────

    async def _shutdown(self):
        logger.info("Shutting down slot monitor...")
        if self.worker_pool:
            await self.worker_pool.stop_all()
        if self.frame_buffer:
            self.frame_buffer.stop_capture()
        if self.db:
            await self.db.close()
        logger.info("Slot monitor shutdown complete")

    # ── Public API ────────────────────────────────────────

    def start(self):
        def _run():
            asyncio.run(self._async_main())

        self._thread = threading.Thread(
            target=_run, daemon=True, name="SlotMonitor"
        )
        self._thread.start()
        logger.info("Slot monitor thread started")

    def join(self, timeout: float = 8.0):
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    async def _async_main(self):
        try:
            await self._setup()
            self._setup_complete.set()
            await self._run()
        except Exception as e:
            logger.error(f"Slot monitor fatal error: {e}", exc_info=True)
        finally:
            await self._shutdown()


# ============================================================
# STANDALONE ENTRY POINT
# ============================================================

async def main():
    stop_event = threading.Event()

    if sys.platform != 'win32':
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

    monitor = HeadlessSlotMonitor(stop_event=stop_event)

    try:
        await monitor._setup()
        await monitor._run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt")
    finally:
        stop_event.set()
        await monitor._shutdown()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    )
    asyncio.run(main())