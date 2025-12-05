import time
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from deepface import DeepFace
from pyzbar.pyzbar import decode, ZBarSymbol
import requests
from back_end.scanner_state import scanner_state

API_BASE = "http://127.0.0.1:5000/api/students"
SIMILARITY_THRESHOLD = 0.5
VALID_TIME = 7
SCALED_WIDTH = 720
FACE_INTERVAL = 0.5
BARCODE_INTERVAL = 0.5

_executor = ThreadPoolExecutor(max_workers=2)

def l2_normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec

def parse_pg_array(embed_value):
    import json, re
    if embed_value is None:
        return None
    if isinstance(embed_value, (list, tuple, np.ndarray)):
        return np.array(embed_value, dtype=np.float32)
    if isinstance(embed_value, str):
        try:
            if embed_value.startswith("["):
                return np.array(json.loads(embed_value), dtype=np.float32)
            if embed_value.startswith("{"):
                clean = embed_value.strip("{}").strip()
                if not clean:
                    return None
                return np.array(list(map(float, re.split(r",\s*", clean))), dtype=np.float32)
        except Exception:
            return None
    return None

def fetch_student_by_sid(sid):
    try:
        r = requests.get(f"{API_BASE}/{sid}", timeout=3)
        if r.status_code == 200:
            wrapper = r.json()
            student = wrapper["data"]
            embed = student.get("embed", None)
            student["embed"] = parse_pg_array(embed)
            return student
    except Exception:
        pass
    return None


def _deepface_represent(resized):
    return DeepFace.represent(
        img_path=resized,
        model_name="SFace",
        detector_backend="opencv",
        enforce_detection=False
    )

def emit_if_changed(new_auth, new_results):
    changed = False
    if new_auth != scanner_state.auth_status:
        scanner_state.auth_status.update(new_auth)
        changed = True
    if new_results != scanner_state.scan_results:
        scanner_state.scan_results.update(new_results)
        changed = True
    if changed:
        scanner_state.emit_scan_status()

def scan_worker():
    emit_if_changed(
        {"authorized": False, "user": None},
        {"face_verified": False, "barcode_verified": False, "current_name": "Idle"}
    )
    face_ok = scanner_state.scan_results["face_verified"]
    barcode_ok = scanner_state.scan_results["barcode_verified"]
    name = scanner_state.scan_results["current_name"]
    timeout = False
    last_face_scan = 0
    last_barcode_scan = 0
    face_future = None
    student = None
    sid = None

    while not scanner_state.stop_requested and scanner_state.scan_request["running"]:
        try:
            task = scanner_state.task_queue.get(timeout=0.5)
        except Exception:
            continue

        if task is None or scanner_state.stop_requested:
            break

        frame, roi_coords, timestamp = task
        # --- BARCODE DETECTION ---
        if timestamp - last_barcode_scan > BARCODE_INTERVAL:
            last_barcode_scan = timestamp
            roi = frame[roi_coords[1]:roi_coords[3], roi_coords[0]:roi_coords[2]]
            if student is None:
                decoded = decode(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), symbols=[ZBarSymbol.CODE128])

            if decoded:
                sid = decoded[0].data.decode("utf-8").strip()
                print(sid)
                student = fetch_student_by_sid(sid)

                if student is not None and student.get("embed") is not None:
                    barcode_ok = True
                    scanner_state.barcode_lock_until = timestamp + VALID_TIME
                    scanner_state.current_student = student
                    scanner_state.current_embed = l2_normalize(student["embed"])
                    name = f"{student.get('first_name', '')} {student.get('last_name', '')}".strip()
                    scanner_state.update_last_barcode()
                else:
                    barcode_ok = False
                    scanner_state.current_embed = None
                    scanner_state.current_student = None

        # --- TIMEOUT ---
        if scanner_state.badge_timeout_exceeded() and not barcode_ok:
            print("[-] Badge timeout exceeded.")
            timeout = True
            barcode_ok = False
            face_ok = False
            break

        # --- FACE VERIFICATION ---
        if barcode_ok and scanner_state.current_embed is not None:
            if timestamp - last_face_scan > FACE_INTERVAL:
                last_face_scan = timestamp
                try:
                    scale = SCALED_WIDTH / frame.shape[1]
                    resized = cv2.resize(frame, (SCALED_WIDTH, int(frame.shape[0] * scale)))
                    if face_future is None or face_future.done():
                        face_future = _executor.submit(_deepface_represent, resized)
                except Exception:
                    continue

        if face_future and face_future.done():
            try:
                results = face_future.result(timeout=0)
                if results:
                    largest = max(results, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"])
                    live_embed = l2_normalize(np.array(largest["embedding"], dtype=np.float32))
                    sim = float(np.dot(live_embed, scanner_state.current_embed))
                    if sim >= SIMILARITY_THRESHOLD:
                        face_ok = True
                        break
            except Exception:
                pass
            finally:
                face_future = None

        # --- EXPIRATION ---
        now = time.time()
        if now > scanner_state.face_lock_until:
            face_ok = False
        if now > scanner_state.barcode_lock_until:
            barcode_ok = False

        emit_if_changed(
            scanner_state.auth_status,
            {"face_verified": face_ok, "barcode_verified": barcode_ok, "current_name": name}
        )

    # Clean shutdown
    print("[+] Scan worker finished.")
    scanner_state.stop_requested = False
    scanner_state.scan_request["running"] = False
    emit_if_changed(
        {"authorized": face_ok and barcode_ok, "user": sid if not None else None},
        {"face_verified": face_ok, "barcode_verified": barcode_ok, "current_name": name, "badge_timeout_exceeded": timeout}
    )