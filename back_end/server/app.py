# back_end/server/app.py
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
    CameraConfig         as _CC,
    DatabaseConfig       as _DC,
    SlotMonitorConfig    as _SMC,
    MonitorServiceConfig as _MSC,   # Opt #39
)

from back_end.secrets import Secrets

# Optimization #24: HTTP gzip compression (~60-80% payload reduction on JSON)
# pip install flask-compress
try:
    from flask_compress import Compress as _Compress
    _COMPRESS_AVAILABLE = True
except ImportError:
    _COMPRESS_AVAILABLE = False

setup_logging(log_level=logging.INFO)
logger = logging.getLogger(__name__)

_slot_monitor = None
_slot_operations = None


def create_app(stop_event: threading.Event = None):
    global _slot_monitor, _slot_operations

    if stop_event is None:
        stop_event = threading.Event()

    app = Flask(__name__)
    CORS(app)

    # Optimization #24: enable gzip for all JSON/text responses
    if _COMPRESS_AVAILABLE:
        _Compress(app)
        logger.info("HTTP response compression enabled (flask-compress)")
    else:
        logger.warning(
            "flask-compress not installed — run: pip install flask-compress"
        )

    app.register_blueprint(students_bp, url_prefix="/api/students")
    app.register_blueprint(phones_bp, url_prefix="/api/phones")
    logger.info("API blueprints registered")

    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode='threading',
        logger=True,
        engineio_logger=False
    )
    logger.info("SocketIO initialized")

    _initialize_monitoring(socketio, stop_event)

    # Opt #39: start Redis alarm subscriber so all Flask instances receive
    # alarm events even if the slot monitor runs in a separate process.
    if _MSC.ENABLED:
        try:
            from back_end.slot_monitor.redis_bridge import AlarmSubscriber
            _alarm_sub = AlarmSubscriber(socketio)
            _alarm_sub.start()
            logger.info("[App] Redis alarm subscriber started (Opt #39)")
        except Exception as e:
            logger.error(
                f"[App] Failed to start Redis alarm subscriber: {e} "
                f"-- ensure Redis is running at {_MSC.REDIS_HOST}:{_MSC.REDIS_PORT}"
            )

    logger.info("Flask application ready")
    return app, socketio



def _initialize_monitoring(socketio, stop_event: threading.Event):
    global _slot_monitor, _slot_operations

    try:
        from back_end.slot_monitor.headless_slot_monitor import HeadlessSlotMonitor
        from back_end.slot_monitor.slot_operations import SlotOperations

        CONFIG = {
            "stop_event": stop_event,

            "camera_id": _CC.BOTTOM_CAM_INDEX,
            "camera_width": _CC.BOTTOM_CAM_WIDTH,
            "camera_height": _CC.BOTTOM_CAM_HEIGHT,
            "camera_fps": _CC.BOTTOM_CAM_FPS,

            "grid_rows": None,
            "grid_cols": None,

            "num_workers": _SMC.NUM_WORKERS,

            "mismatch_threshold": _SMC.MISMATCH_THRESHOLD,
            "recalc_threshold": _SMC.RECALC_THRESHOLD,
            "grace_period": _SMC.GRACE_PERIOD,

            "db_host": _DC.ASYNC_HOST,
            "db_port": _DC.ASYNC_PORT,
            "db_name": _DC.ASYNC_DATABASE,
            "db_user": Secrets.DB_USER,
            "db_password": Secrets.DB_PASSWORD,

            "socketio": socketio,
        }

        _slot_monitor = HeadlessSlotMonitor(**CONFIG)
        _slot_operations = SlotOperations(monitor=None)
        logger.info("Slot monitor created (not started yet)")

        # Opt #39: wire Redis publisher into alarm controller when enabled
        # so alarm events are fanned out to all Flask instances via Redis.
        if _MSC.ENABLED:
            try:
                from back_end.slot_monitor.redis_bridge import AlarmPublisher
                publisher = AlarmPublisher()
                # Publisher is set after monitor.start() / alarm setup;
                # see set_monitor_components() call in server_main.py.
                _slot_monitor._pending_redis_publisher = publisher
                logger.info("[App] AlarmPublisher ready for Redis fanout (Opt #39)")
            except Exception as e:
                logger.warning(
                    f"[App] Redis publisher not available ({e}) "
                    f"-- alarm fanout disabled"
                )


    except Exception as e:
        logger.error(f"Failed to initialize monitoring: {e}", exc_info=True)
        _slot_monitor = None
        _slot_operations = None


def get_slot_monitor():
    return _slot_monitor


def get_slot_operations():
    return _slot_operations


def set_monitor_components(monitor):
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