# ============================================================
# FILE: back_end/server/app.py
# ============================================================
"""
Flask application factory.
"""

import threading
import logging

from flask import Flask
from flask_cors import CORS
from flask_socketio import SocketIO
from back_end.Database.API.students_API import students_bp
from back_end.Database.API.phones_API import phones_bp
from back_end.Database.logging_config import setup_logging
from back_end.config import (
    CameraConfig      as _CC,
    DatabaseConfig    as _DC,
    SlotMonitorConfig as _SMC,
)
from back_end.secrets import Secrets
#from back_end.camera_manager import cam_mgr

# Setup logging first
setup_logging(log_level=logging.INFO)
logger = logging.getLogger(__name__)

# Global references
_slot_monitor = None
_slot_operations = None


def create_app(stop_event: threading.Event = None):
    """
    Create and configure the Flask application.

    Initializes:
    - Flask app with CORS
    - API blueprints (students, phones)
    - SocketIO
    - Slot monitoring system (HeadlessSlotMonitor)
    - Slot operations (DVW support)

    Args:
        stop_event: Shared threading.Event from server_main.
                    Passed into HeadlessSlotMonitor so it exits cleanly
                    when the event is set. A private event is created if
                    None (useful for tests).
    """
    global _slot_monitor, _slot_operations

    if stop_event is None:
        stop_event = threading.Event()

    app = Flask(__name__)

    CORS(app)

    # ============================================================
    # REGISTER API BLUEPRINTS
    # ============================================================
    app.register_blueprint(students_bp, url_prefix="/api/students")
    app.register_blueprint(phones_bp, url_prefix="/api/phones")
    logger.info("API blueprints registered")

    # ============================================================
    # CREATE SOCKETIO
    # ============================================================
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode='threading',
        logger=True,
        engineio_logger=False
    )
    logger.info("SocketIO initialized")

    _initialize_monitoring(socketio, stop_event)

    logger.info("Flask application ready")
    return app, socketio


def _initialize_monitoring(socketio, stop_event: threading.Event):
    global _slot_monitor, _slot_operations

    try:
        from back_end.slot_monitor.headless_slot_monitor import HeadlessSlotMonitor
        from back_end.slot_monitor.slot_operations import SlotOperations

        CONFIG = {
        "stop_event": stop_event,

        # Camera
        "camera_id": _CC.BOTTOM_CAM_INDEX, # cam_mgr.index("bottom_cam") slot monitor
        "camera_width": _CC.BOTTOM_CAM_WIDTH,
        "camera_height": _CC.BOTTOM_CAM_HEIGHT,
        "camera_fps": _CC.BOTTOM_CAM_FPS,

        # Grid (auto-detected from DB)
        "grid_rows": None,
        "grid_cols": None,

        # Workers
        "num_workers": _SMC.NUM_WORKERS,

        # Thresholds
        "mismatch_threshold": _SMC.MISMATCH_THRESHOLD,
        "recalc_threshold": _SMC.RECALC_THRESHOLD,
        "grace_period": _SMC.GRACE_PERIOD,

        # Database
        "db_host": _DC.ASYNC_HOST,
        "db_port": _DC.ASYNC_PORT,
        "db_name": _DC.ASYNC_DATABASE,
        "db_user": Secrets.DB_USER,
        "db_password": Secrets.DB_PASSWORD,

        "socketio": socketio,
        }

        _slot_monitor = HeadlessSlotMonitor(**CONFIG)
        # monitor reference is wired in after monitor.start() via set_monitor_components()
        _slot_operations = SlotOperations(monitor=None)
        logger.info("Slot monitor created (not started yet)")

    except Exception as e:
        logger.error(f"Failed to initialize monitoring: {e}", exc_info=True)
        _slot_monitor = None
        _slot_operations = None


def get_slot_monitor():
    return _slot_monitor


def get_slot_operations():
    return _slot_operations


def set_monitor_components(monitor):
    """Wire the live HeadlessSlotMonitor into SlotOperations after monitor.start()."""
    if _slot_operations:
        _slot_operations.set_monitor(monitor)
        logger.info("Monitor connected to SlotOperations")
    else:
        logger.warning("SlotOperations not initialized — cannot set monitor")



def get_monitor_status() -> dict:
    if not _slot_monitor:
        return {"status": "offline", "message": "Monitor not initialized"}
    return {
        "status": "online",
        "monitor_active": True,
        "operations_active": _slot_operations is not None,
    }