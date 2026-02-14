# ============================================================
# FILE: server/slot_monitor/qr_scanner.py
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
    timeout_sec: float = 10.0
) -> Optional[int]:
    """
    Scan camera feed for a QR code containing a PID.

    Accepts formats:
    - "123" (raw number)
    - "PID:123" (prefixed)

    Args:
        camera_index: OpenCV camera index (0 = default)
        timeout_sec: Max seconds to scan before giving up

    Returns:
        pid (int) if detected and valid
        None if timeout or invalid QR format
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

            # Decode all QR codes in frame
            decoded = decode(frame)
            if decoded:
                # Take first QR code found
                data = decoded[0].data.decode("utf-8").strip()
                logger.debug(f"QR detected: {data}")

                # Parse PID format
                if data.startswith("PID:"):
                    data = data[4:]

                if data.isdigit():
                    pid = int(data)
                    logger.info(f"✅ PID scanned: {pid}")
                    return pid
                else:
                    logger.warning(f"Invalid QR format: {data}")

            # Timeout check
            elapsed = (cv2.getTickCount() - start) / freq
            if elapsed > timeout_sec:
                logger.warning("QR scan timed out")
                return None

    finally:
        cap.release()
        logger.debug("Camera released")


def scan_and_validate_pid(camera_index: int = 0) -> dict:
    """
    Full QR scanning workflow:
    1. Scan camera feed for QR code
    2. Extract PID
    3. Validate PID exists in database

    Args:
        camera_index: OpenCV camera index

    Returns:
        Success: {"status": "success", "pid": int}
        Failure: {"status": "error", "message": str, "pid": int | None}
    """
    try:
        # Step 1: Read QR code
        pid = read_pid_from_camera(camera_index)

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