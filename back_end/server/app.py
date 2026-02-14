# ============================================================
# FILE: back_end/server/app.py (HYBRID - COMPLETE VERSION)
# ============================================================
"""
Flask application factory with integrated slot monitoring.

Combines:
- Existing API blueprints (students, phones)
- New HeadlessSlotMonitor (async, event-driven)
- DVW system support
- Backward compatibility
"""

from flask import Flask
from flask_cors import CORS
from flask_socketio import SocketIO
from back_end.Database.API.students_API import students_bp
from back_end.Database.API.phones_API import phones_bp
from back_end.Database.logging_config import setup_logging
import logging

# Setup logging first
setup_logging(log_level=logging.INFO)
logger = logging.getLogger(__name__)

# Global references
_slot_monitor = None
_slot_operations = None


def create_app():
    """
    Create and configure Flask application.

    Initializes:
    - Flask app with CORS
    - API blueprints (students, phones)
    - SocketIO
    - Slot monitoring system (HeadlessSlotMonitor)
    - Slot operations (DVW support)

    Returns:
        tuple: (app, socketio)
    """
    global _slot_monitor, _slot_operations

    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'

    # Enable CORS
    CORS(app)

    # ============================================================
    # REGISTER API BLUEPRINTS (Existing functionality)
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

    # ============================================================
    # INITIALIZE SLOT MONITORING
    # ============================================================
    logger.info("=" * 60)
    logger.info("Initializing slot monitoring system...")
    logger.info("=" * 60)

    _initialize_monitoring(socketio)

    logger.info("=" * 60)
    logger.info("Flask application initialized successfully")
    logger.info("=" * 60)

    return app, socketio


def _initialize_monitoring(socketio):
    """
    Initialize slot monitoring and operations.

    Creates:
    - HeadlessSlotMonitor (async, event-driven, no GUI)
    - SlotOperations (DVW support)

    Note: Monitor and embedder are connected later via set_monitor_components()
          after the monitor is fully started in server_main.py

    Args:
        socketio: Flask-SocketIO instance for alarm notifications
    """
    global _slot_monitor, _slot_operations

    try:
        from back_end.slot_monitor.headless_slot_monitor import HeadlessSlotMonitor
        from back_end.slot_monitor.slot_operations import SlotOperations

        # ============================================================
        # CONFIGURATION
        # ============================================================
        CONFIG = {
            # Camera
            "camera_id": 1,  # Bottom camera for slot monitoring
            "camera_width": 1280,
            "camera_height": 720,
            "camera_fps": 30,

            # Grid (auto-calculated from database)
            "grid_rows": None,  # Auto-detect
            "grid_cols": None,  # Auto-detect

            # Workers
            "num_workers": 4,  # Adjust based on CPU cores

            # Thresholds
            "mismatch_threshold": 0.15,  # Alarm trigger threshold
            "recalc_threshold": 0.05,  # Baseline update threshold
            "grace_period": 15.0,  # Seconds before alarm (matches old system)

            # Database
            "db_host": "localhost",
            "db_port": 5432,
            "db_name": "PhoneBoxDB",
            "db_user": "admin",
            "db_password": "admin",

            # SocketIO
            "socketio": socketio,  # For alarm events
        }

        # ============================================================
        # CREATE HEADLESS MONITOR
        # ============================================================
        logger.info("Creating HeadlessSlotMonitor with config:")
        logger.info(
            f"  Camera: {CONFIG['camera_id']} ({CONFIG['camera_width']}x{CONFIG['camera_height']} @ {CONFIG['camera_fps']}fps)")
        logger.info(f"  Workers: {CONFIG['num_workers']}")
        logger.info(f"  Thresholds: mismatch={CONFIG['mismatch_threshold']}, recalc={CONFIG['recalc_threshold']}")
        logger.info(f"  Grace period: {CONFIG['grace_period']}s")

        _slot_monitor = HeadlessSlotMonitor(**CONFIG)

        # ============================================================
        # CREATE SLOT OPERATIONS
        # ============================================================
        _slot_operations = SlotOperations(
            monitor=None,  # Will be set after monitor starts
            embedder=None  # Will be set after monitor starts
        )

        logger.info("Monitoring system initialized (not started yet)")
        logger.info("Monitor will be started in server_main.py")
        logger.info("Components will be connected via set_monitor_components()")

    except Exception as e:
        logger.error(f"Failed to initialize monitoring: {e}", exc_info=True)
        _slot_monitor = None
        _slot_operations = None


def get_slot_monitor():
    """
    Get the global slot monitor instance.

    Returns:
        HeadlessSlotMonitor or None
    """
    return _slot_monitor


def get_slot_operations():
    """
    Get the global slot operations instance.

    Returns:
        SlotOperations or None
    """
    return _slot_operations


def set_monitor_components(monitor, embedder):
    """
    Set monitor and embedder references in slot operations.

    CRITICAL: This must be called after the monitor is fully started.

    This allows SlotOperations to:
    - Pause/resume slot monitoring during DVW operations
    - Capture baselines after deposit/withdrawal

    Args:
        monitor: WorkerPool instance from HeadlessSlotMonitor
        embedder: Camera instance from HeadlessSlotMonitor

    Example:
        # In server_main.py:
        slot_monitor = get_slot_monitor()
        if slot_monitor:
            slot_monitor.start()
            set_monitor_components(
                monitor=slot_monitor.worker_pool,
                embedder=slot_monitor.camera
            )
    """
    global _slot_operations

    if _slot_operations:
        _slot_operations.set_monitor(monitor)
        _slot_operations.set_embedder(embedder)
        logger.info("Monitor and embedder attached to slot operations")
        logger.info("DVW operations can now pause monitoring and capture baselines")
    else:
        logger.warning("Slot operations not initialized, cannot set components")


def get_monitor_status():
    """
    Get current monitoring system status.

    Returns:
        dict: Status information
    """
    if not _slot_monitor:
        return {
            "status": "offline",
            "message": "Monitor not initialized"
        }

    return {
        "status": "online",
        "monitor_active": _slot_monitor is not None,
        "operations_active": _slot_operations is not None,
    }


def shutdown_monitoring():
    """
    Gracefully shutdown monitoring system.

    Note: The HeadlessSlotMonitor handles its own async shutdown
          through its shutdown() method when the main thread exits.
    """
    global _slot_monitor, _slot_operations

    logger.info("Shutting down monitoring system...")
    _slot_monitor = None
    _slot_operations = None
    logger.info("Monitoring system shutdown complete")