# ============================================================
# FILE: server/slot_monitor/qr_pid_reader.py
# ============================================================
"""
QR Code scanning and PID validation.

Handles:
- Camera feed reading
- QR code detection (using pyzbar)
- PID format parsing
- Database existence validation
"""

import cv2
import logging
from pyzbar.pyzbar import decode
from typing import Optional
from back_end.slot_monitor.db_interface import SlotMonitorDB

logger = logging.getLogger(__name__)


def read_pid_from_camera(
    camera_index: int = 0,
    timeout_sec: float = 15.0,
) -> Optional[str]:
    """
    Scan camera feed for a QR code containing a PID.

    Accepts formats:
    - "123" (raw number)
    - "PID:123" (prefixed)

    Args:
        camera_index: OpenCV camera index (0 = default)
        timeout_sec:  Max seconds to scan before giving up

    Returns:
        pid as str if detected and parseable, None on timeout or invalid format
    """
    cap = cv2.VideoCapture(camera_index)
    if not cap.isOpened():
        logger.error(f"Camera {camera_index} not available")
        raise RuntimeError(f"Camera {camera_index} not available")

    logger.info(f"Starting QR scan (timeout={timeout_sec}s)...")

    start = cv2.getTickCount()
    freq = cv2.getTickFrequency()

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                continue

            decoded = decode(frame)
            if decoded:
                data = decoded[0].data.decode("utf-8").strip()
                logger.debug(f"QR detected: {data}")

                if data.startswith("PID:"):
                    data = data[4:]

                if data.isdigit():
                    pid = str(int(data))   # normalise (strips leading zeros)
                    logger.info(f"PID scanned: {pid}")
                    return pid
                else:
                    logger.warning(f"Invalid QR format: {data}")

            elapsed = (cv2.getTickCount() - start) / freq
            if elapsed > timeout_sec:
                logger.warning("QR scan timed out")
                return None

    finally:
        cap.release()
        logger.debug("Camera released")


def scan_and_validate_pid(camera_index: int = 0, timeout_sec: float = 15.0) -> dict:
    """
    Full QR scanning workflow:
    1. Scan camera feed for QR code
    2. Extract PID as str
    3. Validate PID exists in database

    Args:
        camera_index: OpenCV camera index
        timeout_sec:  Max seconds to scan before giving up

    Returns:
        Success: {"status": "success", "pid": str}
        Failure: {"status": "error", "message": str, "pid": str | None}
    """
    try:
        pid = read_pid_from_camera(camera_index, timeout_sec=timeout_sec)

        if pid is None:
            return {
                "status": "error",
                "message": "qr_not_detected"
            }

        # Step 2: Validate against database
        if not SlotMonitorDB.pid_exists(pid):
            logger.warning(f"PID {pid} not found in database")
            return {
                "status": "error",
                "message": "pid_not_found",
                "pid": pid
            }

        logger.info(f"PID {pid} validated successfully")
        return {
            "status": "success",
            "pid": pid
        }

    except RuntimeError as e:
        logger.error(f"Camera error: {e}")
        return {
            "status": "error",
            "message": "camera_error"
        }
    except Exception as e:
        logger.error(f"QR scan error: {e}")
        return {
            "status": "error",
            "message": "scan_error"
        }