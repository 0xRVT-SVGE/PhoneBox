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
    prev_state = scanner_state.scan_request["running"]
    new_state = not prev_state

    scanner_state.stop_requested = not new_state
    scanner_state.scan_request["running"] = new_state

    if new_state:
        scanner_state.update_last_barcode()

    scanner_state.emit_scan_status()

@socketio.on("get_status")
def handle_get_status(_):
    scanner_state.emit_scan_status()

# --- Threads ---
if __name__ == "__main__":
    threading.Thread(target=_start_async_loop, args=(async_loop,), daemon=True).start()
    threading.Thread(target=lambda: scanner_loop(debugwindow=False, debugroi=True), daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
