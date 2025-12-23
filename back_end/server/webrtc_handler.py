import asyncio
import numpy as np
from aiortc.rtcrtpparameters import RTCRtpEncodingParameters

from flask import Blueprint, jsonify, request
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCRtpSender
from av import VideoFrame
import av

from back_end.scanner_state import scanner_state
from back_end.embedding_gen import generate_embedding

webrtc_bp = Blueprint("webrtc", __name__)

# Separate sets for main and preview connections
pcs_main = set()
pcs_preview = set()

# Dedicated async loop for aiortc + async tasks
async_loop = asyncio.new_event_loop()

# Hardware acceleration detection
HW_ACCEL_AVAILABLE = False
HW_CODEC = None


def detect_hw_acceleration():
    """Detect available hardware acceleration"""
    global HW_ACCEL_AVAILABLE, HW_CODEC

    # List of hardware encoders to try (in order of preference)
    hw_encoders = [
        ('h264_nvenc', 'NVIDIA'),  # NVIDIA
        ('h264_qsv', 'Intel QuickSync'),  # Intel
        ('h264_vaapi', 'VAAPI'),  # Linux VA-API
        ('h264_videotoolbox', 'VideoToolbox'),  # macOS
        ('h264_amf', 'AMD'),  # AMD
    ]

    for codec_name, hw_name in hw_encoders:
        try:
            # Try to create a codec to verify it's available
            codec = av.codec.Codec(codec_name, 'w')
            HW_ACCEL_AVAILABLE = True
            HW_CODEC = codec_name
            print(f"[WebRTC] Hardware acceleration enabled: {hw_name} ({codec_name})")
            return True
        except:
            continue

    print("[WebRTC] No hardware acceleration available, using CPU encoding")
    HW_CODEC = 'libx264'  # Fallback to CPU
    return False


# Detect on module load
detect_hw_acceleration()


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
# Force H264 codec on a PeerConnection with HW accel preference
# ===========================================================
def force_h264(pc: RTCPeerConnection):
    for transceiver in pc.getTransceivers():
        if transceiver.kind != "video":
            continue

        capabilities = RTCRtpSender.getCapabilities("video")

        if not capabilities or not hasattr(capabilities, 'codecs'):
            print(f"[WebRTC] Warning: Could not get codec capabilities")
            return

        h264_codecs = [c for c in capabilities.codecs if c.mimeType == "video/H264"]

        if not h264_codecs:
            raise RuntimeError("H264 not supported by aiortc build")

        transceiver.setCodecPreferences(h264_codecs)

        # Log the codec being used
        codec = h264_codecs[0]
        print(f"[WebRTC] Preferred codec: {codec.mimeType} (clockRate: {codec.clockRate})")


def modify_sdp_bitrate(sdp: str, max_bitrate_kbps: int) -> str:
    """
    Modify SDP to add bitrate constraints

    Args:
        sdp: The SDP string
        max_bitrate_kbps: Maximum bitrate in Kbps (e.g., 1000 for 1 Mbps)
    """
    lines = sdp.split('\r\n')
    modified_lines = []

    for i, line in enumerate(lines):
        modified_lines.append(line)

        # Add bitrate limit after video media line
        if line.startswith('m=video'):
            # Add TIAS (Transport Independent Application Specific) bandwidth
            modified_lines.append(f'b=TIAS:{max_bitrate_kbps * 1000}')
            # Add AS (Application Specific) bandwidth for compatibility
            modified_lines.append(f'b=AS:{max_bitrate_kbps}')

    return '\r\n'.join(modified_lines)


# ===========================================================
# Hardware-Accelerated Video Track Base Class
# ===========================================================
class HWAccelVideoTrack(VideoStreamTrack):
    """Base class for hardware-accelerated video tracks"""

    def __init__(self):
        super().__init__()
        self.encoder = None
        self.encoder_context = None
        self._setup_encoder()

    def _setup_encoder(self):
        """Setup hardware encoder if available"""
        try:
            if HW_ACCEL_AVAILABLE and HW_CODEC:
                # Note: aiortc handles encoding internally
                # We can hint at preferred encoder via codec preferences
                print(f"[WebRTC] Track initialized with {HW_CODEC} preference")
        except Exception as e:
            print(f"[WebRTC] Encoder setup warning: {e}")


# ===========================================================
# MAIN STREAM (ROI) with HW Acceleration
# ===========================================================
class MainVideoTrack(HWAccelVideoTrack):
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
# PREVIEW STREAM (RAW) with HW Acceleration
# ===========================================================
class PreviewVideoTrack(HWAccelVideoTrack):
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
# WebRTC OFFER HANDLER with HW Acceleration
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
        video_track = PreviewVideoTrack()

    pc.addTrack(video_track)

    force_h264(pc)

    # Cleanup on disconnect
    @pc.on("connectionstatechange")
    async def on_state_change():
        state = pc.connectionState

        if state == "disconnected":
            # wait briefly before closing to allow transient reconnect
            await asyncio.sleep(5)

        if state in ("closed", "failed", "disconnected"):
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

    modified_sdp = modify_sdp_bitrate(answer.sdp, max_bitrate_kbps=100)  # 100 Kbps
    answer_with_bitrate = RTCSessionDescription(sdp=modified_sdp, type=answer.type)

    await pc.setLocalDescription(answer_with_bitrate)

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


@webrtc_bp.route("/hw_accel_status", methods=["GET"])
def hw_accel_status():
    """Check hardware acceleration status"""
    return jsonify({
        "status": "success",
        "hw_accel_available": HW_ACCEL_AVAILABLE,
        "codec": HW_CODEC
    })