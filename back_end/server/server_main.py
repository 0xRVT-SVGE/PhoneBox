# ============================================================
# FILE: back_end/server/server_main.py (CLEAN)
# ============================================================

import threading
import asyncio
import logging
from back_end.server.app import create_app, get_slot_monitor, get_slot_operations
from back_end.server.webrtc_handler import webrtc_bp, async_loop
from back_end.scanner_loop import scanner_loop
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import scan_worker, start_scan, stop_scan

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


# --- WebSocket Events ---
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


# --- WebSocket Events (Slot Operations) ---
@socketio.on("deposit_phone")
def handle_deposit_phone(data):
    """Handle phone deposit via WebSocket"""
    pid = data.get("pid")
    if not pid:
        socketio.emit("phone_operation_result", {
            "status": "error",
            "message": "Missing pid"
        })
        return

    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("phone_operation_result", {
            "status": "error",
            "message": "Slot operations not available"
        })
        return

    logger.info(f"WebSocket: Deposit request for phone {pid}")
    result = slot_ops.deposit_phone(pid)

    if result['status'] == 'success':
        socketio.emit("slot_state_changed", {
            "lid": result.get("lid"),
            "action": "deposit",
            "pid": pid
        }, broadcast=True)

    socketio.emit("phone_operation_result", result)


@socketio.on("withdraw_phone")
def handle_withdraw_phone(data):
    """Handle phone withdrawal via WebSocket"""
    pid = data.get("pid")
    if not pid:
        socketio.emit("phone_operation_result", {
            "status": "error",
            "message": "Missing pid"
        })
        return

    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("phone_operation_result", {
            "status": "error",
            "message": "Slot operations not available"
        })
        return

    logger.info(f"WebSocket: Withdraw request for phone {pid}")
    result = slot_ops.withdraw_phone(pid)

    if result['status'] == 'success':
        socketio.emit("slot_state_changed", {
            "lid": result.get("lid"),
            "action": "withdraw",
            "pid": pid
        }, broadcast=True)

    socketio.emit("phone_operation_result", result)


@socketio.on("get_slot_status")
def handle_get_slot_status(data):
    """Get specific slot status"""
    lid = data.get("lid")
    if lid is None:
        socketio.emit("slot_status", {"status": "error", "message": "Missing lid"})
        return

    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("slot_status", {"status": "error", "message": "Slot operations not available"})
        return

    result = slot_ops.get_slot_status(lid)
    socketio.emit("slot_status", result)


@socketio.on("get_all_slots")
def handle_get_all_slots(_):
    """Get all slots status"""
    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("all_slots_status", {"status": "error", "message": "Slot operations not available"})
        return

    result = slot_ops.get_all_slots()
    socketio.emit("all_slots_status", result)


@socketio.on("get_problem_slots")
def handle_get_problem_slots(_):
    """Get problem slots"""
    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("problem_slots_list", {"status": "error", "message": "Slot operations not available"})
        return

    result = slot_ops.get_problem_slots()
    socketio.emit("problem_slots_list", result)


@socketio.on("get_anomalies")
def handle_get_anomalies(data):
    """Get anomalies"""
    hours = data.get("hours", 24)
    severity = data.get("severity")

    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("anomalies_list", {"status": "error", "message": "Slot operations not available"})
        return

    result = slot_ops.get_anomalies(hours, severity)
    socketio.emit("anomalies_list", result)


@socketio.on("get_system_health")
def handle_get_system_health(_):
    """Get system health"""
    slot_ops = get_slot_operations()
    if not slot_ops:
        socketio.emit("system_health", {"status": "error", "message": "Slot operations not available"})
        return

    result = slot_ops.get_system_health()
    socketio.emit("system_health", result)


# --- Main ---
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

    # Start slot monitor
    slot_monitor = get_slot_monitor()
    if slot_monitor:
        slot_monitor.start()
        logger.info("Slot monitoring active")

    logger.info("=" * 60)
    logger.info("Starting Flask-SocketIO on 0.0.0.0:5000")
    logger.info("=" * 60)

    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)