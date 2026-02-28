# ============================================================
# FILE: back_end/server/server_main.py
# ============================================================
"""
Application entry point.

Shutdown model:
    One threading.Event (_stop_event) is created here and passed to every
    module that needs it. On SIGINT/SIGTERM:
      1. _stop_event.set()           — all watchers begin their own cleanup
      2. webrtc_handler.shutdown()   — closes WebRTC connections + loop
      3. slot_monitor.join()         — waits for async monitor to finish
      4. os._exit(0)

Each module is responsible for shutting down what it owns:
  - scanner_loop           → stops scan_worker
  - headless_slot_monitor  → stops worker_pool, frame_buffer, db
  - webrtc_handler         → closes peer connections, stops async_loop
  - op_ctx                 → starts and stops its own cleanup thread
"""

import threading
import logging
import signal
import os
from back_end.slot_monitor.admin.admin_ops_handler import register_admin_handlers
from back_end.server.app import create_app, get_slot_monitor, get_slot_operations, set_monitor_components
from back_end.server import webrtc_handler
from back_end.server.webrtc_handler import webrtc_bp
from back_end.scanner_loop import scanner_loop
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import scan_worker
from back_end.slot_monitor.ops_handler import register_dvw_handlers
from back_end.slot_monitor.services.operation_context import op_ctx
from flask import request

logger = logging.getLogger(__name__)

DEBUG_ROI = True
DEBUG_WINDOW = False

# ============================================================
# SHARED SHUTDOWN SIGNAL
# One event. Set it — everything stops.
# ============================================================
_stop_event = threading.Event()

# ============================================================
# APP SETUP
# ============================================================
app, socketio = create_app(stop_event=_stop_event)
app.register_blueprint(webrtc_bp, url_prefix="/webrtc")
scanner_state.set_socketio(socketio)


# ============================================================
# SHUTDOWN HANDLER
# ============================================================

def _shutdown(sig, frame):
    """
    Signal handler — coordinates a clean exit.

    Only server_main logic lives here. Each module handles its
    own internal teardown when it sees stop_event or is called once.
    """
    logger.info("=" * 60)
    logger.info(f"Signal {sig} received — shutting down")
    logger.info("=" * 60)

    # 1. Signal every module that watches stop_event
    _stop_event.set()

    # 2. WebRTC: close connections and stop the loop
    webrtc_handler.shutdown()

    # 3. DVW cleanup thread
    op_ctx.stop_cleanup_thread()
    op_ctx.clear_all()

    # 4. Wait for slot monitor to finish its async shutdown
    slot_monitor = get_slot_monitor()
    if slot_monitor:
        slot_monitor.join(timeout=8.0)

    logger.info("All modules stopped — exiting")
    os._exit(0)


# ============================================================
# WEBSOCKET EVENTS — LEGACY SCANNER
# ============================================================

@socketio.on("toggle_scan")
def handle_toggle_scan(_):
    from back_end.scanner_worker import start_scan, stop_scan
    client_id = request.sid
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
        start_scan(client_id)
    else:
        logger.info(f"Stopping scan for client {client_id}")
        stop_scan()

    scanner_state.emit_to_requester()


@socketio.on("get_status")
def handle_get_status(_):
    scanner_state.emit_to_requester()


# ============================================================
# MAIN ENTRY POINT
# ============================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("PHONE BOX SERVER — STARTING")
    logger.info("=" * 60)

    # 1. Scan worker (face + barcode)
    threading.Thread(target=scan_worker, daemon=True, name="ScanWorker").start()
    logger.info("Scan worker started")

    # 2. WebRTC async loop
    webrtc_handler.start()

    # 3. Scanner loop (face/barcode camera)
    threading.Thread(
        target=lambda: scanner_loop(
            stop_event=_stop_event,
            debugwindow=DEBUG_WINDOW,
            debugroi=DEBUG_ROI,
        ),
        daemon=True,
        name="ScannerLoop",
    ).start()
    logger.info("Scanner loop started")

    # 4. DVW + admin resolution system
    slot_ops = get_slot_operations()
    if not slot_ops:
        logger.error("Slot operations not available — DVW system DISABLED")
    else:
        register_dvw_handlers(socketio, slot_ops)
        op_ctx.start_cleanup_thread()
        logger.info("DVW system ready")

        # Admin resolution handlers need the alarm, which is only available
        # after the monitor has started (step 5). Wire them after monitor.start().
        # See the addition after slot_monitor.start() below.

    # 5. Slot monitor
    slot_monitor = get_slot_monitor()
    if slot_monitor:
        slot_monitor.start()
        logger.info("Slot monitor started")
        set_monitor_components(slot_monitor)

        # Wire admin handlers now that alarm exists
        if slot_ops and slot_monitor.alarm:
            register_admin_handlers(socketio, slot_ops, slot_monitor.alarm)
            logger.info("Admin resolution system ready")
        else:
            logger.warning("Admin resolution system not started — alarm unavailable")
    else:
        logger.warning("Slot monitor not available")

    # 6. Signal handlers
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    logger.info("Ready — press Ctrl+C to stop")

    # 7. Run Flask-SocketIO (blocking)
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)