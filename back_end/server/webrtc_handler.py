# ============================================================
# FILE: back_end/server/webrtc_handler.py
# ============================================================
"""
WebRTC video streaming handler.

Shutdown ownership: webrtc_handler owns its async_loop and all peer
connections. Call webrtc_handler.shutdown() to close everything cleanly.
server_main does not need to touch async_loop directly.
"""

import asyncio
import threading
import logging
import numpy as np

from flask import Blueprint, jsonify, request
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCRtpSender
from av import VideoFrame
import av

from back_end.scanner_state import scanner_state
from back_end.embedding_gen import generate_embedding

logger = logging.getLogger(__name__)

webrtc_bp = Blueprint("webrtc", __name__)

# Separate sets for main and preview connections
pcs_main: set[RTCPeerConnection] = set()
pcs_preview: set[RTCPeerConnection] = set()

# Dedicated async loop — owned by this module
async_loop = asyncio.new_event_loop()
_async_thread: threading.Thread = None

# Hardware acceleration
HW_ACCEL_AVAILABLE = False
HW_CODEC = None


# ============================================================
# HARDWARE ACCELERATION DETECTION
# ============================================================

def _detect_hw_acceleration():
    global HW_ACCEL_AVAILABLE, HW_CODEC
    hw_encoders = [
        ('h264_nvenc', 'NVIDIA'),
        ('h264_qsv', 'Intel QuickSync'),
        ('h264_vaapi', 'VAAPI'),
        ('h264_videotoolbox', 'VideoToolbox'),
        ('h264_amf', 'AMD'),
    ]
    for codec_name, hw_name in hw_encoders:
        try:
            av.codec.Codec(codec_name, 'w')
            HW_ACCEL_AVAILABLE = True
            HW_CODEC = codec_name
            logger.info(f"Hardware acceleration: {hw_name} ({codec_name})")
            return
        except Exception:
            continue
    HW_CODEC = 'libx264'
    logger.info("No hardware acceleration available, using CPU encoding")


_detect_hw_acceleration()


# ============================================================
# UTILITIES
# ============================================================

def make_video_frame(frame, pts, time_base):
    if frame is None:
        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        vf = VideoFrame.from_ndarray(arr, format="bgr24")
    else:
        vf = VideoFrame.from_ndarray(frame, format="bgr24")
    vf.pts = pts
    vf.time_base = time_base
    return vf


def force_h264(pc: RTCPeerConnection):
    for transceiver in pc.getTransceivers():
        if transceiver.kind != "video":
            continue
        capabilities = RTCRtpSender.getCapabilities("video")
        if not capabilities or not hasattr(capabilities, 'codecs'):
            logger.warning("Could not get codec capabilities")
            return
        h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
        if not h264_codecs:
            raise RuntimeError("H264 not supported by aiortc build")
        transceiver.setCodecPreferences(h264_codecs)


def modify_sdp_bitrate(sdp: str, max_bitrate_kbps: int) -> str:
    lines = sdp.split('\r\n')
    modified_lines = []
    for line in lines:
        modified_lines.append(line)
        if line.startswith('m=video'):
            modified_lines.append(f'b=TIAS:{max_bitrate_kbps * 1000}')
            modified_lines.append(f'b=AS:{max_bitrate_kbps}')
    return '\r\n'.join(modified_lines)


# ============================================================
# VIDEO TRACKS
# ============================================================

class HWAccelVideoTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        if HW_ACCEL_AVAILABLE:
            logger.debug(f"Track initialized with {HW_CODEC} preference")


class MainVideoTrack(HWAccelVideoTrack):
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        while not scanner_state._main_frame_event.wait(timeout=0.01):
            await asyncio.sleep(0.001)
        frame = scanner_state.get_frame()
        scanner_state._main_frame_event.clear()
        return make_video_frame(frame, pts, time_base)


class PreviewVideoTrack(HWAccelVideoTrack):
    kind = "video"

    async def recv(self):
        if not scanner_state.preview_requested.is_set():
            raise ConnectionError("Preview not active")
        if scanner_state.photo_taken_event.is_set():
            raise ConnectionError("Preview finished")

        pts, time_base = await self.next_timestamp()
        while not scanner_state._preview_frame_event.wait(timeout=0.01):
            await asyncio.sleep(0.001)
        frame = scanner_state.get_rframe()
        scanner_state._preview_frame_event.clear()
        return make_video_frame(frame, pts, time_base)


# ============================================================
# OFFER HANDLER
# ============================================================

async def _handle_offer(offer_sdp, offer_type, mode):
    pc = RTCPeerConnection()

    if mode == "main":
        pcs_main.add(pc)
        video_track = MainVideoTrack()
    else:
        pcs_preview.add(pc)
        scanner_state.request_preview()
        video_track = PreviewVideoTrack()

    pc.addTrack(video_track)
    force_h264(pc)

    @pc.on("connectionstatechange")
    async def on_state_change():
        state = pc.connectionState
        if state == "disconnected":
            await asyncio.sleep(5)
        if state in ("closed", "failed", "disconnected"):
            (pcs_main if mode == "main" else pcs_preview).discard(pc)
            await pc.close()

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
    answer = await pc.createAnswer()
    modified_sdp = modify_sdp_bitrate(answer.sdp, max_bitrate_kbps=100)
    answer_with_bitrate = RTCSessionDescription(sdp=modified_sdp, type=answer.type)
    await pc.setLocalDescription(answer_with_bitrate)

    return {
        "status": "success",
        "data": {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        },
    }


async def _close_all_connections():
    """Close all open peer connections — called during shutdown."""
    all_pcs = list(pcs_main) + list(pcs_preview)
    if all_pcs:
        await asyncio.gather(*[pc.close() for pc in all_pcs], return_exceptions=True)
    pcs_main.clear()
    pcs_preview.clear()


# ============================================================
# LIFECYCLE  (called by server_main)
# ============================================================

def start():
    """Start the async event loop in a background daemon thread."""
    global _async_thread

    def _run_loop():
        asyncio.set_event_loop(async_loop)
        async_loop.run_forever()

    _async_thread = threading.Thread(target=_run_loop, daemon=True, name="WebRTCLoop")
    _async_thread.start()
    logger.info("WebRTC async event loop started")


def shutdown():
    """
    Close all peer connections then stop the event loop.

    Owned entirely by this module — server_main calls this once
    and does not touch async_loop directly.
    """
    logger.info("Shutting down WebRTC handler...")

    # Close connections inside the loop, then stop it
    future = asyncio.run_coroutine_threadsafe(_close_all_connections(), async_loop)
    try:
        future.result(timeout=5.0)
    except Exception as e:
        logger.warning(f"WebRTC connection close error: {e}")

    async_loop.call_soon_threadsafe(async_loop.stop)

    if _async_thread:
        _async_thread.join(timeout=3.0)

    logger.info("WebRTC handler shutdown complete")


# ============================================================
# HTTP ENDPOINTS
# ============================================================

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


@webrtc_bp.route("/take_photo", methods=["POST"])
def take_photo():
    try:
        scanner_state.mark_photo_taken()
        future = asyncio.run_coroutine_threadsafe(generate_embedding(), async_loop)
        embed = future.result(timeout=10)
        if embed is None:
            return jsonify({"status": "error", "message": "No face detected"})
        return jsonify({
            "status": "success",
            "embed": "{" + ",".join(str(x) for x in embed.tolist()) + "}",
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    finally:
        scanner_state.stop_preview()


@webrtc_bp.route("/cancel/<mode>", methods=["POST"])
def cancel_connection(mode):
    pcs = pcs_main if mode == "main" else pcs_preview
    if mode == "preview":
        scanner_state.stop_preview()
    for pc in list(pcs):
        asyncio.run_coroutine_threadsafe(pc.close(), async_loop)
        pcs.discard(pc)
    return jsonify({"status": "success"})


@webrtc_bp.route("/hw_accel_status", methods=["GET"])
def hw_accel_status():
    return jsonify({
        "status": "success",
        "hw_accel_available": HW_ACCEL_AVAILABLE,
        "codec": HW_CODEC,
    })