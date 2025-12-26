# ============================================================
# FILE: back_end/server/app.py (UPDATED)
# ============================================================

from flask import Flask
from flask_cors import CORS
from flask_socketio import SocketIO
from back_end.Database.API.students_API import students_bp
from back_end.Database.API.phones_API import phones_bp
from back_end.Database.logging_config import setup_logging
import logging
import cv2

# Setup logging first
setup_logging(log_level=logging.INFO)
logger = logging.getLogger(__name__)

# Global references for slot monitoring (initialized in create_app)
slot_monitor = None
slot_operations = None


def create_app():
    """Create and configure Flask application with slot monitoring"""
    global slot_monitor, slot_operations

    app = Flask(__name__)
    CORS(app)

    # Register existing blueprints
    app.register_blueprint(students_bp, url_prefix="/api/students")
    app.register_blueprint(phones_bp, url_prefix="/api/phones")

    socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

    logger.info("=" * 60)
    logger.info("Flask application initializing...")
    logger.info("=" * 60)

    # ============================================================
    # SLOT MONITORING INITIALIZATION
    # ============================================================

    try:
        from back_end.slot_monitor.slot_camera import generate_grid_rois
        from back_end.slot_monitor.slot_monitor_main import SlotMonitor
        from back_end.slot_monitor.slot_operations import SlotOperations

        CAM_INDEX = 1
        ROWS = 5
        COLS = 10
        SPACING = 10
        GRACE_PERIOD = 15.0

        logger.info("Initializing slot monitoring system...")

        cap = cv2.VideoCapture(CAM_INDEX, cv2.CAP_DSHOW)
        if not cap.isOpened():
            logger.error(f"Failed to open camera {CAM_INDEX}")
            slot_monitor = None
            slot_operations = None
        else:
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()

            rois = generate_grid_rois(width, height, ROWS, COLS, SPACING)

            slot_monitor = SlotMonitor(
                cam_index=CAM_INDEX,
                rois=rois,
                grace_period=GRACE_PERIOD
            )

            # ✅ Initialize SlotOperations
            slot_operations = SlotOperations()
            slot_operations.set_monitor(slot_monitor)

            logger.info("Slot monitoring system initialized successfully")

    except Exception as e:
        logger.error(f"Failed to initialize slot monitoring: {e}", exc_info=True)
        slot_monitor = None
        slot_operations = None

    logger.info("Flask application initialized successfully")

    return app, socketio


def get_slot_monitor():
    """Get the global slot monitor instance"""
    return slot_monitor


def get_slot_operations():
    """Get the global slot operations instance"""
    return slot_operations
