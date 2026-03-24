# ============================================================
# FILE: back_end/slot_monitor/camera/qr_pid_reader.py
# ============================================================
"""
QR Code scanning and PID validation.

Two scan paths:

  scan_and_validate_pid(camera_index, timeout_sec)
      Original path — opens camera directly via cv2.VideoCapture.
      Used for the bottom camera (slot monitoring) and any context
      where no shared frame buffer is available.

  scan_and_validate_pid_from_buffer(frame_buffer, timeout_sec)
      Buffer path — reads frames from a TopCamera buffer instance.
      Used by admin_ops_handler for camera 2 (top-down) so the
      WebRTC admin stream never has to pause or stall during a QR scan.
      pyzbar decoding happens on the same frames already being streamed.

PID formats accepted:
  "123"      raw integer string
  "PID:123"  prefixed
"""

import logging
import re
import time
from typing import Optional, TYPE_CHECKING

import cv2
from pyzbar.pyzbar import decode

from back_end.slot_monitor.db_interface import SlotMonitorDB

if TYPE_CHECKING:
    from back_end.slot_monitor.camera.top_camera import TopCamera

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# PID parsing (shared by both paths)
# ──────────────────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _parse_pid(raw: str) -> Optional[str]:
    """
    Extract a normalised PID string from raw QR data.

    Accepted formats:
        "9f74aca1-556e-4212-aafd-5f1f48db319a"   bare UUID  (phones.pid)
        "PID:9f74aca1-556e-4212-aafd-5f1f48db319a"  prefixed UUID
        "PID:12345"   legacy integer format
        "12345"       legacy bare integer

    Returns None if the format is unrecognised.
    """
    data = raw.strip()
    if data.upper().startswith("PID:"):
        data = data[4:].strip()

    # UUID format — phones.pid primary key
    if _UUID_RE.match(data):
        return data.lower()
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Path 1 — direct camera open (bottom camera / standalone use)
# ──────────────────────────────────────────────────────────────────────────────

def read_pid_from_camera(
    camera_index: int = 0,
    timeout_sec: float = 15.0,
) -> Optional[str]:
    """
    Scan camera feed for a QR code containing a PID.
    Opens and releases the camera internally.

    Returns:
        pid as str if detected and parseable, None on timeout or bad format.
    """
    cap = cv2.VideoCapture(camera_index)
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

            for qr in decode(frame):
                pid = _parse_pid(qr.data.decode("utf-8"))
                if pid:
                    logger.info(f"QR scan (direct): PID={pid}")
                    return pid
                else:
                    logger.warning(f"QR scan: unrecognised format: {qr.data!r}")

        logger.warning("QR scan (direct) timed out")
        return None

    finally:
        cap.release()


def scan_and_validate_pid(
    camera_index: int = 0,
    timeout_sec: float = 15.0,
) -> dict:
    """
    Scan camera directly, then validate PID against DB.

    Returns:
        {"status": "success", "pid": str}
        {"status": "error",   "message": str, "pid": str | None}
    """
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


# ──────────────────────────────────────────────────────────────────────────────
# Path 2 — shared frame buffer (top camera / admin session)
# ──────────────────────────────────────────────────────────────────────────────

def read_pid_from_buffer(
    frame_buffer: "TopCamera",
    timeout_sec: float = 15.0,
) -> Optional[str]:
    """
    Read frames from a TopCamera buffer and decode QR codes.

    The buffer's capture thread already owns the camera — this function
    never opens cv2.VideoCapture itself, so the WebRTC admin stream
    continues uninterrupted during the scan.

    Args:
        frame_buffer: A running TopCamera instance.
        timeout_sec:  Max seconds before giving up.

    Returns:
        pid as str if found, None on timeout or bad format.
    """
    logger.info(f"QR scan started via frame buffer (timeout={timeout_sec}s)")

    deadline = time.time() + timeout_sec

    while time.time() < deadline:
        remaining = deadline - time.time()
        if remaining <= 0:
            break

        # Wait up to 100ms for the next frame, then check timeout and retry
        got_frame = frame_buffer.wait_for_frame(timeout=min(0.1, remaining))
        if not got_frame:
            continue

        frame = frame_buffer.get_frame()
        frame_buffer.clear_frame_event()

        if frame is None:
            continue

        # pyzbar needs a writeable frame — buffer frames are read-only
        frame_copy = frame.copy()

        for qr in decode(frame_copy):
            pid = _parse_pid(qr.data.decode("utf-8"))
            if pid:
                logger.info(f"QR scan (buffer): PID={pid}")
                return pid
            else:
                logger.warning(f"QR scan (buffer): unrecognised format: {qr.data!r}")

    logger.warning("QR scan (buffer) timed out")
    return None


def scan_and_validate_pid_from_buffer(
    frame_buffer: "TopCamera",
    timeout_sec: float = 15.0,
) -> dict:
    """
    Scan from a shared frame buffer, then validate PID against DB.
    Use this in admin_ops_handler instead of scan_and_validate_pid.

    Returns:
        {"status": "success", "pid": str}
        {"status": "error",   "message": str, "pid": str | None}
    """
    try:
        pid = read_pid_from_buffer(frame_buffer, timeout_sec=timeout_sec)
    except Exception as e:
        logger.error(f"QR buffer scan error: {e}")
        return {"status": "error", "message": "scan_error"}

    if pid is None:
        return {"status": "error", "message": "qr_not_detected"}

    if not SlotMonitorDB.pid_exists(pid):
        logger.warning(f"PID {pid} not found in database")
        return {"status": "error", "message": "pid_not_found", "pid": pid}

    logger.info(f"PID {pid} validated (buffer)")
    return {"status": "success", "pid": pid}