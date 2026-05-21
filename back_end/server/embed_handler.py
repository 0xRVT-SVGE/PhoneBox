# ============================================================
# FILE: back_end/server/embed_handler.py
# ============================================================
"""
HTTP-only face embedding capture endpoint.

Replaces the WebRTC preview + take_photo flow for admin face enrolment.

Two routes registered under /api/embed/:
  GET  /preview  — JPEG snapshot of the current front-camera frame.
                   No face detection — cheap, suitable for polling every ~333ms
                   to show a live-ish preview on the admin tablet.
                   Returns: image/jpeg  (binary, not JSON)

  POST /capture  — Run face detection + embedding on the current frame.
                   Returns: JSON { status, embed, preview_jpeg }
                   preview_jpeg: base64-encoded JPEG of the frame used,
                   so the client can show a freeze-frame confirmation.

Design rationale
────────────────
The old flow: WebRTC negotiate (1-3s) → PreviewVideoTrack (live) → take_photo.
The new flow: poll GET /preview (zero setup) → tap Capture → POST /capture.

Latency comparison on LAN:
  WebRTC approach  : ~1.5-3 s to first video frame (ICE + DTLS handshake)
  HTTP approach    : ~80-150 ms to first JPEG (existing keep-alive connection)

The front camera is always running (scanner_loop feeds scanner_state every frame).
We simply read the latest raw frame — it is already in RAM, protected by a lock.
"""

import base64
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

import cv2
import numpy as np
from flask import Blueprint, jsonify, Response

from Backup.back_end.scanner_state import scanner_state
from Backup.back_end.scanner_worker import _deepface_represent, l2_normalize
from Backup.back_end.config import ScannerConfig as _SC

logger = logging.getLogger(__name__)

embed_bp = Blueprint("embed", __name__)

# ── Shared thread pool — max 1 worker so concurrent capture taps are serialised.
# Face detection is CPU-heavy; a queue of 1 ensures back-pressure without
# spawning unbounded threads.
_embed_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="EmbedCapture")

# ── JPEG encode quality (0-100). 75 is the standard default; the preview
# stream doesn't need more than that and it halves the payload vs. quality=95.
_JPEG_QUALITY = 75

# ── Preview resize: downscale for fast polling.  Full 1280×720 at 75 quality
# is ~60 KB per frame; 640×360 is ~20 KB — 3× less bandwidth for the same
# visual clarity on a 4-5" preview widget.
_PREVIEW_WIDTH  = 640
_PREVIEW_HEIGHT = 360

# Cached scale for embedding — same pattern as scanner_worker._resize_scale.
_embed_scale: float | None = None
_embed_scale_lock = threading.Lock()


def _get_current_frame() -> np.ndarray | None:
    """Return a copy of the latest raw front-camera frame, or None."""
    frame = scanner_state.get_rframe()
    if frame is None or frame.size == 0:
        return None
    return frame.copy()   # copy outside the lock so we hold it as briefly as possible


def _frame_to_jpeg(frame: np.ndarray, width: int | None = None) -> bytes | None:
    """
    Encode a BGR numpy frame to JPEG bytes.

    Args:
        frame: BGR numpy array.
        width: If given, resize to this width (maintaining aspect ratio).

    Returns:
        JPEG bytes, or None on encode failure.
    """
    if width is not None:
        scale  = width / frame.shape[1]
        height = int(frame.shape[0] * scale)
        frame  = cv2.resize(frame, (width, height), interpolation=cv2.INTER_LINEAR)

    ok, buf = cv2.imencode(
        ".jpg", frame,
        [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY],
    )
    return bytes(buf) if ok else None


def _run_embedding(frame: np.ndarray) -> np.ndarray | None:
    """
    Run face detection + L2-normalised SFace embedding on a BGR frame.

    Returns a (128,) float32 array, or None if no face is detected.
    This blocks — call it only from the executor thread.
    """
    global _embed_scale
    with _embed_scale_lock:
        if _embed_scale is None:
            _embed_scale = _SC.SCALED_WIDTH / frame.shape[1]
        scale = _embed_scale

    resized = cv2.resize(
        frame,
        (_SC.SCALED_WIDTH, int(frame.shape[0] * scale)),
        interpolation=cv2.INTER_LINEAR,
    )

    results = _deepface_represent(resized)
    if not results:
        return None

    largest = max(results, key=lambda r: r["facial_area"]["w"] * r["facial_area"]["h"])
    return l2_normalize(np.array(largest["embedding"], dtype=np.float32))


# ── Routes ────────────────────────────────────────────────────────────────────

@embed_bp.route("/preview", methods=["GET"])
def preview():
    """
    GET /api/embed/preview

    Returns the latest front-camera frame as a JPEG image.
    Clients should poll this at ~3 fps to show a live-ish preview.
    No face detection — cheap (just a resize + JPEG encode).

    Response: 200 image/jpeg  or  503 if no frame available yet.
    """
    frame = _get_current_frame()
    if frame is None:
        return Response("No camera frame available", status=503)

    jpeg = _frame_to_jpeg(frame, width=_PREVIEW_WIDTH)
    if jpeg is None:
        return Response("JPEG encode failed", status=500)

    return Response(
        jpeg,
        status=200,
        mimetype="image/jpeg",
        headers={
            # Prevent any caching — every poll must get a fresh frame.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
        },
    )


@embed_bp.route("/capture", methods=["POST"])
def capture():
    """
    POST /api/embed/capture

    Grab the current front-camera frame, run face detection and embedding,
    and return the result.

    Response JSON:
      {
        "status":        "success" | "error",
        "embed":         [float, ...],         # 128-dim L2-normalised vector
        "preview_jpeg":  "<base64>",           # freeze-frame for UI confirmation
        "message":       "..."                 # only on error
      }

    Errors:
      503 — no camera frame available (scanner not running)
      422 — no face detected in frame
      408 — embedding timed out (CPU overloaded)
      500 — unexpected error
    """
    frame = _get_current_frame()
    if frame is None:
        return jsonify({"status": "error", "message": "No camera frame — is the scanner running?"}), 503

    # Encode preview JPEG immediately (before the expensive face detection).
    # This way the client always gets a freeze-frame even if embedding fails.
    jpeg      = _frame_to_jpeg(frame, width=_PREVIEW_WIDTH)
    jpeg_b64  = base64.b64encode(jpeg).decode("ascii") if jpeg else None

    try:
        future = _embed_executor.submit(_run_embedding, frame)
        embed  = future.result(timeout=10.0)   # face detection budget
    except FuturesTimeoutError:
        logger.warning("[EmbedHandler] Embedding timed out")
        return jsonify({
            "status":       "error",
            "message":      "Embedding timed out — try again",
            "preview_jpeg": jpeg_b64,
        }), 408
    except Exception as exc:
        logger.exception(f"[EmbedHandler] Unexpected error: {exc}")
        return jsonify({
            "status":       "error",
            "message":      str(exc),
            "preview_jpeg": jpeg_b64,
        }), 500

    if embed is None:
        return jsonify({
            "status":       "error",
            "message":      "No face detected — centre the student's face and try again",
            "preview_jpeg": jpeg_b64,
        }), 422

    return jsonify({
        "status":       "success",
        "embed":        embed.tolist(),   # list[float] — JSON-serialisable
        "preview_jpeg": jpeg_b64,
    })
