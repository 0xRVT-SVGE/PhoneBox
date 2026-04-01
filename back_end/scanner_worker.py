# ============================================================
# FILE: back_end/scanner_worker.py
# ============================================================
"""
Scan worker — face + barcode verification.

Optimization vs previous version
──────────────────────────────────
fetch_student_by_sid previously made an HTTP GET to
http://127.0.0.1:5000/api/students/{sid} — a full round-trip through
Flask's request handling, JSON serialisation, network stack, and back.
That adds 5–30 ms per barcode scan and creates a circular dependency
(scanner → Flask API → DB) when Flask is running in the same process.

It now queries the DB directly via the shared sync connection pool,
cutting the lookup to <1 ms and removing the circular dependency.
The returned dict mirrors the old API shape so no downstream code changed.

Shutdown ownership: scanner_loop calls stop_scan() when it exits.
"""

import logging
import time
import threading
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from deepface import DeepFace
from pyzbar.pyzbar import decode, ZBarSymbol
from back_end.scanner_state import scanner_state

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.5
VALID_TIME           = 7
SCALED_WIDTH         = 720
FACE_INTERVAL        = 0.5
BARCODE_INTERVAL     = 0.5

_client_id        = None
_executor         = ThreadPoolExecutor(max_workers=1)
_scan_start_event = threading.Event()
_scan_stop_event  = threading.Event()


# ============================================================
# UTILITIES
# ============================================================

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


def fetch_student_by_sid(sid: str):
    """
    Direct DB lookup for a student by SID.

    Replaces the old HTTP GET to http://127.0.0.1:5000/api/students/{sid}.
    Uses the shared sync connection pool (get_conn/put_conn) which is
    already imported and used everywhere else in the backend.

    Returns a dict with keys: sid, first_name, last_name, embed
    (same shape as the old API response), or None on error / not found.
    """
    try:
        from back_end.Database.db import get_conn, put_conn
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sid, first_name, last_name, embed
                    FROM students
                    WHERE sid = %s
                    LIMIT 1;
                    """,
                    (sid,),
                )
                row = cur.fetchone()
        finally:
            put_conn(conn)

        if row is None:
            return None

        sid_val, first_name, last_name, embed_raw = row
        return {
            "sid":        sid_val,
            "first_name": first_name,
            "last_name":  last_name,
            "embed":      parse_pg_array(embed_raw),
        }

    except Exception as e:
        logger.warning(f"fetch_student_by_sid DB error for SID={sid}: {e}")
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
    emit_if_changed(
        {"authorized": False, "user": None},
        {
            "face_verified":         False,
            "barcode_verified":      False,
            "current_name":          "Idle",
            "badge_timeout_exceeded": False,
        },
    )

    face_ok          = False
    barcode_ok       = False
    name             = "Idle"
    timeout          = False
    last_face_scan   = 0
    last_barcode_scan = 0
    face_future      = None
    student          = None
    sid              = None

    while not _scan_stop_event.is_set():
        task = scanner_state.task_queue.get()

        if _scan_stop_event.is_set():
            break

        frame, roi_coords, timestamp = task

        if frame is None:   # sentinel from stop_scan()
            break

        # ── Barcode ─────────────────────────────────────────
        if timestamp - last_barcode_scan > BARCODE_INTERVAL:
            last_barcode_scan = timestamp
            roi = frame[roi_coords[1]:roi_coords[3],
                        roi_coords[0]:roi_coords[2]]

            if student is None:
                decoded = decode(
                    cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY),
                    symbols=[ZBarSymbol.CODE128],
                )
                if decoded:
                    sid     = decoded[0].data.decode("utf-8").strip()
                    student = fetch_student_by_sid(sid)

                    if student and student.get("embed") is not None:
                        barcode_ok = True
                        scanner_state.barcode_lock_until = timestamp + VALID_TIME
                        scanner_state.current_student    = student
                        scanner_state.current_embed      = l2_normalize(student["embed"])
                        name = (
                            f"{student.get('first_name', '')} "
                            f"{student.get('last_name', '')}"
                        ).strip()
                        scanner_state.update_last_barcode()
                    else:
                        barcode_ok               = False
                        scanner_state.current_embed   = None
                        scanner_state.current_student = None

        # ── Timeout ──────────────────────────────────────────
        if scanner_state.badge_timeout_exceeded() and not barcode_ok:
            timeout    = True
            barcode_ok = False
            face_ok    = False
            break

        # ── Face verification ─────────────────────────────────
        if barcode_ok and scanner_state.current_embed is not None:
            if timestamp - last_face_scan > FACE_INTERVAL:
                last_face_scan = timestamp
                scale   = SCALED_WIDTH / frame.shape[1]
                resized = cv2.resize(
                    frame, (SCALED_WIDTH, int(frame.shape[0] * scale))
                )
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
                    live_embed = l2_normalize(
                        np.array(largest["embedding"], dtype=np.float32)
                    )
                    sim = float(np.dot(live_embed, scanner_state.current_embed))
                    if sim >= SIMILARITY_THRESHOLD:
                        face_ok = True
                        break
            except Exception:
                pass
            finally:
                face_future = None

        # ── Expiration ───────────────────────────────────────
        now = time.time()
        if now > scanner_state.face_lock_until:
            face_ok = False
        if now > scanner_state.barcode_lock_until:
            barcode_ok = False

        emit_if_changed(
            scanner_state.auth_status,
            {
                "face_verified":         face_ok,
                "barcode_verified":      barcode_ok,
                "current_name":          name,
                "badge_timeout_exceeded": timeout,
            },
        )

    # Session complete
    scanner_state.scan_request["running"] = False
    emit_if_changed(
        {"authorized": face_ok and barcode_ok, "user": sid},
        {
            "face_verified":         face_ok,
            "barcode_verified":      barcode_ok,
            "current_name":          name,
            "badge_timeout_exceeded": timeout,
        },
    )


# ============================================================
# PERSISTENT WORKER
# ============================================================

def scan_worker():
    while True:
        _scan_start_event.wait()
        _scan_start_event.clear()
        _scan_stop_event.clear()

        run_scan_session()

        scanner_state.current_embed   = None
        scanner_state.current_student = None


# ============================================================
# PUBLIC CONTROL API
# ============================================================

def start_scan(client_id: str):
    global _client_id
    _client_id = client_id
    _scan_stop_event.clear()
    _scan_start_event.set()


def stop_scan():
    _scan_stop_event.set()
    scanner_state.task_queue.put((None, None, None))