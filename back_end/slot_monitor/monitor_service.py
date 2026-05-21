#!/usr/bin/env python3
# ============================================================
# FILE: back_end/slot_monitor/monitor_service.py
# ============================================================
"""
Opt #39 — Standalone Slot Monitor Service.

Run this as a SEPARATE PROCESS from the Flask servers when
MonitorServiceConfig.ENABLED = True in config.py:

    # Terminal 1 — slot monitor (one instance, owns the camera)
    python -m back_end.slot_monitor.monitor_service

    # Terminal 2 … N — Flask API servers (stateless, any count)
    python -m back_end.server.server_main

How it works
────────────
1. This process boots a HeadlessSlotMonitor normally.
2. AlarmController.set_socketio() receives a RedisSocketIOBridge instead
   of a real Flask-SocketIO object.
3. Every alarm_controller.sio.emit(event, data) call publishes the event
   to Redis channel "phonebox:alarms".
4. Each Flask server runs an AlarmSubscriber background thread that
   listens on the same channel and forwards events to its own SocketIO
   instance (and thus to all connected WebSocket clients).

This gives:
  • True separation of the CV/camera workload from the web layer.
  • Horizontal scaling: add Flask instances behind nginx; they all get
    alarm events from the single monitor via Redis.
  • GPU/multi-core pinning: run the monitor on a core with camera access;
    run Flask on other cores.

Environment:
    PHONEBOX_DEV=1   Single-camera role simulation (for testing).

Shutdown:
    SIGINT / SIGTERM → graceful slot monitor stop → process exits.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# ── Python path: allow running as a module from the repo root ─────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from Backup.back_end.config import (
    CameraConfig        as _CC,
    DatabaseConfig      as _DC,
    SlotMonitorConfig   as _SMC,
    MonitorServiceConfig as _MSC,
)
from Backup.back_end.secrets import Secrets
from Backup.back_end.Database.logging_config import setup_logging

logger = logging.getLogger(__name__)
setup_logging(log_level=logging.INFO)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not _MSC.ENABLED:
        logger.error(
            "MonitorServiceConfig.ENABLED is False.\n"
            "Set it to True in back_end/config.py before running "
            "the monitor as a standalone service."
        )
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("PHONEBOX SLOT MONITOR SERVICE — STARTING")
    logger.info("=" * 60)

    # ── Redis bridge ──────────────────────────────────────────────────────────
    from Backup.back_end.slot_monitor.redis_bridge import AlarmPublisher, RedisSocketIOBridge
    try:
        publisher = AlarmPublisher()
        bridge    = RedisSocketIOBridge(publisher)
        logger.info(
            f"[MonitorService] Redis publisher ready: "
            f"{_MSC.REDIS_HOST}:{_MSC.REDIS_PORT} "
            f"channel={_MSC.ALARM_CHANNEL}"
        )
    except Exception as e:
        logger.error(f"[MonitorService] Cannot connect to Redis: {e}")
        sys.exit(1)

    # ── Monitor config ────────────────────────────────────────────────────────
    dev_mode = os.getenv("PHONEBOX_DEV", "0") == "1"
    if dev_mode:
        logger.warning("[MonitorService] DEV MODE — single-camera simulation active")

    config = {
        "stop_event": None,       # set below via asyncio Event

        "camera_id":     _CC.BOTTOM_CAM_INDEX,
        "camera_width":  _CC.BOTTOM_CAM_WIDTH,
        "camera_height": _CC.BOTTOM_CAM_HEIGHT,
        "camera_fps":    _CC.BOTTOM_CAM_FPS,

        "grid_rows": None,
        "grid_cols": None,

        "num_workers": _SMC.NUM_WORKERS,

        "mismatch_threshold": _SMC.MISMATCH_THRESHOLD,
        "recalc_threshold":   _SMC.RECALC_THRESHOLD,
        "grace_period":       _SMC.GRACE_PERIOD,

        "db_host":     _DC.ASYNC_HOST,
        "db_port":     _DC.ASYNC_PORT,
        "db_name":     _DC.ASYNC_DATABASE,
        "db_user":     Secrets.DB_USER,
        "db_password": Secrets.DB_PASSWORD,

        "socketio": bridge,   # ← Redis bridge replaces Flask-SocketIO
    }

    # ── asyncio event loop ────────────────────────────────────────────────────
    import threading

    loop       = asyncio.new_event_loop()
    stop_event = threading.Event()
    config["stop_event"] = stop_event

    asyncio.set_event_loop(loop)

    from Backup.back_end.slot_monitor.headless_slot_monitor import HeadlessSlotMonitor
    monitor = HeadlessSlotMonitor(**config)

    # ── Shutdown handler ──────────────────────────────────────────────────────

    def _shutdown(sig, frame):
        logger.info(f"[MonitorService] Signal {sig} — stopping monitor")
        stop_event.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── ROI calibration (headless: skip interactive UI, load json directly) ───
    # When running standalone, calibration must have been done already by the
    # server process (or by running roi_calibration.py manually once).
    # The monitor reads rois_bottom.json from the tools/ directory.
    _check_roi_file()

    # ── Start monitor ─────────────────────────────────────────────────────────
    monitor.start()
    logger.info("[MonitorService] Slot monitor started")

    # Await setup completion
    if not monitor._setup_complete.wait(timeout=30):
        logger.error("[MonitorService] Monitor setup timed out — exiting")
        stop_event.set()

    # ── Evidence storage pruner ───────────────────────────────────────────────
    try:
        from Backup.back_end.server.evidence_storage import evidence_store
        evidence_store.start()
        logger.info("[MonitorService] Evidence storage pruner started")
    except Exception as e:
        logger.warning(f"[MonitorService] Evidence storage pruner failed to start: {e}")

    # ── Block until stop_event ────────────────────────────────────────────────
    logger.info("[MonitorService] Running — SIGINT or SIGTERM to stop")
    stop_event.wait()

    logger.info("[MonitorService] Shutting down")
    monitor.join(timeout=10.0)
    logger.info("[MonitorService] Exited cleanly")


def _check_roi_file() -> None:
    """Warn if the box-specific rois_bottom_{slug}.json is missing."""
    from Backup.back_end.config import ServerConfig as _SVC
    slug     = getattr(_SVC, "BOX_SLUG", "") or "box_1"
    roi_name = f"rois_bottom_{slug}.json"
    roi_path = Path(__file__).resolve().parent / "tools" / roi_name
    if not roi_path.exists():
        logger.warning(
            f"[MonitorService] {roi_name} not found — auto-grid will be used.\n"
            f"Run roi_calibration.py with PHONEBOX_BOX_SLUG={slug!r} to generate it,\n"
            f"then ensure it is at {roi_path}"
        )


if __name__ == "__main__":
    main()
