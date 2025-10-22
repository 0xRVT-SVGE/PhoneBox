# server.py
import asyncio
import threading
import time
from typing import Dict, Any

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

from back_end.Database.API.phones_API import phones_bp
from back_end.Database.API.students_API import students_bp
from back_end.scanner import (
    scan_request,
    start_background_scanner,
    get_latest_frame,
    auth_status,
)

# ------------------- SETUP ------------------- #
app = Flask(__name__)
CORS(app)

app.register_blueprint(students_bp, url_prefix="/api/students")
app.register_blueprint(phones_bp, url_prefix="/api/phones")

# start the scanner background thread (writes latest_frame)
start_background_scanner()

# Global set of peer connections for cleanup
pcs = set()

# Background asyncio event loop (runs in its own thread)
async_loop: asyncio.AbstractEventLoop = None  # will be set in main


# ------------------- WEBRTC TRACK ------------------- #
class CameraVideoTrack(VideoStreamTrack):
    """Pull frames from scanner.get_latest_frame() for WebRTC."""

    def __init__(self):
        super().__init__()

    async def recv(self) -> VideoFrame:
        # Wait until a frame is available
        frame = get_latest_frame()
        if frame is None:
            # no frame yet — sleep a little and return a blank frame by retrying
            await asyncio.sleep(0.05)
            # returning after sleep: try again recursively (aiortc expects a VideoFrame)
            frame = get_latest_frame()
            if frame is None:
                # fallback: create a tiny black frame
                import numpy as np
                arr = np.zeros((240, 320, 3), dtype=np.uint8)
                vframe = VideoFrame.from_ndarray(arr, format="bgr24")
                vframe.pts, vframe.time_base = await self.next_timestamp()
                return vframe

        # convert the BGR numpy frame to VideoFrame
        vframe = VideoFrame.from_ndarray(frame, format="bgr24")
        vframe.pts, vframe.time_base = await self.next_timestamp()
        return vframe


# ------------------- ASYNC OFFER HANDLER ------------------- #
async def _handle_offer(offer_sdp: str, offer_type: str) -> Dict[str, Any]:
    """
    Coroutine that builds a RTCPeerConnection, attaches CameraVideoTrack,
    sets remote description and creates & sets local description (answer).
    Returns a dict with keys 'sdp' and 'type'.
    """
    pc = RTCPeerConnection()
    pcs.add(pc)

    # attach track
    pc.addTrack(CameraVideoTrack())

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        print(f"[webrtc] connection state: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            try:
                await pc.close()
            except Exception:
                pass
            pcs.discard(pc)

    # set remote description and create answer
    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


# ------------------- SIGNALING ------------------- #
@app.route("/offer", methods=["POST"])
def offer():
    global async_loop
    params = request.get_json()
    if not params or "sdp" not in params or "type" not in params:
        return jsonify({"error": "invalid offer"}), 400

    offer_sdp = params["sdp"]
    offer_type = params["type"]

    # schedule the coroutine on the background asyncio loop
    try:
        future = asyncio.run_coroutine_threadsafe(_handle_offer(offer_sdp, offer_type), async_loop)
        result = future.result(timeout=10)  # wait up to 10s
    except Exception as e:
        return jsonify({"error": f"failed to handle offer: {e}"}), 500

    return jsonify(result)


# ------------------- SCANNER ROUTES ------------------- #
@app.route("/start_scan", methods=["POST"])
def start_scan():
    # reset scanner flags in module
    from back_end import scanner

    scanner.stop_requested = False
    scanner.scan_results.update({
        "face_verified": False,
        "barcode_verified": False,
        "current_name": "Idle"
    })
    scanner.auth_status.update({"authorized": False, "user": None})

    if not scan_request["running"]:
        scan_request["running"] = True
        scan_request["start_time"] = time.time()
        return jsonify({"status": "scanning started"})
    return jsonify({"status": "already running"}), 400


@app.route("/stop_scan", methods=["POST"])
def stop_scan():
    from back_end import scanner
    scanner.stop_requested = True
    scan_request["running"] = False
    scanner.auth_status.update({"authorized": False, "user": None})
    return jsonify({"status": "scanning stopped"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify(auth_status)


# ------------------- ENTRY ------------------- #
if __name__ == "__main__":
    # Create and start the background asyncio loop (used for aiortc)
    async_loop = asyncio.new_event_loop()

    def _start_loop(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_start_loop, args=(async_loop,), daemon=True)
    t.start()

    # Run Flask app (sync)
    from werkzeug.serving import run_simple
    run_simple("0.0.0.0", 5000, app, use_reloader=False)
