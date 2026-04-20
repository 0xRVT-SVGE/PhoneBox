# ============================================================
# FILE: back_end/scanner_worker.py
# ============================================================
"""
Scan worker — face + barcode verification.

Shutdown ownership: scanner_loop calls stop_scan() when it exits.
This module does not need to know about the global stop_event.
"""

import json
import re
import time
import threading
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from deepface import DeepFace
from pyzbar.pyzbar import decode, ZBarSymbol
import requests
from back_end.scanner_state import scanner_state
from back_end.config import ScannerConfig as _SC, ServerConfig as _SVC

# Edit back_end/config.py → ScannerConfig / ServerConfig to change these.
API_BASE             = _SVC.STUDENT_API_BASE
SIMILARITY_THRESHOLD = _SC.SIMILARITY_THRESHOLD
VALID_TIME           = _SC.BADGE_VALID_TIME
SCALED_WIDTH         = _SC.SCALED_WIDTH
FACE_INTERVAL        = _SC.FACE_INTERVAL
BARCODE_INTERVAL     = _SC.BARCODE_INTERVAL

_client_id = None
_executor = ThreadPoolExecutor(max_workers=1)
_scan_start_event = threading.Event()
_scan_stop_event = threading.Event()

# Pre-compiled regex for parse_pg_array (was imported+compiled inside the fn).
_PG_ARRAY_SPLIT_RE = re.compile(r",\s*")

# Cached resize scale factor: SCALED_WIDTH / camera_width.
# Camera resolution is fixed for the lifetime of the process.  Computed once
# on first face-scan call then reused — avoids a float division every 0.5 s.
_resize_scale: float | None = None


# ============================================================
# UTILITIES
# ============================================================

def l2_normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def parse_pg_array(embed_value):
    # json and re are now module-level imports — no per-call overhead.
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
                return np.array(list(map(float, _PG_ARRAY_SPLIT_RE.split(clean))),
                                dtype=np.float32)
        except Exception:
            return None
    return None


def fetch_student_by_sid(sid):
    try:
        r = requests.get(f"{API_BASE}/{sid}", timeout=3)
        if r.status_code == 200:
            wrapper = r.json()
            student = wrapper["data"]
            student["embed"] = parse_pg_array(student.get("embed"))
            return student
    except Exception:
        pass
    return None


def _deepface_represent(resized):
    return DeepFace.represent(
        img_path=resized,
        model_name="SFace",
        detector_backend="opencv",
        enforce_detection=False,
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
        scanner_state.emit_with_callbacks(_client_id)


# ============================================================
# SCAN SESSION
# ============================================================

def run_scan_session():
    """Execute a single scan session until completion or stop signal."""
    global _resize_scale

    emit_if_changed(
        {"authorized": False, "user": None},
        {
            "face_verified": False,
            "barcode_verified": False,
            "current_name": "Idle",
            "badge_timeout_exceeded": False
        },
    )

    face_ok = False
    barcode_ok = False
    name = "Idle"
    timeout = False
    last_face_scan = 0
    last_barcode_scan = 0
    face_future = None
    student = None
    sid = None

    while not _scan_stop_event.is_set():
        task = scanner_state.task_queue.get()

        if _scan_stop_event.is_set():
            break

        frame, roi_coords, timestamp = task

        # Sentinel value from stop_scan()
        if frame is None:
            break

        # --- BARCODE DETECTION ---
        if timestamp - last_barcode_scan > BARCODE_INTERVAL:
            last_barcode_scan = timestamp
            roi = frame[roi_coords[1]:roi_coords[3], roi_coords[0]:roi_coords[2]]

            if student is None:
                decoded = decode(
                    cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY),
                    symbols=[ZBarSymbol.CODE128]
                )
                if decoded:
                    sid = decoded[0].data.decode("utf-8").strip()
                    student = fetch_student_by_sid(sid)

                    if student and student.get("embed") is not None:
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
            timeout = True
            barcode_ok = False
            face_ok = False
            break

        # --- FACE VERIFICATION ---
        if barcode_ok and scanner_state.current_embed is not None:
            if timestamp - last_face_scan > FACE_INTERVAL:
                last_face_scan = timestamp

                # Compute resize scale once per process lifetime — camera
                # resolution is fixed, so the ratio never changes.
                if _resize_scale is None:
                    _resize_scale = SCALED_WIDTH / frame.shape[1]
                scale = _resize_scale

                resized = cv2.resize(frame, (SCALED_WIDTH, int(frame.shape[0] * scale)))
                if face_future is None or face_future.done():
                    face_future = _executor.submit(_deepface_represent, resized)

        if face_future and face_future.done():
            try:
                results = face_future.result(timeout=0)
                if results:
                    largest = max(
                        results,
                        key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"],
                    )
                    # np.asarray avoids a copy when the source is already array-like.
                    live_embed = l2_normalize(
                        np.asarray(largest["embedding"], dtype=np.float32)
                    )
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
            {
                "face_verified": face_ok,
                "barcode_verified": barcode_ok,
                "current_name": name,
                "badge_timeout_exceeded": timeout
            },
        )

    # Session complete
    scanner_state.scan_request["running"] = False
    emit_if_changed(
        {"authorized": face_ok and barcode_ok, "user": sid},
        {
            "face_verified": face_ok,
            "barcode_verified": barcode_ok,
            "current_name": name,
            "badge_timeout_exceeded": timeout
        },
    )


# ============================================================
# PERSISTENT WORKER  (runs for the lifetime of the process)
# ============================================================

def scan_worker():
    """
    Persistent worker that waits for scan sessions.

    Lifetime: same as the process — started once by server_main,
    exits naturally when the process exits (daemon thread).

    Shutdown: scanner_loop calls stop_scan() which unblocks any
    blocking queue.get() via a sentinel and sets _scan_stop_event.
    The worker then falls through and waits on _scan_start_event again,
    where it will block until the process dies (daemon thread).
    """
    while True:
        _scan_start_event.wait()   # 0% CPU while idle
        _scan_start_event.clear()
        _scan_stop_event.clear()

        run_scan_session()

        scanner_state.current_embed = None
        scanner_state.current_student = None


# ============================================================
# PUBLIC CONTROL API
# ============================================================

def start_scan(client_id: str):
    """Start a scan session for the given WebSocket client."""
    global _client_id
    _client_id = client_id
    _scan_stop_event.clear()
    _scan_start_event.set()


def stop_scan():
    """
    Stop the current scan session.

    Called by scanner_loop when it exits — not by server_main directly.
    Puts a sentinel into the task queue to unblock any blocking get().
    """
    _scan_stop_event.set()
    scanner_state.task_queue.put((None, None, None))