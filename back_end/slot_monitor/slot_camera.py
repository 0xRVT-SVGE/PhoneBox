# server/slot_monitor/slot_camera.py

import cv2

def generate_grid_rois(
    frame_width: int,
    frame_height: int,
    rows: int,
    cols: int,
    spacing: int
):
    rois = {}

    cell_h = frame_height // rows
    cell_w = frame_width // cols

    idx = 0
    for i in range(rows):
        for j in range(cols):
            x1 = j * cell_w + spacing // 2
            y1 = i * cell_h + spacing // 2
            x2 = (j + 1) * cell_w - spacing // 2
            y2 = (i + 1) * cell_h - spacing // 2

            rois[f"SLOT_{idx:02d}"] = (
                x1,
                y1,
                x2 - x1,
                y2 - y1
            )
            idx += 1

    return rois


class SlotCamera:
    def __init__(self, cam_index: int, rois: dict):
        self.cap = cv2.VideoCapture(cam_index, cv2.CAP_DSHOW)
        self.rois = rois

        if not self.cap.isOpened():
            raise RuntimeError("Slot camera failed to open")

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        return frame

    def extract_rois(self, frame):
        slices = {}
        for slot_id, (x, y, w, h) in self.rois.items():
            slices[slot_id] = frame[y:y+h, x:x+w]
        return slices

    def release(self):
        self.cap.release()
