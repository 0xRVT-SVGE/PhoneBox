# server.py
import asyncio
import json
import cv2
import time
from flask import Flask, jsonify, request
from flask_cors import CORS
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
import threading

from back_end.scanner import (
    scan_request,
    start_background_scanner,
    get_latest_frame,
    auth_status,
)

# Import your API blueprints
from back_end.Database.API.students_API import students_bp
from back_end.Database.API.phones_API import phones_bp


# ------------------- SETUP ------------------- #
app = Flask(__name__)
CORS(app)

app.register_blueprint(students_bp, url_prefix="/api/students")
app.register_blueprint(phones_bp, url_prefix="/api/phones")

start_background_scanner()

pcs = set()


# ------------------- WEBRTC TRACK ------------------- #
class CameraVideoTrack(VideoStreamTrack):
    """Pulls frames from scanner.py for WebRTC streaming."""

    def __init__(self):
        super().__init__()

    async def recv(self):
        frame = get_latest_frame()
        if frame is None:
            await asyncio.sleep(0.05)
            return None

        # Convert to VideoFrame
        video_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame.pts, video_frame.time_base = await self.next_timestamp()
        return video_frame


# ------------------- SIGNALING ------------------- #
@app.route("/offer", methods=["POST"])
def offer():
    params = request.get_json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state_change():
        print(f"Connection state: {pc.connectionState}")
        if pc.connectionState == "failed" or pc.connectionState == "closed":
            await pc.close()
            pcs.discard(pc)

    # Add our video track
    pc.addTrack(CameraVideoTrack())

    async def set_remote():
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return answer

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    answer = loop.run_until_complete(set_remote())

    return jsonify({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })


# ------------------- SCANNER ROUTES ------------------- #
@app.route("/start_scan", methods=["POST"])
def start_scan():
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
    import threading
    from werkzeug.serving import run_simple

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    run_simple("0.0.0.0", 5000, app, use_reloader=False)
