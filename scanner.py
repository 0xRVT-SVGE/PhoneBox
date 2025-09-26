# scanner.py
import cv2
from deepface import DeepFace
from pyzbar.pyzbar import decode, ZBarSymbol
import numpy as np
import time
import threading
import json
from queue import Queue

# ==================== GLOBAL STATE ==================== #
latest_frame = None
frame_lock = threading.Lock()
scan_request = {"running": False}
auth_status = {"authorized": False, "user": None}

# Persistent verification state
face_verified = False
barcode_verified = False
current_name = "Idle"
face_lock_until = 0
barcode_lock_until = 0
valid_time = 7

# Detection interval (seconds)
FACE_INTERVAL = 0.5
BARCODE_INTERVAL = 0.5

# Task queue for async worker
task_queue = Queue()
scan_results = {"face_verified": False, "barcode_verified": False, "current_name": "Idle"}

# Face recognition scaling
SCALED_WIDTH = 480

# ==================== HELPERS ==================== #
def l2_normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec

def get_latest_frame():
    """Return latest frame safely for MJPEG streaming."""
    global latest_frame
    with frame_lock:
        return latest_frame.copy() if latest_frame is not None else None

# ==================== LOAD DATABASE ==================== #
with open("database.json", "r") as f:
    database = json.load(f)

names = list(database.keys())
embeddings = np.array([
    l2_normalize(np.array(database[name]["embedding"], dtype=np.float32))
    for name in names
])
barcodes = {name: database[name]["barcode"] for name in names}

print(f"[+] Loaded {len(names)} known identities from database.json")

# ==================== PRELOAD DEEPFACE MODEL ==================== #
print("[*] Loading SFace model...")
DeepFace.build_model("SFace")  # preloaded model
print("[+] Model loaded")

# ==================== WORKER THREAD ==================== #
def scan_worker():
    """Handles face recognition and barcode scanning asynchronously."""
    global scan_results, face_lock_until, barcode_lock_until

    last_face_scan = 0
    last_barcode_scan = 0

    while True:
        frame, roi_coords, timestamp, debug_draw = task_queue.get()
        if frame is None:
            break

        face_ok = scan_results["face_verified"]
        barcode_ok = scan_results["barcode_verified"]
        name = scan_results["current_name"]

        # ====== Face Recognition ======
        if timestamp - last_face_scan > FACE_INTERVAL:
            last_face_scan = timestamp
            try:
                scale_factor = SCALED_WIDTH / frame.shape[1]
                scaled_height = int(frame.shape[0] * scale_factor)
                small_frame = cv2.resize(frame, (SCALED_WIDTH, scaled_height))

                results = DeepFace.represent(
                    img_path=small_frame,
                    model_name="SFace",
                    detector_backend="opencv",
                    enforce_detection=False
                )

                if results:
                    largest_face = max(results, key=lambda f: f["facial_area"]["w"]*f["facial_area"]["h"])
                    embedding = l2_normalize(np.array(largest_face["embedding"], dtype=np.float32))
                    similarities = np.dot(embeddings, embedding)
                    best_idx = np.argmax(similarities)
                    best_score = similarities[best_idx]

                    if best_score > 0.5:
                        name = names[best_idx]
                        face_ok = True
                        face_lock_until = timestamp + valid_time
                    else:
                        name = "Unknown"
                        if timestamp > face_lock_until:
                            face_ok = False

                    # Debug draw only if requested
                    if debug_draw:
                        fa = largest_face["facial_area"]
                        x, y, w_f, h_f = [int(v / scale_factor) for v in (fa["x"], fa["y"], fa["w"], fa["h"])]
                        color = (0, 255, 0) if face_ok else (0, 0, 255)
                        cv2.rectangle(frame, (x, y), (x+w_f, y+h_f), color, 2)
                        cv2.putText(frame, name, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

            except Exception:
                if timestamp > face_lock_until:
                    face_ok = False
                name = "Error"

        # ====== Barcode Scanning ======
        if timestamp - last_barcode_scan > BARCODE_INTERVAL:
            last_barcode_scan = timestamp
            roi = frame[roi_coords[1]:roi_coords[3], roi_coords[0]:roi_coords[2]]
            gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            for code in decode(gray_roi, symbols=[ZBarSymbol.QRCODE]):
                last_barcode = code.data.decode("utf-8").strip()
                if name in barcodes:
                    expected = barcodes[name].strip()
                    barcode_ok = last_barcode == expected
                    if barcode_ok:
                        barcode_lock_until = timestamp + valid_time

                # Debug draw only if requested
                if debug_draw:
                    bx, by, bw, bh = code.rect
                    bx += roi_coords[0]
                    by += roi_coords[1]
                    cv2.rectangle(frame, (bx, by), (bx+bw, by+bh), (0, 255, 255), 2)
                    cv2.putText(frame, last_barcode, (bx, by-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)

        scan_results["face_verified"] = face_ok
        scan_results["barcode_verified"] = barcode_ok
        scan_results["current_name"] = name

# ==================== PROCESS FRAME ==================== #
def process_frame(frame, timestamp, scanning=False, DEBUG_DRAW=False):
    global latest_frame, scan_results

    h, w = frame.shape[:2]
    roi_w, roi_h = w // 2, h // 2
    roi_coords = (0, h - roi_h, roi_w, h)

    # Send frame to worker
    if scanning:
        task_queue.put((frame.copy(), roi_coords, timestamp, DEBUG_DRAW))

    # Draw persistent blue ROI on full-res frame
    cv2.rectangle(frame, (roi_coords[0], roi_coords[1]), (roi_coords[2], roi_coords[3]), (255, 255, 0), 2)

    # Overlay Face/Barcode status on full-res frame
    if scanning:
        face_ok = scan_results["face_verified"]
        barcode_ok = scan_results["barcode_verified"]
        name = scan_results["current_name"]
        auth_status.update({
            "authorized": face_ok and barcode_ok,
            "user": name if (face_ok and barcode_ok) else None
        })
    else:
        face_ok = barcode_ok = False
        auth_status.update({"authorized": False, "user": None})
        name = "Idle"

    cv2.putText(frame, f"Face:{face_ok} | Barcode:{barcode_ok}", (20, frame.shape[0]-20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)

    if scanning and face_ok and barcode_ok:
        cv2.putText(frame, "ACCESS GRANTED", (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0,255,0), 3)

    # Update latest_frame for streaming
    with frame_lock:
        latest_frame = frame

# ==================== MAIN LOOP ==================== #
def scanner_loop():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[-] Camera not accessible")
        return

    ret, frame = cap.read()
    if not ret:
        print("[-] Failed to read initial frame")
        return

    global latest_frame
    latest_frame = frame.copy()

    DEBUG_DRAW = False

    # Start worker thread
    threading.Thread(target=scan_worker, daemon=True).start()

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        timestamp = time.time()
        scanning = scan_request["running"]
        process_frame(frame, timestamp, scanning=scanning, DEBUG_DRAW=DEBUG_DRAW)

        # Display full-res video for debug
        if DEBUG_DRAW:
            cv2.imshow("Face + Barcode Verification", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    cap.release()
    cv2.destroyAllWindows()
    task_queue.put((None, None, None, False))  # stop worker

# ==================== START SCANNER IN BACKGROUND ==================== #
def start_background_scanner():
    threading.Thread(target=scanner_loop, daemon=True).start()
