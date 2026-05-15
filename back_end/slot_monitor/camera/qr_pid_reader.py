# ============================================================
# FILE: back_end/slot_monitor/camera/qr_pid_reader.py
# ============================================================
"""
QR Code scanning and PID validation.

Opt #4 — zxing-cpp backend with cv2 fallback
----------------------------------------------
zxing-cpp (pip install zxing-cpp) is a pure C++ multi-format decoder
with Python bindings.  It is ~3-5× faster than cv2.QRCodeDetector on
typical phone-sized QR stickers and handles low-contrast / partially
obscured codes more robustly.

At module load we try to import zxingcpp.  If it is unavailable the
module silently falls back to cv2.QRCodeDetector — behaviour is
identical, just slower.  No code-path changes required in callers.

Install:   pip install zxing-cpp
Verify:    python -c "import zxingcpp; print(zxingcpp.__version__)"

Changes from original:
  - read_pid_from_buffer() and scan_and_validate_pid_from_buffer() now
    accept an optional cancel_event: threading.Event.  The scan loop
    checks it on every iteration and returns None immediately when set,
    so op_ctx.clear() / cancel_operation unblocks the blocking call
    within one frame interval (~30 ms) instead of waiting up to 15 s.
"""

import logging
import re
import threading
import time
from typing import Optional, TYPE_CHECKING
import cv2
from back_end.config import CameraConfig as _CC
from back_end.slot_monitor.db_interface import SlotMonitorDB

if TYPE_CHECKING:
    from back_end.slot_monitor.camera.top_camera import TopCamera

logger = logging.getLogger(__name__)

# ── Opt #4: zxing-cpp with graceful fallback ──────────────────────────────────
try:
    import zxingcpp as _zxing
    _ZXING_AVAILABLE = True
    logger.info("[QRReader] zxing-cpp available — using fast C++ decoder")
except ImportError:
    _zxing = None
    _ZXING_AVAILABLE = False
    logger.info("[QRReader] zxing-cpp not installed — falling back to cv2.QRCodeDetector")
    logger.info("[QRReader] Install with: pip install zxing-cpp")

# cv2 fallback detector — created once, reused across all calls
_cv2_qr_detector = cv2.QRCodeDetector()

# ──────────────────────────────────────────────────────────────────────────────
# PID parsing (shared by both paths)
# ──────────────────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _parse_pid(raw: str) -> Optional[str]:
    data = raw.strip()
    if data.upper().startswith("PID:"):
        data = data[4:].strip()
    if _UUID_RE.match(data):
        return data.lower()
    return None


# ── QR decode dispatcher ─────────────────────────────────────────────────────

def _decode_qr(gray) -> Optional[str]:
    """
    Decode a QR code from a grayscale frame.

    Uses zxing-cpp when available (Opt #4), falls back to cv2.QRCodeDetector.
    Returns the raw decoded string, or None if nothing was found.
    """
    if _ZXING_AVAILABLE:
        # zxing-cpp: returns a list of Result objects; grab the first QR code
        try:
            results = _zxing.read_barcodes(gray)
            for r in results:
                if r.valid and r.text:
                    return r.text
        except Exception as e:
            logger.debug(f"[QRReader] zxing-cpp error, falling back to cv2: {e}")
            # Fall through to cv2

    # cv2 fallback
    data, _, _ = _cv2_qr_detector.detectAndDecode(gray)
    return data if data else None


# ── Path 1: direct camera open ────────────────────────────

def read_pid_from_camera(
    camera_index: int = 0,
    timeout_sec: float = 15.0,
) -> Optional[str]:
    cap = cv2.VideoCapture(camera_index, _CC.resolve_backend(_CC.TOP_CAM_BACKEND))
    if not cap.isOpened():
        logger.error(f"Camera {camera_index} not available")
        raise RuntimeError(f"Camera {camera_index} not available")

    logger.info(f"QR scan started via camera {camera_index} (timeout={timeout_sec}s)")
    deadline = time.time() + timeout_sec

    try:
        while time.time() < deadline:
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            data = _decode_qr(gray)

            if data:
                pid = _parse_pid(data)
                if pid:
                    logger.info(f"QR scan (direct): PID={pid}")
                    return pid
                logger.warning(f"QR scan: unrecognised format: {data!r}")

        logger.warning("QR scan (direct) timed out")
        return None
    finally:
        cap.release()


def scan_and_validate_pid(
    camera_index: int = 0,
    timeout_sec: float = 15.0,
) -> dict:
    try:
        pid = read_pid_from_camera(camera_index, timeout_sec=timeout_sec)
    except RuntimeError:
        return {"status": "error", "message": "camera_error"}
    except Exception as e:
        logger.error(f"QR scan error: {e}")
        return {"status": "error", "message": "scan_error"}

    if pid is None:
        return {"status": "error", "message": "qr_not_detected"}

    if not SlotMonitorDB.pid_exists(pid):
        logger.warning(f"PID {pid} not found in database")
        return {"status": "error", "message": "pid_not_found", "pid": pid}

    logger.info(f"PID {pid} validated (direct camera)")
    return {"status": "success", "pid": pid}


# ── Path 2: shared frame buffer ───────────────────────────

def read_pid_from_buffer(
    frame_buffer:  "TopCamera",
    timeout_sec:   float = 15.0,
    cancel_event:  Optional[threading.Event] = None,
) -> Optional[str]:
    """
    Read frames from a TopCamera buffer and decode QR codes.

    Args:
        frame_buffer:  Running TopCamera instance.
        timeout_sec:   Max seconds before giving up.
        cancel_event:  Optional threading.Event; if set the loop exits
                       immediately returning None so the caller can
                       detect cancellation within one frame interval.
    Returns:
        pid as str if found, None on timeout, cancellation, or bad format.
    """
    logger.info(f"QR scan started via frame buffer (timeout={timeout_sec}s)")
    deadline = time.time() + timeout_sec

    get_frame_fn = (frame_buffer.get_raw_frame
                    if hasattr(frame_buffer, "get_raw_frame")
                    else frame_buffer.get_frame)

    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            logger.info("QR scan (buffer): cancelled")
            return None

        remaining = deadline - time.time()
        if remaining <= 0:
            break

        got_frame = frame_buffer.wait_for_frame(timeout=min(0.05, remaining))
        if not got_frame:
            continue

        frame = get_frame_fn()
        frame_buffer.clear_frame_event()

        if frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        data = _decode_qr(gray)

        if data:
            pid = _parse_pid(data)
            if pid:
                logger.info(f"QR scan (buffer): PID={pid}")
                return pid
            logger.warning(f"QR scan (buffer): unrecognised format: {data!r}")

    if cancel_event is None or not cancel_event.is_set():
        logger.warning("QR scan (buffer) timed out")
    return None


def scan_and_validate_pid_from_buffer(
    frame_buffer:  "TopCamera",
    timeout_sec:   float = 15.0,
    cancel_event:  Optional[threading.Event] = None,
) -> dict:
    """
    Scan from a shared frame buffer, then validate PID against DB.

    Args:
        cancel_event: If set, scan exits immediately and returns
                      {"status": "error", "message": "cancelled"}.
    """
    try:
        pid = read_pid_from_buffer(
            frame_buffer,
            timeout_sec=timeout_sec,
            cancel_event=cancel_event,
        )
    except Exception as e:
        logger.error(f"QR buffer scan error: {e}")
        return {"status": "error", "message": "scan_error"}

    if pid is None:
        if cancel_event is not None and cancel_event.is_set():
            return {"status": "error", "message": "cancelled"}
        return {"status": "error", "message": "qr_not_detected"}

    if not SlotMonitorDB.pid_exists(pid):
        logger.warning(f"PID {pid} not found in database")
        return {"status": "error", "message": "pid_not_found", "pid": pid}

    logger.info(f"PID {pid} validated (buffer)")
    return {"status": "success", "pid": pid}