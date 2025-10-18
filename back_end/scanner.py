# back_end/scanner.py
import cv2
from deepface import DeepFace
from pyzbar.pyzbar import decode, ZBarSymbol
import numpy as np
import time
import threading
import requests
from queue import Queue
import json
import re

# ===================================================== #
# ===================== CONFIG ========================= #
# ===================================================== #
API_BASE = "http://127.0.0.1:5000/api/students"
SIMILARITY_THRESHOLD = 0.5
VALID_TIME = 7
SCALED_WIDTH = 720
FACE_INTERVAL = 0.5
BARCODE_INTERVAL = 0.5

# ===================================================== #
# ===================== GLOBAL STATE =================== #
# ===================================================== #
latest_frame = None
frame_lock = threading.Lock()
task_queue = Queue()
scan_request = {"running": False}
auth_status = {"authorized": False, "user": None}
scan_results = {"face_verified": False, "barcode_verified": False, "current_name": "Idle"}

current_student = None
current_embed = None
face_lock_until = 0
barcode_lock_until = 0
stop_requested = False  # Flag to stop scanning once matched


# ===================================================== #
# ===================== HELPERS ======================== #
# ===================================================== #
def l2_normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def parse_pg_array(embed_value):
    """Convert Postgres-stored embedding to np.float32 array."""
    if embed_value is None:
        return None

    if isinstance(embed_value, (list, tuple, np.ndarray)):
        return np.array(embed_value, dtype=np.float32)

    if isinstance(embed_value, str):
        try:
            if embed_value.strip().startswith("["):
                return np.array(json.loads(embed_value), dtype=np.float32)
            if embed_value.strip().startswith("{"):
                clean = embed_value.strip("{}").strip()
                if not clean:
                    return None
                return np.array(list(map(float, re.split(r",\s*", clean))), dtype=np.float32)
        except Exception as e:
            print(f"[PARSE ERROR] {e}")

    return None


def fetch_student_by_sid(sid):
    """Fetch student record from Flask/Postgres API."""
    try:
        r = requests.get(f"{API_BASE}/{sid}", timeout=3)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "embed" in data:
                data["embed"] = parse_pg_array(data["embed"])
            print(f"[DB] Student {sid} found.")
            return data
        else:
            print(f"[DB] Student {sid} not found ({r.status_code})")
    except Exception as e:
        print(f"[DB ERROR] {e}")
    return None


def get_latest_frame():
    """Return latest frame safely for WebRTC streaming."""
    with frame_lock:
        return latest_frame.copy() if latest_frame is not None else None


# ===================================================== #
# ===================== MODEL LOAD ===================== #
# ===================================================== #
print("[*] Loading SFace model (singleton)...")
DeepFace.build_model("SFace")
print("[+] Model ready.")


# ===================================================== #
# ===================== SCANNER WORKER ================= #
# ===================================================== #
def scan_worker():
    """Handles barcode + face recognition asynchronously."""
    global current_student, current_embed, face_lock_until, barcode_lock_until, stop_requested

    last_face_scan = 0
    last_barcode_scan = 0

    while True:
        task = task_queue.get()
        if task is None:
            break

        if stop_requested:
            continue

        frame, roi_coords, timestamp, debug = task
        if frame is None:
            continue

        face_ok = scan_results["face_verified"]
        barcode_ok = scan_results["barcode_verified"]
        name = scan_results["current_name"]

        # ===== BARCODE DETECTION ===== #
        if timestamp - last_barcode_scan > BARCODE_INTERVAL:
            last_barcode_scan = timestamp
            roi = frame[roi_coords[1]:roi_coords[3], roi_coords[0]:roi_coords[2]]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            decoded = decode(gray, symbols=[ZBarSymbol.CODE128])

            if decoded:
                sid = decoded[0].data.decode("utf-8").strip()
                print(f"[SCAN] Barcode detected: {sid}")
                student = fetch_student_by_sid(sid)

                if student and isinstance(student.get("embed"), np.ndarray):
                    barcode_ok = True
                    barcode_lock_until = timestamp + VALID_TIME
                    current_student = student
                    current_embed = l2_normalize(student["embed"])
                    name = f"{student.get('first_name', '')} {student.get('last_name', '')}".strip()
                    print(f"[DB] Loaded student: {name}")
                else:
                    print("[WARN] Invalid or missing embed from DB")
                    barcode_ok = False
                    current_embed = None
                    current_student = None

        # ===== FACE RECOGNITION ===== #
        if barcode_ok and current_embed is not None and timestamp - last_face_scan > FACE_INTERVAL:
            last_face_scan = timestamp
            try:
                scale_factor = SCALED_WIDTH / frame.shape[1]
                resized = cv2.resize(frame, (SCALED_WIDTH, int(frame.shape[0] * scale_factor)))

                results = DeepFace.represent(
                    img_path=resized,
                    model_name="SFace",
                    detector_backend="opencv",
                    enforce_detection=False
                )

                if results:
                    largest = max(results, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"])
                    live_embed = l2_normalize(np.array(largest["embedding"], dtype=np.float32))
                    sim = float(np.dot(live_embed, current_embed))

                    if sim >= SIMILARITY_THRESHOLD:
                        face_ok = True
                        face_lock_until = timestamp + VALID_TIME
                        stop_requested = True
                        scan_request["running"] = False
                        auth_status.update({"authorized": True, "user": name})
                        print(f"[ACCESS] Match ({sim:.3f}) for {name}")
                        print("[SYSTEM] Successful match — stopping scan.")
                        continue
                    else:
                        face_ok = False
                        print(f"[DENIED] Mismatch ({sim:.3f})")

            except Exception as e:
                print(f"[FACE ERROR] {e}")

        # ===== EXPIRATION ===== #
        now = time.time()
        if now > face_lock_until:
            face_ok = False
        if now > barcode_lock_until:
            barcode_ok = False

        scan_results.update({
            "face_verified": face_ok,
            "barcode_verified": barcode_ok,
            "current_name": name
        })


# ===================================================== #
# ===================== FRAME LOOP ===================== #
# ===================================================== #
def process_frame(frame, timestamp, scanning=False, debug=False):
    global latest_frame
    h, w = frame.shape[:2]
    roi_w, roi_h = w // 2, h // 2
    roi_coords = (0, h - roi_h, roi_w, h)

    if scanning and not stop_requested:
        task_queue.put((frame.copy(), roi_coords, timestamp, debug))

    cv2.rectangle(frame, (roi_coords[0], roi_coords[1]),
                  (roi_coords[2], roi_coords[3]), (255, 255, 0), 2)

    face_ok = scan_results["face_verified"]
    barcode_ok = scan_results["barcode_verified"]
    name = scan_results["current_name"]

    if scanning:
        auth_status.update({
            "authorized": face_ok and barcode_ok,
            "user": name if face_ok and barcode_ok else None
        })

    if face_ok and barcode_ok:
        cv2.putText(frame, f"ACCESS GRANTED: {name}", (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 3)
    else:
        cv2.putText(frame, f"Face:{face_ok} | Barcode:{barcode_ok}",
                    (20, h - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                    (0, 255, 0) if (face_ok and barcode_ok) else (255, 255, 255), 2)

    with frame_lock:
        latest_frame = frame


# ===================================================== #
# ===================== MAIN LOOP ====================== #
# ===================================================== #
def scanner_loop(debug=False):
    global stop_requested
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[-] Cannot open camera.")
        return

    threading.Thread(target=scan_worker, daemon=True).start()
    print("[Scanner] Running. Press 'q' to quit (debug only).")

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        process_frame(frame, time.time(), scanning=scan_request["running"], debug=debug)

        if debug:
            cv2.imshow("Scanner", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        if stop_requested:
            print("[SYSTEM] Scan stopped (manual or success).")
            scan_request["running"] = False
            time.sleep(1)

    cap.release()
    cv2.destroyAllWindows()
    task_queue.put(None)
    print("[Scanner] Stopped.")


def start_background_scanner():
    threading.Thread(target=scanner_loop, daemon=True).start()
