import threading, asyncio
from back_end.server.app import create_app
from back_end.server.webrtc_handler import webrtc_bp, async_loop
from back_end.scanner_loop import scanner_loop
from back_end.scanner_state import scanner_state

app, socketio = create_app()
app.register_blueprint(webrtc_bp, url_prefix="/webrtc")

# Pass socketio to scanner_state for emissions
scanner_state.set_socketio(socketio)

def _start_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# --- WebSocket Events ---
@socketio.on("toggle_scan")
def handle_toggle_scan(_):
    new_state = not scanner_state.scan_request["running"]


    scanner_state.stop_requested = not new_state
    scanner_state.scan_request["running"] = new_state

    if new_state:
        scanner_state.update_last_barcode()
        scanner_state.auth_status = {"authorized": False, "user": None}
        scanner_state_scan_results = {
            "face_verified": False,
            "barcode_verified": False,
            "current_name": "Idle",
            "badge_timeout_exceeded": False,
        }
    scanner_state._emit_socket()

@socketio.on("get_status")
def handle_get_status(_):
    scanner_state._emit_socket()

# --- Threads ---
if __name__ == "__main__":
    threading.Thread(target=_start_async_loop, args=(async_loop,), daemon=True).start()
    threading.Thread(target=lambda: scanner_loop(debugwindow=False, debugroi=True), daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
