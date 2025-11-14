# back_end/server/webrtc_handler.py
import asyncio
import numpy as np
from flask import Blueprint, jsonify, request
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from back_end.scanner_state import scanner_state

webrtc_bp = Blueprint("webrtc", __name__)
pcs = set()
async_loop = asyncio.new_event_loop()

class CameraVideoTrack(VideoStreamTrack):
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        frame = scanner_state.get_frame()
        if frame is None:
            # fallback black frame (no camera data yet)
            arr = np.zeros((480, 640, 3), dtype=np.uint8)
            frame = VideoFrame.from_ndarray(arr, format="bgr24")
        else:
            frame = VideoFrame.from_ndarray(frame, format="bgr24")

        frame.pts = pts
        frame.time_base = time_base
        return frame

async def _handle_offer(offer_sdp, offer_type):
    pc = RTCPeerConnection()
    pcs.add(pc)
    video_track = CameraVideoTrack()
    pc.addTrack(video_track)

    @pc.on("connectionstatechange")
    async def on_change():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            pcs.discard(pc)
            await pc.close()

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {
        "status": "success",
        "data": {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
    }

@webrtc_bp.route("/offer", methods=["POST"])
def offer():
    params = request.get_json()
    if not params or "sdp" not in params or "type" not in params:
        return jsonify({"status": "error", "message": "Invalid offer"}), 400
    try:
        future = asyncio.run_coroutine_threadsafe(
            _handle_offer(params["sdp"], params["type"]), async_loop
        )
        return jsonify(future.result(timeout=10))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
