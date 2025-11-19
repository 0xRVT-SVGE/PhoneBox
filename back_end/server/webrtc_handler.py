# back_end/server/webrtc_handler.py
import asyncio
import numpy as np
from back_end.embedding_gen import generate_embedding
from flask import Blueprint, jsonify, request
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame
from back_end.scanner_state import scanner_state

webrtc_bp = Blueprint("webrtc", __name__)
pcs_main = set()
pcs_preview = set()
async_loop = asyncio.new_event_loop()

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

        frame = scanner_state.get_frame()

        return make_video_frame(frame, pts, time_base)


# ===========================================================
# PREVIEW STREAM (RAW) — PURE EVENT-DRIVEN
# ===========================================================
class PreviewVideoTrack(VideoStreamTrack):
    kind = "video"

    async def recv(self):
        # Stop instantly if preview is not active anymore
        if not scanner_state.preview_requested.is_set():
            raise ConnectionError("Preview not active")

        # Stop instantly if the admin took a photo
        if scanner_state.photo_taken_event.is_set():
            raise ConnectionError("Preview finished")

        pts, time_base = await self.next_timestamp()
        frame = scanner_state.get_rframe()

        return make_video_frame(frame, pts, time_base)


# ===========================================================
# OFFER HANDLER
# ===========================================================
async def _handle_offer(offer_sdp, offer_type, mode):
    pc = RTCPeerConnection()

    if mode == "main":
        pcs_main.add(pc)
        video_track = MainVideoTrack()

    else:  # preview mode
        pcs_preview.add(pc)

        # Hard stop: don't accept preview if not requested
        if not scanner_state.preview_requested.is_set():
            raise RuntimeError("Preview not requested")

        video_track = PreviewVideoTrack()

    pc.addTrack(video_track)

    @pc.on("connectionstatechange")
    async def on_change():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            (pcs_main if mode == "main" else pcs_preview).discard(pc)
            await pc.close()

    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=offer_sdp, type=offer_type)
    )

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return {
        "status": "success",
        "data": {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        }
    }


@webrtc_bp.route("/offer/<mode>", methods=["POST"])
def offer(mode):
    if mode not in ("main", "preview"):
        return jsonify({"status": "error", "message": "Invalid mode"}), 400

    params = request.get_json()
    if not params or "sdp" not in params or "type" not in params:
        return jsonify({"status": "error", "message": "Invalid offer"}), 400

    try:
        future = asyncio.run_coroutine_threadsafe(
            _handle_offer(params["sdp"], params["type"], mode),
            async_loop
        )
        return jsonify(future.result(timeout=10))

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@webrtc_bp.route("/take_photo", methods=["POST"])
def take_photo():
    try:
        # Mark photo taken (triggers embedding_gen to capture frame)
        scanner_state.mark_photo_taken()

        # Run embedding generation in async loop
        future = asyncio.run_coroutine_threadsafe(generate_embedding(), async_loop)
        embed = future.result(timeout=10)  # adjust timeout as needed

        if embed is not None:
            return jsonify({"status": "success", "embedd": embed.tolist()})
        else:
            return jsonify({"status": "error", "message": "No face detected"})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

    finally:
        # Stop the preview automatically after taking the photo
        scanner_state.stop_preview()
