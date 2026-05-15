# ============================================================
# FILE: back_end/scanner_worker.py
# ============================================================
"""
Scan worker — face + barcode verification.

Opt #3: _deepface_represent uses ONNX Runtime (face_embedder) when
        available, falls back to DeepFace + TensorFlow.
Opt #26: fetch_student_by_sid uses Redis cache (scanner_worker_cache)
         when available, falls back to direct API call.

Shutdown ownership: scanner_loop calls stop_scan() when it exits.
"""

import json
import time
import threading
import logging
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from pyzbar.pyzbar import decode, ZBarSymbol
import requests

from back_end.scanner_state import scanner_state
from back_end.config import ScannerConfig as _SC, ServerConfig as _SVC

logger = logging.getLogger(__name__)

# Lazy-loaded DeepFace reference — populated on first fallback call.
# Keeping it lazy means the TF env-var suppressions in face_embedder.py
# always fire before TensorFlow is imported.
_DeepFace = None


API_BASE             = _SVC.STUDENT_API_BASE
SIMILARITY_THRESHOLD = _SC.SIMILARITY_THRESHOLD
VALID_TIME           = _SC.BADGE_VALID_TIME
SCALED_WIDTH         = _SC.SCALED_WIDTH
FACE_INTERVAL        = _SC.FACE_INTERVAL
BARCODE_INTERVAL     = _SC.BARCODE_INTERVAL

_client_id = None
_executor  = ThreadPoolExecutor(max_workers=1)
_scan_start_event = threading.Event()
_scan_stop_event  = threading.Event()

_resize_scale: float | None = None

# ── Opt #3: ONNX face embedder (graceful fallback if unavailable) ─────────────
try:
    from back_end.face_embedder import represent as _onnx_represent
    from back_end.face_embedder import is_onnx_ready as _onnx_ready
    _ONNX_MODULE_AVAILABLE = True
except ImportError:
    _onnx_represent        = None
    _onnx_ready            = lambda: False
    _ONNX_MODULE_AVAILABLE = False

# ── Opt #26: Redis student cache (graceful fallback if unavailable) ───────────
try:
    from back_end.scanner_worker_cache import fetch_student_cached as _cache_fetch
    _CACHE_AVAILABLE = True
except ImportError:
    _cache_fetch     = None
    _CACHE_AVAILABLE = False


# ============================================================
# UTILITIES
# ============================================================

def l2_normalize(vec):
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def parse_pg_array(embed_value):
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
                # CY3: PostgreSQL arrays use "," with no surrounding whitespace;
                # plain split(',') avoids the regex engine on every cache miss.
                return np.array(
                    [float(s) for s in clean.split(',')],
                    dtype=np.float32,
                )
        except Exception:
            return None
    return None


def _fetch_from_api(sid: str):
    """Direct REST API call — used by the cache-miss path."""
    try:
        r = requests.get(f"{API_BASE}/{sid}", timeout=_SVC.STUDENT_API_TIMEOUT)
        if r.status_code == 200:
            wrapper = r.json()
            student = wrapper["data"]
            student["embed"] = parse_pg_array(student.get("embed"))
            return student
    except Exception:
        pass
    return None


def fetch_student_by_sid(sid: str):
    """
    Fetch student by SID.

    Opt #26: wraps the API call with a 60-second Redis TTL cache so
    repeated barcode reads of the same student within one session never
    hit the DB more than once.  Falls back to a direct API call if
    Redis is unavailable or scanner_worker_cache is not installed.
    """
    if _CACHE_AVAILABLE:
        return _cache_fetch(sid, _fetch_from_api, parse_pg_array)
    return _fetch_from_api(sid)


def _deepface_represent(resized: np.ndarray):
    """
    Compute face embedding(s) from a BGR image.

    Opt #3: tries ONNX Runtime first (~3× faster on CPU, 10-50× on GPU).
    Falls back to DeepFace + TensorFlow if ONNX is unavailable or fails.

    Returns same format as DeepFace.represent():
      [{"embedding": [...], "facial_area": {"x","y","w","h"}}, ...]
    """
    if _ONNX_MODULE_AVAILABLE and _onnx_ready():
        try:
            return _onnx_represent(resized)
        except Exception as exc:
            logger.debug(f"[ScanWorker] ONNX failed, falling back: {exc}")

    # Lazy DeepFace import — only pays TF load cost on first fallback call.
    global _DeepFace
    if _DeepFace is None:
        from deepface import DeepFace as _df
        # Silence Python-level TF deprecation warnings after TF has loaded.
        try:
            import tensorflow as tf
            tf.get_logger().setLevel("ERROR")
        except Exception:
            pass
        _DeepFace = _df

    return _DeepFace.represent(
        img_path          = resized,
        model_name        = _SC.FACE_MODEL,
        detector_backend  = _SC.FACE_DETECTOR_BACKEND,
        enforce_detection = False,
    )


def emit_if_changed(new_auth, new_results):
    """
    B9: compare state via tuples instead of dicts.
    Tuple comparison short-circuits on first inequality (~10× faster).
    The dicts are still updated and emitted; only the change-detection is changed.
    """
    # auth keys: authorized, user
    new_auth_key = (new_auth.get("authorized"), new_auth.get("user"))
    cur_auth_key = (
        scanner_state.auth_status.get("authorized"),
        scanner_state.auth_status.get("user"),
    )
    # results keys: face_verified, barcode_verified, current_name, badge_timeout_exceeded
    new_res_key = (
        new_results.get("face_verified"),
        new_results.get("barcode_verified"),
        new_results.get("current_name"),
        new_results.get("badge_timeout_exceeded"),
    )
    cur_res_key = (
        scanner_state.scan_results.get("face_verified"),
        scanner_state.scan_results.get("barcode_verified"),
        scanner_state.scan_results.get("current_name"),
        scanner_state.scan_results.get("badge_timeout_exceeded"),
    )

    changed = False
    if new_auth_key != cur_auth_key:
        scanner_state.auth_status.update(new_auth)
        changed = True
    if new_res_key != cur_res_key:
        scanner_state.scan_results.update(new_results)
        changed = True
    if changed:
        scanner_state.emit_with_callbacks(_client_id)


# ============================================================
# SCAN SESSION
# ============================================================

def run_scan_session():
    global _resize_scale

    emit_if_changed(
        {"authorized": False, "user": None},
        {
            "face_verified":          False,
            "barcode_verified":       False,
            "current_name":           "Idle",
            "badge_timeout_exceeded": False,
        },
    )

    face_ok           = False
    barcode_ok        = False
    name              = "Idle"
    timeout           = False
    last_face_scan    = 0
    last_barcode_scan = 0
    face_future       = None
    student           = None
    sid               = None

    while not _scan_stop_event.is_set():
        task = scanner_state.task_queue.get()

        if _scan_stop_event.is_set():
            break

        frame, roi_coords, timestamp = task

        if frame is None:
            break

        # ── BARCODE ──────────────────────────────────────
        if timestamp - last_barcode_scan > BARCODE_INTERVAL:
            last_barcode_scan = timestamp
            roi = frame[roi_coords[1]:roi_coords[3], roi_coords[0]:roi_coords[2]]

            if student is None:
                decoded = decode(
                    cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY),
                    symbols=[ZBarSymbol.CODE128],
                )
                if decoded:
                    sid     = decoded[0].data.decode("utf-8").strip()
                    student = fetch_student_by_sid(sid)

                    if student and student.get("embed") is not None:
                        barcode_ok                        = True
                        scanner_state.barcode_lock_until  = timestamp + VALID_TIME
                        scanner_state.current_student     = student
                        scanner_state.current_embed       = l2_normalize(student["embed"])
                        name = (
                            f"{student.get('first_name', '')} "
                            f"{student.get('last_name', '')}".strip()
                        )
                        scanner_state.update_last_barcode()
                    else:
                        barcode_ok                    = False
                        scanner_state.current_embed   = None
                        scanner_state.current_student = None

        # ── TIMEOUT ──────────────────────────────────────
        if scanner_state.badge_timeout_exceeded() and not barcode_ok:
            timeout    = True
            barcode_ok = False
            face_ok    = False
            break

        # ── FACE ─────────────────────────────────────────
        if barcode_ok and scanner_state.current_embed is not None:
            if timestamp - last_face_scan > FACE_INTERVAL:
                last_face_scan = timestamp

                if _resize_scale is None:
                    _resize_scale = SCALED_WIDTH / frame.shape[1]
                scale   = _resize_scale
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

        # ── EXPIRATION ───────────────────────────────────
        now = time.time()
        if now > scanner_state.face_lock_until:
            face_ok = False
        if now > scanner_state.barcode_lock_until:
            barcode_ok = False

        emit_if_changed(
            scanner_state.auth_status,
            {
                "face_verified":          face_ok,
                "barcode_verified":       barcode_ok,
                "current_name":           name,
                "badge_timeout_exceeded": timeout,
            },
        )

    # Session complete
    scanner_state.scan_request["running"] = False
    emit_if_changed(
        {"authorized": face_ok and barcode_ok, "user": sid},
        {
            "face_verified":          face_ok,
            "barcode_verified":       barcode_ok,
            "current_name":           name,
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