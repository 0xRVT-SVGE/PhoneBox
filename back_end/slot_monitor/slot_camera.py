# ============================================================
# FILE: server/slot_monitor/slot_camera.py
# ============================================================

import cv2
import logging

logger = logging.getLogger(__name__)


def generate_grid_rois(
    frame_width: int,
    frame_height: int,
    rows: int,
    cols: int,
    spacing: int
):
    """Generate ROI coordinates for grid layout"""
    rois = {}
    cell_h = frame_height // rows
    cell_w = frame_width // cols

    for i in range(rows):
        for j in range(cols):
            x1 = j * cell_w + spacing // 2
            y1 = i * cell_h + spacing // 2
            x2 = (j + 1) * cell_w - spacing // 2
            y2 = (i + 1) * cell_h - spacing // 2

            # lid = slot index (0-based)
            lid = i * cols + j
            rois[lid] = (x1, y1, x2 - x1, y2 - y1)  # (x, y, w, h)

    return rois


class SlotCamera:
    def __init__(self, cam_index: int, rois: dict):
        self.cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
        self.rois = rois
        self.frame_count = 0

        if not self.cap.isOpened():
            raise RuntimeError(f"Slot camera {cam_index} failed to open")

        # Get camera info
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info(f"Camera {cam_index} opened: {self.width}x{self.height}")

    def read(self):
        """Read a frame from camera"""
        ok, frame = self.cap.read()
        if not ok:
            logger.error("Failed to read frame from camera")
            return None
        self.frame_count += 1
        return frame

    def extract_rois(self, frame):
        """Extract ROI regions from frame"""
        slices = {}
        for lid, (x, y, w, h) in self.rois.items():
            # Validate coordinates
            if x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
                logger.warning(f"ROI {lid} out of bounds, skipping")
                continue
            slices[lid] = frame[y:y+h, x:x+w].copy()
        return slices

    def release(self):
        """Release camera resources"""
        if self.cap.isOpened():
            self.cap.release()
        logger.info("Camera released")