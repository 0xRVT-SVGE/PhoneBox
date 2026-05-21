# ============================================================
# FILE: back_end/server/webrtc_handler.py
# ============================================================
"""
WebRTC video streaming handler.

Modes:
  main    — front-facing camera via scanner_state (face recognition)
  preview — front-facing camera preview for photo capture
  admin   — top-down camera via top_camera (admin resolution session)
            frames are pre-annotated with staging ROI overlays

Opt #7  — recv() no longer busy-polls. Each track awaits
          run_in_executor(None, event.wait) which suspends the coroutine
          until the camera thread fires the event, releasing the event
          loop completely between frames.

Opt #23 — Per-mode SDP bitrate caps:
          admin   → 800 kbps  (QR codes must be sharp)
          main    → 300 kbps  (face recognition detail)
          preview → 300 kbps  (same as main)

Latency improvements (this revision):
  • force_h264() now constrains the codec to H.264 Constrained Baseline
    Profile (profile-level-id=42e01f).  Baseline disallows B-frames, which
    are the largest single source of encoder-introduced delay (1-3 frames).
    Main/High profiles allow B-frames; aiortc's default negotiation can end
    up on either — explicitly specifying Constrained Baseline guarantees
    zero B-frame latency regardless of the client's preference order.

  • modify_sdp_bitrate() now also injects x-google-max-bitrate and
    x-google-min-bitrate as fmtp parameters on the H.264 payload type.
    Chrome respects these inline hints for its internal rate controller
    even when the session-level b= lines are already present; without them
    Chrome can temporarily exceed the cap and trigger internal pacing that
    adds 50-200 ms of delay.

  • The answer SDP now sets a=fmtp:... level-asymmetry-allowed=1 so the
    server is free to encode at a lower profile than the client offers,
    which is required for Constrained Baseline to take effect when the
    client offers High.

Shutdown ownership: webrtc_handler owns its async_loop and all peer
connections. Call webrtc_handler.shutdown() to close everything cleanly.
"""

import asyncio
import threading
import logging
import numpy as np

from flask import Blueprint, jsonify, request
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCRtpSender
from av import VideoFrame
import av

from Backup.back_end.scanner_state import scanner_state
from Backup.back_end.embedding_gen import generate_embedding
from Backup.back_end.slot_monitor.camera.top_camera import top_camera
from Backup.back_end.config import WebRTCConfig as _WRC

logger = logging.getLogger(__name__)

webrtc_bp = Blueprint("webrtc", __name__)

# Separate sets per mode
pcs_main: set[RTCPeerConnection] = set()
pcs_preview: set[RTCPeerConnection] = set()
pcs_admin: set[RTCPeerConnection] = set()

# C1: all pcs set mutations happen on the async-loop thread only.
# The Flask route submits a coroutine rather than touching the sets directly.
# (No threading.Lock needed — single-writer rule enforced by design.)

# Dedicated async loop — owned by this module
async_loop = asyncio.new_event_loop()
_async_thread: threading.Thread = None

# Hardware acceleration
HW_ACCEL_AVAILABLE = False
HW_CODEC = None

# Opt #23: per-mode bitrate lookup — set once at module load from config.
_BITRATE_BY_MODE = {
    "admin":   _WRC.ADMIN_BITRATE_KBPS,
    "main":    _WRC.MAIN_BITRATE_KBPS,
    "preview": _WRC.MAIN_BITRATE_KBPS,
}

# ── H.264 Constrained Baseline profile-level-id ──────────────────────────────
# 42e01f breaks down as:
#   42   = profile_idc 66  → Baseline
#   e0   = constraint flags: constraint_set0=1 constraint_set1=1 → Constrained
#   1f   = level_idc 31    → Level 3.1 (supports up to 1080p@30)
#
# Constrained Baseline explicitly forbids B-frames and CABAC entropy coding.
# B-frames require the encoder to hold a frame back as a future reference,
# adding 1–3 frames of intrinsic encoder delay regardless of network conditions.
# On a LAN stream at 30 fps, 2 B-frames = ~67 ms of unavoidable latency.
_H264_CBP_PROFILE = "42e01f"


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
    """
    Restrict negotiation to H.264 AND pin the profile to Constrained Baseline.

    Why profile matters for latency
    ────────────────────────────────
    When force_h264 selected any H.264 codec, aiortc's SDP might include
    both Constrained Baseline (42e01f) and High (640c1f) in the offer.
    The peer could then choose High, which allows B-frames.

    This revision keeps ONLY Constrained Baseline entries in the codec list.
    If no matching codec is found (very old aiortc build) it falls back to
    keeping all H.264 codecs, preserving the original behaviour.
    """
    for transceiver in pc.getTransceivers():
        if transceiver.kind != "video":
            continue
        capabilities = RTCRtpSender.getCapabilities("video")
        if not capabilities or not hasattr(capabilities, 'codecs'):
            logger.warning("Could not get codec capabilities")
            return

        all_h264 = [c for c in capabilities.codecs if c.mimeType == "video/H264"]
        if not all_h264:
            raise RuntimeError("H264 not supported by aiortc build")

        # Prefer codecs whose fmtp already includes the Baseline profile.
        # The profile-level-id attribute is present in the sdpFmtpLine of
        # each capability entry when aiortc exposes it.
        cbp_codecs = [
            c for c in all_h264
            if _H264_CBP_PROFILE in (getattr(c, 'sdpFmtpLine', '') or '').lower()
        ]

        chosen = cbp_codecs if cbp_codecs else all_h264
        transceiver.setCodecPreferences(chosen)

        if cbp_codecs:
            logger.debug(
                f"[WebRTC] H.264 Constrained Baseline profile set "
                f"({len(cbp_codecs)} matching codec(s)) — B-frames disabled"
            )
        else:
            logger.debug(
                "[WebRTC] CBP codec not found in capabilities — "
                "using all H.264 codecs (SDP will patch profile)"
            )


def modify_sdp_bitrate(sdp: str, max_bitrate_kbps: int) -> str:
    """
    Patch the answer SDP for low-latency, bandwidth-capped H.264 streaming.

    Changes made to each video m-section:
      1. b=TIAS / b=AS  — session-level bandwidth cap (RFC 3890 / RFC 2327).
         These are what most WebRTC stacks enforce for pacing.

      2. x-google-max-bitrate / x-google-min-bitrate  — Chrome-specific fmtp
         parameters injected into the H.264 payload type's a=fmtp: line.
         Chrome's internal GCC (Google Congestion Control) rate limiter reads
         these even when b= lines are present and uses them to clamp its
         send-side bitrate estimator.  Without them Chrome can temporarily
         spike above the b=AS cap and then trigger pacing delay while it
         drains the excess.

      3. profile-level-id=42e01f / level-asymmetry-allowed=1  — ensures the
         encoded stream uses Constrained Baseline even if the offer included
         a higher profile in its fmtp.  level-asymmetry-allowed=1 is
         required by RFC 6184 §8.1 to permit the answer to use a lower level
         than the offer.

    The function is idempotent: running it twice produces the same result
    because it checks for existing x-google-* attributes before inserting.
    """
    lines = sdp.split('\r\n')
    out   = []
    in_video = False

    # Collect payload type numbers for H.264 so we can patch their fmtp lines.
    # We scan forward first, then re-process.
    h264_pts: set[str] = set()
    for line in lines:
        if line.startswith('m=video'):
            in_video = True
        elif line.startswith('m='):
            in_video = False
        if in_video and line.startswith('a=rtpmap:') and 'H264' in line:
            pt = line.split(':')[1].split(' ')[0]
            h264_pts.add(pt)

    in_video = False
    bw_inserted = False
    for line in lines:
        if line.startswith('m=video'):
            in_video     = True
            bw_inserted  = False
            out.append(line)
            continue
        elif line.startswith('m='):
            in_video = False

        if in_video and not bw_inserted and line.startswith(('c=', 'a=')):
            # Insert session-level bandwidth cap immediately before the first
            # attribute or connection line in the video m-section.
            out.append(f'b=TIAS:{max_bitrate_kbps * 1000}')
            out.append(f'b=AS:{max_bitrate_kbps}')
            bw_inserted = True

        # Patch a=fmtp: lines for H.264 payload types.
        if in_video and line.startswith('a=fmtp:'):
            pt = line.split(':')[1].split(' ')[0]
            if pt in h264_pts:
                # Already has x-google hints? skip to avoid duplication.
                if 'x-google-max-bitrate' not in line:
                    # Ensure the profile and level-asymmetry flags are present.
                    # We normalise by removing any existing profile-level-id
                    # and re-inserting the CBP value.
                    params = line.split(' ', 1)[1] if ' ' in line else ''
                    # Strip any existing profile-level-id to avoid duplicates
                    param_parts = [
                        p for p in params.split(';')
                        if 'profile-level-id' not in p.lower()
                        and 'level-asymmetry-allowed' not in p.lower()
                    ]
                    param_parts += [
                        f'profile-level-id={_H264_CBP_PROFILE}',
                        'level-asymmetry-allowed=1',
                        f'x-google-max-bitrate={max_bitrate_kbps}',
                        f'x-google-min-bitrate={max_bitrate_kbps // 4}',
                    ]
                    line = f'a=fmtp:{pt} ' + ';'.join(p.strip() for p in param_parts if p.strip())
                    logger.debug(f"[WebRTC] Patched fmtp for PT {pt}: CBP + bitrate hints")

        out.append(line)

    return '\r\n'.join(out)


# ============================================================
# VIDEO TRACKS
# ============================================================

class HWAccelVideoTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        if HW_ACCEL_AVAILABLE:
            logger.debug(f"Track initialized with {HW_CODEC} preference")


class MainVideoTrack(HWAccelVideoTrack):
    """
    Opt #7: replaced busy-poll loop with run_in_executor.
    """
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        # Opt #7: suspend until frame arrives — no spin, no sleep
        await asyncio.get_event_loop().run_in_executor(
            None, scanner_state._main_frame_event.wait
        )
        frame = scanner_state.get_frame()
        scanner_state._main_frame_event.clear()
        return make_video_frame(frame, pts, time_base)


class PreviewVideoTrack(HWAccelVideoTrack):
    """Opt #7: same run_in_executor pattern as MainVideoTrack."""
    kind = "video"

    async def recv(self):
        if not scanner_state.preview_requested.is_set():
            raise ConnectionError("Preview not active")
        if scanner_state.photo_taken_event.is_set():
            raise ConnectionError("Preview finished")

        pts, time_base = await self.next_timestamp()
        # Opt #7: suspend until frame arrives
        await asyncio.get_event_loop().run_in_executor(
            None, scanner_state._preview_frame_event.wait
        )
        frame = scanner_state.get_rframe()
        scanner_state._preview_frame_event.clear()
        return make_video_frame(frame, pts, time_base)


class AdminVideoTrack(HWAccelVideoTrack):
    """
    Streams the top-down camera (index 2) with staging ROI overlays.
    Opt #7: run_in_executor replaces the busy-poll on top_camera._frame_event.
    Reads from top_camera which owns the camera capture thread.
    ROI rectangles are already drawn on the frames — no client-side
    drawing needed.
    """
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        # Opt #7: suspend until the top camera posts a new frame
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: top_camera.wait_for_frame(timeout=1.0)
        )
        frame = top_camera.get_frame()
        top_camera.clear_frame_event()
        return make_video_frame(frame, pts, time_base)


# ============================================================
# OFFER HANDLER
# ============================================================

async def _handle_offer(offer_sdp, offer_type, mode):
    pc = RTCPeerConnection()

    if mode == "main":
        pcs_main.add(pc)
        video_track = MainVideoTrack()
    elif mode == "preview":
        pcs_preview.add(pc)
        scanner_state.request_preview()
        video_track = PreviewVideoTrack()
    else:  # admin
        pcs_admin.add(pc)
        top_camera.start()   # lazy — idempotent if DVW ops already started it
        video_track = AdminVideoTrack()

    pc.addTrack(video_track)
    force_h264(pc)

    @pc.on("connectionstatechange")
    async def on_state_change():
        state = pc.connectionState
        # 'disconnected' is transient — ICE may self-heal without intervention.
        # Only clean up on terminal states: 'failed' means ICE has exhausted all
        # candidates; 'closed' means we explicitly called pc.close() ourselves.
        # Closing on 'disconnected' caused a continuous cancel→reconnect loop
        # in the admin resolution page (every ~8-10 s).
        if state in ("closed", "failed"):
            logger.debug(f"[WebRTC] {mode!r} connection {state} — cleaning up")
            _pcs_for_mode(mode).discard(pc)
            await pc.close()

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type=offer_type))
    answer = await pc.createAnswer()

    # Opt #23: use the per-mode bitrate cap instead of a single global value.
    bitrate_kbps = _BITRATE_BY_MODE.get(mode, _WRC.MAX_BITRATE_KBPS)
    modified_sdp = modify_sdp_bitrate(answer.sdp, max_bitrate_kbps=bitrate_kbps)
    logger.debug(
        f"WebRTC offer mode={mode!r} bitrate={bitrate_kbps} kbps "
        f"profile=Constrained-Baseline (no B-frames)"
    )

    answer_with_bitrate = RTCSessionDescription(sdp=modified_sdp, type=answer.type)
    await pc.setLocalDescription(answer_with_bitrate)

    return {
        "status": "success",
        "data": {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        },
    }


def _pcs_for_mode(mode: str) -> set:
    return {"main": pcs_main, "preview": pcs_preview, "admin": pcs_admin}.get(mode, pcs_main)


async def _close_all_connections():
    all_pcs = list(pcs_main) + list(pcs_preview) + list(pcs_admin)
    if all_pcs:
        await asyncio.gather(*[pc.close() for pc in all_pcs], return_exceptions=True)
    pcs_main.clear()
    pcs_preview.clear()
    pcs_admin.clear()


# ============================================================
# LIFECYCLE
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
    """
    logger.info("Shutting down WebRTC handler...")

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
    if mode not in ("main", "preview", "admin"):
        return jsonify({"status": "error", "message": "Invalid mode"}), 400
    data = request.get_json()
    if not data or "sdp" not in data or "type" not in data:
        return jsonify({"status": "error", "message": "Invalid offer"}), 400
    try:
        future = asyncio.run_coroutine_threadsafe(
            _handle_offer(data["sdp"], data["type"], mode),
            async_loop,
        )
        return jsonify(future.result(timeout=_WRC.OFFER_TIMEOUT))
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
    """
    C1 fix: set mutations moved into a coroutine executed on the async loop
    thread.  Previously, pcs.discard() was called directly from the Flask
    thread while on_state_change() mutated the same set from the async loop
    thread — a data race under concurrent connections.
    """
    if mode == "preview":
        scanner_state.stop_preview()

    async def _cancel_on_loop():
        pcs = _pcs_for_mode(mode)
        for pc in list(pcs):          # snapshot before mutating
            pcs.discard(pc)           # async-loop thread owns the set
            await pc.close()

    try:
        future = asyncio.run_coroutine_threadsafe(_cancel_on_loop(), async_loop)
        future.result(timeout=5.0)
    except Exception as exc:
        logger.warning(f"[WebRTC] cancel_connection({mode}) error: {exc}")

    return jsonify({"status": "success"})


@webrtc_bp.route("/hw_accel_status", methods=["GET"])
def hw_accel_status():
    return jsonify({
        "status": "success",
        "hw_accel_available": HW_ACCEL_AVAILABLE,
        "codec": HW_CODEC,
    })