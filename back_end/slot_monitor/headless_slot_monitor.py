#!/usr/bin/env python3
"""
Headless Async Slot Monitor (Production Mode)

No visualization window - runs as background service.
All monitoring via logs and WebSocket events.
"""

import asyncio
import logging
import signal
import sys
import time
from typing import Dict, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import async modules
from back_end.slot_monitor.camera.camera_async import AsyncFrameBuffer, AsyncCameraCapture
from back_end.slot_monitor.worker_async import WorkerPool
from back_end.slot_monitor.slots import Slot, generate_grid_rois
from back_end.slot_monitor.alarm_controller import AlarmController
from back_end.slot_monitor.db_interface import AsyncSlotMonitorDB


class HeadlessSlotMonitor:
    """
    Production slot monitoring system (no GUI).

    Designed to run as a background service.
    Integrates with Flask-SocketIO for remote monitoring.
    """

    def __init__(
            self,
            # Camera config
            camera_id: int = 1,  # Bottom camera (slot monitoring)
            camera_width: int = 1280,
            camera_height: int = 720,
            camera_fps: int = 30,

            # Grid config (loaded from DB)
            grid_rows: int = None,
            grid_cols: int = None,

            # Worker config
            num_workers: int = 4,

            # Monitoring config
            mismatch_threshold: float = 0.15,
            recalc_threshold: float = 0.05,
            grace_period: float = 5.0,

            # DB config
            db_host: str = "localhost",
            db_port: int = 5432,
            db_name: str = "PhoneBoxDB",
            db_user: str = "admin",
            db_password: str = "admin",

            # SocketIO (optional - for remote monitoring)
            socketio=None,
    ):
        # Camera configuration
        self.camera_id = camera_id
        self.camera_width = camera_width
        self.camera_height = camera_height
        self.camera_fps = camera_fps
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols

        # Worker configuration
        self.num_workers = num_workers
        self.mismatch_threshold = mismatch_threshold
        self.recalc_threshold = recalc_threshold
        self.grace_period = grace_period

        # DB configuration
        self.db_config = {
            "host": db_host,
            "port": db_port,
            "database": db_name,
            "user": db_user,
            "password": db_password,
        }

        # SocketIO for remote monitoring
        self.socketio = socketio

        # System components
        self.frame_buffer: Optional[AsyncFrameBuffer] = None
        self.camera: Optional[AsyncCameraCapture] = None
        self.db: Optional[AsyncSlotMonitorDB] = None
        self.alarm: Optional[AlarmController] = None
        self.worker_pool: Optional[WorkerPool] = None
        self.slots: Dict[int, Slot] = {}
        self.rois: Dict = {}

        # Event loop
        self.loop: Optional[asyncio.AbstractEventLoop] = None

        # Shutdown flag
        self._shutdown_event = asyncio.Event()

        logger.info("=" * 70)
        logger.info("HEADLESS SLOT MONITOR (PRODUCTION MODE)")
        logger.info("=" * 70)
        logger.info(f"Camera: {camera_id} ({camera_width}x{camera_height} @ {camera_fps}fps)")
        logger.info(f"Workers: {num_workers} (async, event-driven)")
        logger.info(f"Thresholds: mismatch={mismatch_threshold}, recalc={recalc_threshold}")

    async def setup(self):
        """Initialize all system components"""
        logger.info("\n" + "=" * 70)
        logger.info("SYSTEM SETUP")
        logger.info("=" * 70)

        self.loop = asyncio.get_running_loop()

        await self._setup_database()
        await self._setup_camera()
        await self._check_baselines()
        await self._initialize_monitoring()
        await self._create_workers()

        logger.info("System setup complete")

    async def _setup_database(self):
        """Initialize async database connection"""
        logger.info("Setting up async database...")

        self.db = AsyncSlotMonitorDB(**self.db_config)
        await self.db.connect()

        if await self.db.test_connection():
            logger.info("Database connected")
        else:
            raise RuntimeError("Database connection failed")

        stats = await self.db.get_pool_stats()
        logger.info(f"   Pool: {stats['min']}-{stats['max']} connections")

    async def _setup_camera(self):
        """Initialize async camera system"""
        logger.info("\nSetting up async camera...")

        # Create frame buffer
        self.frame_buffer = AsyncFrameBuffer()
        self.frame_buffer.set_event_loop(self.loop)

        # Start camera capture
        self.frame_buffer.start_capture(
            camera_id=self.camera_id,
            width=self.camera_width,
            height=self.camera_height,
            fps=self.camera_fps,
        )

        # Create camera interface
        self.camera = AsyncCameraCapture(
            frame_buffer=self.frame_buffer,
            camera_id=self.camera_id,
            width=self.camera_width,
            height=self.camera_height,
        )

        # Get initial frame
        frame = self.frame_buffer.get_frame_sync()
        if frame is None:
            raise RuntimeError("Failed to get initial camera frame")

        # Get grid config from DB if not specified
        if self.grid_rows is None or self.grid_cols is None:
            num_lids = await self.db.get_num_lid()
            # Auto-calculate grid dimensions
            import math
            cols = math.ceil(math.sqrt(num_lids))
            rows = math.ceil(num_lids / cols)
            self.grid_rows = rows
            self.grid_cols = cols
            logger.info(f"Auto-calculated grid: {rows}x{cols} for {num_lids} slots")

        # Generate ROIs
        self.rois = generate_grid_rois(
            frame_width=self.camera_width,
            frame_height=self.camera_height,
            rows=self.grid_rows,
            cols=self.grid_cols,
            spacing=10,
            num_lids=await self.db.get_num_lid(),
            frame=frame
        )

        logger.info(f"Generated {len(self.rois)} ROIs")
        logger.info("Async camera ready")

    async def _check_baselines(self):
        """Check existing baselines"""
        logger.info("\n" + "=" * 70)
        logger.info("CHECKING BASELINES")
        logger.info("=" * 70)

        baselines = await self.db.fetch_all_baselines()

        if not baselines:
            logger.warning("No baselines found - system will initialize from current state")
            return

        logger.info(f"Found {len(baselines)} baselines in database")

        frame = self.frame_buffer.get_frame_sync()
        if frame is None:
            logger.error("Failed to get frame")
            return

        mismatches = 0
        updated_baselines = {}

        for lid, baseline in baselines.items():
            if lid not in self.rois:
                continue

            try:
                temp_slot = Slot(
                    lid=lid,
                    roi_coords=self.rois[lid],
                    baseline_emb=baseline,
                    is_occupied=False,
                )

                dist = temp_slot.compute_distance(frame)

                if dist > self.mismatch_threshold:
                    logger.warning(
                        f"Slot {lid}: MISMATCH (dist={dist:.4f}) - manual intervention required"
                    )
                    mismatches += 1
                else:
                    logger.info(f"✓  Slot {lid}: OK (dist={dist:.4f})")

                    if dist > self.recalc_threshold:
                        current_emb = temp_slot.compute_embedding(frame)
                        updated_baselines[lid] = current_emb

            except Exception as e:
                logger.error(f"Failed checking slot {lid}: {e}")

        if updated_baselines:
            await self.db.save_baselines_batch(updated_baselines)
            logger.info(f"Updated {len(updated_baselines)} baselines")

        if mismatches > 0:
            logger.critical(f"{mismatches} SLOT(S) WITH MISMATCHES - MANUAL INTERVENTION REQUIRED")

    async def _initialize_monitoring(self):
        """Initialize slot states from database"""
        logger.info("\n" + "=" * 70)
        logger.info("INITIALIZING MONITORING")
        logger.info("=" * 70)

        occupied_slots = await self.db.fetch_occupied_slots()
        occupied_lids = {lid: pid for lid, pid in occupied_slots}

        baselines = await self.db.fetch_all_baselines()

        logger.info(f"Found {len(occupied_lids)} occupied slots")
        logger.info(f"Found {len(baselines)} baselines")

        for lid, baseline in baselines.items():
            if lid not in self.rois:
                continue

            is_occupied = lid in occupied_lids

            self.slots[lid] = Slot(
                lid=lid,
                roi_coords=self.rois[lid],
                baseline_emb=baseline,
                is_occupied=is_occupied,
            )

            status = "OCCUPIED" if is_occupied else "EMPTY"
            pid = occupied_lids.get(lid, "N/A")
            logger.info(f"  Slot {lid}: {status}" + (f" (PID: {pid})" if is_occupied else ""))

        logger.info(f"Initialized {len(self.slots)} slots")

    async def _create_workers(self):
        """Create async worker pool"""
        logger.info("\n" + "=" * 70)
        logger.info("CREATING WORKER POOL")
        logger.info("=" * 70)

        if not self.slots:
            raise RuntimeError("No slots initialized!")

        # Initialize alarm controller
        self.alarm = AlarmController()

        # If SocketIO provided, attach it to alarm controller
        if self.socketio:
            self.alarm.set_socketio(self.socketio)
            logger.info("Alarm controller connected to SocketIO")

        # Create worker pool
        slot_list = list(self.slots.values())
        self.worker_pool = WorkerPool(
            num_workers=self.num_workers,
            slots=slot_list,
            frame_buffer=self.frame_buffer,
            db=self.db,
            alarm=self.alarm,
            mismatch_threshold=self.mismatch_threshold,
            recalc_threshold=self.recalc_threshold,
            grace_period=self.grace_period,
        )

        logger.info(f"Worker pool created: {self.num_workers} workers")

    async def run(self):
        """Main monitoring loop (headless)"""
        logger.info("\n" + "=" * 70)
        logger.info("STARTING HEADLESS MONITORING")
        logger.info("=" * 70)
        logger.info("Event-driven monitoring active")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 70 + "\n")

        # Start workers
        await self.worker_pool.start_all()

        # Status reporting task
        status_task = asyncio.create_task(self._status_reporter())

        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass

    async def _status_reporter(self):
        """Periodic status reporting"""
        try:
            while True:
                await asyncio.sleep(30)  # Report every 30s
                await self._print_status()
        except asyncio.CancelledError:
            pass

    async def _print_status(self):
        """Print system status"""
        logger.info("\n" + "=" * 70)
        logger.info("SYSTEM STATUS")
        logger.info("=" * 70)

        if self.camera:
            try:
                cam_metrics = self.camera.get_metrics()
                logger.info(f"Camera: {cam_metrics['frame_count']} frames, "
                            f"{cam_metrics['active_subscribers']} subscribers")
            except Exception as e:
                logger.warning(f"Camera metrics unavailable: {e}")

        if self.worker_pool:
            try:
                worker_metrics = self.worker_pool.get_metrics()
                logger.info(f"Workers: {worker_metrics['total_frames_processed']} frames, "
                            f"avg_dist={worker_metrics['avg_distance']:.4f}")
                logger.info(f"Alarms: {worker_metrics['total_alarms']}, "
                            f"Errors: {worker_metrics['total_errors']}")
            except Exception as e:
                logger.warning(f"Worker metrics unavailable: {e}")

        if self.alarm:
            try:
                alarm_status = self.alarm.get_status()
                if alarm_status['active']:
                    logger.warning(f"ALARM ACTIVE: {alarm_status['mismatch_count']} mismatches")
            except Exception as e:
                logger.warning(f"Alarm status unavailable: {e}")

    async def shutdown(self):
        """Graceful shutdown"""
        logger.info("\n" + "=" * 70)
        logger.info("SHUTTING DOWN")
        logger.info("=" * 70)

        if self.worker_pool:
            await self.worker_pool.stop_all()

        if self.frame_buffer:
            self.frame_buffer.stop_capture()

        if self.db:
            await self.db.close()

        await self._print_status()
        logger.info("\nShutdown complete")

    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"\nSignal {signum} received")
        self._shutdown_event.set()

    # ============================================================
    # PUBLIC API (for integration with Flask app)
    # ============================================================

    def start(self):
        """Start monitor in background thread"""
        import threading

        def run_async():
            asyncio.run(self._async_main())

        self._thread = threading.Thread(target=run_async, daemon=True, name="SlotMonitor")
        self._thread.start()
        logger.info("Slot monitor started in background thread")

    async def _async_main(self):
        """Async entry point for threading"""
        try:
            await self.setup()
            await self.run()
        except KeyboardInterrupt:
            logger.info("\nKeyboard interrupt")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            await self.shutdown()


async def main():
    """Standalone entry point"""
    config = {
        "camera_id": 1,
        "camera_width": 1280,
        "camera_height": 720,
        "camera_fps": 30,
        "num_workers": 4,
        "mismatch_threshold": 0.15,
        "recalc_threshold": 0.05,
        "grace_period": 5.0,
        "db_host": "localhost",
        "db_port": 5432,
        "db_name": "PhoneBoxDB",
        "db_user": "admin",
        "db_password": "admin",
    }

    system = HeadlessSlotMonitor(**config)

    if sys.platform != 'win32':
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: system.signal_handler(s, None))

    try:
        await system.setup()
        await system.run()
    except KeyboardInterrupt:
        logger.info("\nKeyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        await system.shutdown()


if __name__ == "__main__":
    asyncio.run(main())