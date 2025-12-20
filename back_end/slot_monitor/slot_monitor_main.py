# server/slot_monitor/slot_monitor_main.py

import time
import threading

from slot_camera import SlotCamera
from slot_embed import compute_embedding, embedding_distance
from slot_state import SlotState

CHECK_EVERY_N = 3
T_MINOR = 0.15
T_MAJOR = 0.35


class SlotMonitor(threading.Thread):
    def __init__(self, cam_index, rois, baseline_map):
        super().__init__(daemon=True)

        self.camera = SlotCamera(cam_index, rois)
        self.slots = {
            slot_id: SlotState(slot_id, baseline_map[slot_id])
            for slot_id in rois.keys()
        }

        self.running = True
        self.frame_id = 0

    def run(self):
        while self.running:
            frame = self.camera.read()
            if frame is None:
                time.sleep(0.05)
                continue

            self.frame_id += 1
            if self.frame_id % CHECK_EVERY_N != 0:
                continue

            rois = self.camera.extract_rois(frame)

            for slot_id, roi in rois.items():
                emb = compute_embedding(roi)
                slot = self.slots[slot_id]
                dist = embedding_distance(emb, slot.baseline)

                slot.update(dist, T_MINOR, T_MAJOR)

                # TODO: emit events / update DB on state change

            time.sleep(0.001)

    def stop(self):
        self.running = False
        self.camera.release()

# server_main.py
rois = load_rois("slot_rois.json")

baseline_map = load_baselines_from_db()

slot_monitor = SlotMonitor(
    cam_index=1,
    rois=rois,
    baseline_map=baseline_map
)
slot_monitor.start()

