import cv2
from deepface import DeepFace
from pyzbar.pyzbar import decode
import numpy as np
import json
import time


def l2_normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


# =====================
# Load known embeddings & barcodes
# =====================
with open("database.json", "r") as f:
    database = json.load(f)

names = list(database.keys())
embeddings = np.array([
    l2_normalize(np.array(database[name]["embedding"], dtype=np.float32))
    for name in names
])
barcodes = {name: database[name]["barcode"] for name in names}

print(f"[+] Loaded {len(names)} known identities from database.json")

# =====================
# Open webcam
# =====================
cap = cv2.VideoCapture(0)

ret, frame = cap.read()
if not ret:
    raise RuntimeError("Failed to read from webcam")

h, w = frame.shape[:2]
roi_w, roi_h = w // 2, h // 2
x1, x2 = 0, roi_w
y1, y2 = h - roi_h, h
roi_coords = (x1, y1, x2, y2)  # static ROI coordinates

frame_count = 0
process_every = 5      # face recognition every 5 frames
barcode_every = 2      # barcode scan every 2 frames
last_write = None

current_name = "Scanning..."
current_color = (255, 255, 0)

face_verified = False
barcode_verified = False

face_lock_until = 0
barcode_lock_until = 0
valid_time = 7  # seconds validity

# Debug flag (draw face and barcode rectangles)
DEBUG_DRAW = False

while True:
    ret, frame = cap.read()
    if not ret:
        break

    frame_count += 1
    now = time.time()

    # ================= FACE RECOGNITION =================
    if frame_count % process_every == 0:
        scale_width = 480
        small_frame = cv2.resize(
            frame,
            (scale_width, int(frame.shape[0] * scale_width / frame.shape[1]))
        )

        try:
            results = DeepFace.represent(
                img_path=small_frame,
                model_name="SFace",
                detector_backend="opencv",
                enforce_detection=False
            )

            if results:
                largest_face = max(results, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"])
                embedding = l2_normalize(np.array(largest_face["embedding"], dtype=np.float32))

                similarities = np.dot(embeddings, embedding)
                best_idx = np.argmax(similarities)
                best_score = similarities[best_idx]

                if best_score > 0.5:
                    current_name = names[best_idx]
                    current_color = (0, 255, 0)
                    face_verified = True
                    face_lock_until = now + valid_time
                else:
                    current_name = "Unknown"
                    current_color = (0, 0, 255)
                    if now > face_lock_until:
                        face_verified = False

                # Draw face bounding box ONLY if DEBUG_DRAW is True
                if DEBUG_DRAW:
                    fa = largest_face["facial_area"]
                    scale_factor = frame.shape[1] / small_frame.shape[1]
                    x, y, w, h = [int(v * scale_factor) for v in (fa["x"], fa["y"], fa["w"], fa["h"])]
                    cv2.rectangle(frame, (x, y), (x + w, y + h), current_color, 2)
                    cv2.putText(frame, current_name, (x, y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, current_color, 2)

        except Exception:
            if now > face_lock_until:
                face_verified = False
            current_name = "Error"
            current_color = (0, 0, 255)

    # ================= BARCODE SCANNING =================
    if frame_count % barcode_every == 0:
        if now > barcode_lock_until:
            barcode_verified = False

        roi = frame[roi_coords[1]:roi_coords[3], roi_coords[0]:roi_coords[2]]
        gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        for code in decode(gray_roi):
            last_barcode = code.data.decode("utf-8").strip()

            if DEBUG_DRAW:
                bx, by, bw, bh = code.rect
                bx += roi_coords[0]
                by += roi_coords[1]
                cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), (0, 255, 255), 2)
                cv2.putText(frame, last_barcode, (bx, by - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

            if current_name in barcodes:
                expected = barcodes[current_name].strip()
                if last_barcode == expected:
                    barcode_verified = True
                    barcode_lock_until = now + valid_time
                else:
                    barcode_verified = False
                    barcode_lock_until = 0

    # Draw static blue ROI box (always)
    cv2.rectangle(frame, (roi_coords[0], roi_coords[1]), (roi_coords[2], roi_coords[3]), (255, 255, 0), 2)

    # ================= FINAL STATE =================
    access = face_verified and barcode_verified
    if access:
        cv2.putText(frame, "ACCESS GRANTED", (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 3)

    # Only write JSON if state changed
    state = {"authorized": access, "user": current_name if access else None}
    if state != last_write:
        with open("auth_status.json", "w") as f:
            json.dump(state, f)
        last_write = state

    # Draw status overlay (always)
    cv2.putText(frame, f"Face:{face_verified} | Barcode:{barcode_verified}",
                (20, frame.shape[0] - 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

    # Show frame
    cv2.imshow("Face + Barcode Verification", frame)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()
