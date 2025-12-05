# back_end/server/webrtc_handler.py
import asyncio
import numpy as np

from flask import Blueprint, jsonify, request
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

from back_end.scanner_state import scanner_state
from back_end.embedding_gen import generate_embedding

webrtc_bp = Blueprint("webrtc", __name__)

# Separate sets for main and preview connections
pcs_main = set()
pcs_preview = set()

# Dedicated async loop for aiortc + async tasks
async_loop = asyncio.new_event_loop()


# ===========================================================
# Utility: build a VideoFrame from ndarray or generate black frame
# ===========================================================
def make_video_frame(frame, pts, time_base):
    if frame is None:
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        vf = VideoFrame.from_ndarray(arr, format="bgr24")
    else:
        vf = VideoFrame.from_ndarray(frame, format="bgr24")

    vf.pts = pts
    vf.time_base = time_base
    return vf


# ===========================================================
# MAIN STREAM (ROI)
# ===========================================================
class MainVideoTrack(VideoStreamTrack):
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        # Wait until a new frame is available
        while not scanner_state._main_frame_event.wait(timeout=0.01):
            await asyncio.sleep(0.001)

        frame = scanner_state.get_frame()
        scanner_state._main_frame_event.clear()
        return make_video_frame(frame, pts, time_base)


# ===========================================================
# PREVIEW STREAM (RAW)
# ===========================================================
class PreviewVideoTrack(VideoStreamTrack):
    kind = "video"

    async def recv(self):
        # Stop immediately if preview is cancelled
        if not scanner_state.preview_requested.is_set():
            raise ConnectionError("Preview not active")

        # Stop immediately if photo was taken
        if scanner_state.photo_taken_event.is_set():
            raise ConnectionError("Preview finished")

        pts, time_base = await self.next_timestamp()

        while not scanner_state._preview_frame_event.wait(timeout=0.01):
            await asyncio.sleep(0.001)

        frame = scanner_state.get_rframe()
        scanner_state._preview_frame_event.clear()
        return make_video_frame(frame, pts, time_base)


# ===========================================================
# WebRTC OFFER HANDLER
# ===========================================================
async def _handle_offer(offer_sdp, offer_type, mode):
    pc = RTCPeerConnection()

    # === Choose correct pool & track type ===
    if mode == "main":
        pcs_main.add(pc)
        video_track = MainVideoTrack()
    else:
        pcs_preview.add(pc)
        scanner_state.request_preview()
        """if not scanner_state.preview_requested.is_set():
            raise RuntimeError("Preview not requested")"""

        video_track = PreviewVideoTrack()

    pc.addTrack(video_track)

    # Cleanup on disconnect
    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in ("closed", "failed", "disconnected"):
            if mode == "main":
                pcs_main.discard(pc)
            else:
                pcs_preview.discard(pc)
            await pc.close()

    # Set remote → create answer
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=offer_sdp, type=offer_type)
    )

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {
        "status": "success",
        "data": {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        },
    }


# ===========================================================
# HTTP ENDPOINTS
# ===========================================================
@webrtc_bp.route("/offer/<mode>", methods=["POST"])
def offer(mode):
    if mode not in ("main", "preview"):
        return jsonify({"status": "error", "message": "Invalid mode"}), 400

    data = request.get_json()
    if not data or "sdp" not in data or "type" not in data:
        return jsonify({"status": "error", "message": "Invalid offer"}), 400

    try:
        future = asyncio.run_coroutine_threadsafe(
            _handle_offer(data["sdp"], data["type"], mode),
            async_loop,
        )
        return jsonify(future.result(timeout=10))

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ===========================================================
# PHOTO CAPTURE + EMBEDDING
# ===========================================================
@webrtc_bp.route("/take_photo", methods=["POST"])
def take_photo():
    try:
        scanner_state.mark_photo_taken()

        future = asyncio.run_coroutine_threadsafe(
            generate_embedding(), async_loop
        )
        embed = future.result(timeout=10)

        if embed is None:
            return jsonify({"status": "error", "message": "No face detected"})

        return jsonify({"status": "success", "embed": "{" + ",".join(str(x) for x in embed.tolist()) + "}"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

    finally:
        scanner_state.stop_preview()


@webrtc_bp.route("/cancel/<mode>", methods=["POST"])
def cancel_connection(mode):
    if mode == "main":
        pcs = pcs_main
    else:
        pcs = pcs_preview
        scanner_state.stop_preview()

    for pc in list(pcs):
        asyncio.run_coroutine_threadsafe(pc.close(), async_loop)
        pcs.discard(pc)
    return jsonify({"status": "success"})

