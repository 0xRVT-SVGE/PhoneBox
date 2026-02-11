# ============================================================
# FILE: back_end/server/server_main.py (DVW INTEGRATED)
# ============================================================

import threading
import asyncio
import logging
from back_end.server.app import create_app, get_slot_monitor, get_slot_operations
from back_end.server.webrtc_handler import webrtc_bp, async_loop
from back_end.scanner_loop import scanner_loop
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import scan_worker, start_scan, stop_scan

# DVW System imports
from back_end.slot_monitor.ops_handler import register_dvw_handlers

logger = logging.getLogger(__name__)

# Create Flask app
app, socketio = create_app()
app.register_blueprint(webrtc_bp, url_prefix="/webrtc")

debugroi = True
debugwindow = False

scanner_state.set_socketio(socketio)


def _start_async_loop(loop):
    """Start asyncio event loop in separate thread"""
    asyncio.set_event_loop(loop)
    loop.run_forever()


# ============================================================
# LEGACY WEBSOCKET EVENTS (Keep for backward compatibility)
# ============================================================

@socketio.on("toggle_scan")
def handle_toggle_scan(_):
    """Handle barcode scanner toggle"""
    new_state = not scanner_state.scan_request["running"]
    scanner_state.scan_request["running"] = new_state

    if new_state:
        scanner_state.update_last_barcode()
        scanner_state.auth_status.update({"authorized": False, "user": None})
        scanner_state.scan_results.update({
            "face_verified": False,
            "barcode_verified": False,
            "current_name": "Idle",
            "badge_timeout_exceeded": False,
        })
        start_scan()
    else:
        stop_scan()

    scanner_state._emit_socket()


@socketio.on("get_status")
def handle_get_status(_):
    """Handle status request"""
    scanner_state._emit_socket()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Starting server threads...")
    logger.info("=" * 60)

    # Start worker thread
    worker_thread = threading.Thread(target=scan_worker, daemon=True, name="ScanWorker")
    worker_thread.start()

    # Start async loop
    async_thread = threading.Thread(target=_start_async_loop, args=(async_loop,), daemon=True, name="AsyncLoop")
    async_thread.start()

    # Start scanner loop
    scanner_thread = threading.Thread(target=lambda: scanner_loop(debugwindow, debugroi), daemon=True, name="ScannerLoop")
    scanner_thread.start()

    # Initialize slot operations (needed for DVW)
    slot_ops = get_slot_operations()
    if not slot_ops:
        logger.error("❌ Slot operations not available - DVW system disabled")
    else:
        # Register DVW handlers
        logger.info("Registering DVW WebSocket handlers...")
        register_dvw_handlers(socketio, slot_ops)
        logger.info("✅ DVW system registered")

    # Start slot monitor
    slot_monitor = get_slot_monitor()
    if slot_monitor:
        slot_monitor.start()
        logger.info("✅ Slot monitoring active")
    else:
        logger.warning("⚠️  Slot monitor not available")

    logger.info("=" * 60)
    logger.info("Starting Flask-SocketIO on 0.0.0.0:5000")
    logger.info("=" * 60)

    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)