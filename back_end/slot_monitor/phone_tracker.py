# ============================================================
# FILE: back_end/slot_monitor/phone_tracker.py
# ============================================================
"""
Phone Tracker — hybrid CSRT + motion-contour + QR-fallback pipeline.

Architecture
────────────
1. Frame-diff detection  — finds the phone entering the camera view.
2. CSRT tracker          — primary position source; tracks the whole object.
3. Motion contour        — per-frame independent detection; corrects CSRT drift
                           by re-anchoring the bbox to the actual moving region.
4. QR scanner (pyzbar)   — validation only, sampled every N frames.
                           Used to: confirm phone identity (Phase 1) and detect
                           placement (QR disappears = phone face-down = SUCCESS).

Failure modes handled
─────────────────────
• CSRT drifts off object  → motion contour re-centers bbox.
• QR temporarily invisible → grace window; tracker keeps bbox alive.
• CSRT fails completely    → fallback: QR re-detection resets tracker.
• QR absent too long       → qr_lost failure (Phase 1 only).

Overlay: only source, destination, and staging zones are drawn.
The background slot grid is intentionally suppressed to reduce clutter.
"""

import json
import logging
import math
import os
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from pyzbar.pyzbar import decode

logger = logging.getLogger(__name__)

# ── Timing ────────────────────────────────────────────────
DETECT_TIMEOUT          = 8.0    # s — wait for phone to appear
PLACEMENT_TIMEOUT       = 30.0   # s — total op budget

# ── ROI / Phase-2 → Phase-3 ───────────────────────────────
ROI_INTERSECT_THRESHOLD = 0.30   # fraction bbox∩ROI / bbox_area
ROI_HOLD_DURATION       = 0.4    # s bbox must stay in ROI before Phase 3
TRACKER_SUCCESS_TIMEOUT = 3.0    # s inside ROI; QR must disappear by then

# ── QR validation ─────────────────────────────────────────
_QR_CHECK_EVERY_N       = 3      # pyzbar every Nth frame
QR_ABSENT_FAIL_TIMEOUT  = 0.8    # s QR may be absent in Phase 1 before FAIL
                                  # (0.5s was too tight for rapid hand movement)

# Pre-compiled once; _check_qr is called every _QR_CHECK_EVERY_N frames.
_UUID_RE_TRACKER = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

# ── Hybrid tracker ────────────────────────────────────────
MOTION_BLUR_K           = 15     # Gaussian kernel for frame-diff
MOTION_THRESH           = 20     # pixel-diff threshold
MOTION_DILATE           = 3      # iterations
MOTION_MIN_AREA         = 1500   # px² — ignore small noise
MOTION_IOU_MERGE        = 0.20   # min IoU to consider motion = same object as CSRT
CSRT_REINIT_INTERVAL    = 12     # frames between CSRT re-inits with motion bbox
                                  # (prevents slow drift accumulation)
STAGING_HOLD_TIME       = 1.5    # s bbox must stay in staging zone

# ── Palette (BGR) ─────────────────────────────────────────
_COL_SOURCE        = (30, 130, 255)
_COL_DEST_BASE     = (40, 220, 255)
_COL_STAGING_EMPTY = [(200, 100,  30), (30, 100, 200)]
_COL_STAGING_OCC   = [(255, 180,  80), (80, 180, 255)]
_COL_BBOX_OK       = (20, 215,  20)
_COL_BBOX_WARN     = (20,  20, 215)
_COL_TEXT          = (255, 255, 255)
_FONT              = cv2.FONT_HERSHEY_SIMPLEX


# ══════════════════════════════════════════════════════════
# Drawing primitives
# ══════════════════════════════════════════════════════════

def _draw_dashed_line(frame, x1, y1, x2, y2, color, thickness=2, dash=12, gap=7):
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 1:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    pos, on = 0.0, True
    while pos < length:
        seg = min(dash if on else gap, length - pos)
        if on:
            p1 = (int(x1 + ux * pos),       int(y1 + uy * pos))
            p2 = (int(x1 + ux * (pos + seg)), int(y1 + uy * (pos + seg)))
            cv2.line(frame, p1, p2, color, thickness, cv2.LINE_AA)
        pos += seg
        on = not on


def _draw_dashed_rect(frame, x, y, w, h, color, thickness=2, dash=12, gap=7):
    x2, y2 = x + w, y + h
    _draw_dashed_line(frame, x,  y,  x2,  y, color, thickness, dash, gap)
    _draw_dashed_line(frame, x2, y,  x2, y2, color, thickness, dash, gap)
    _draw_dashed_line(frame, x2, y2,  x, y2, color, thickness, dash, gap)
    _draw_dashed_line(frame,  x, y2,  x,  y, color, thickness, dash, gap)


def _fill_alpha(frame, x, y, w, h, color, alpha=0.15):
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x + w, y + h), color, cv2.FILLED)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


def _corner_brackets(frame, x, y, w, h, color, arm=20, thickness=2):
    x2, y2 = x + w, y + h
    for (ax, ay), (bx, by), (cx, cy) in [
        ((x + arm, y),     (x, y),     (x,  y + arm)),
        ((x2 - arm, y),    (x2, y),    (x2, y + arm)),
        ((x, y2 - arm),    (x, y2),    (x + arm, y2)),
        ((x2, y2 - arm),   (x2, y2),   (x2 - arm, y2)),
    ]:
        cv2.line(frame, (ax, ay), (bx, by), color, thickness, cv2.LINE_AA)
        cv2.line(frame, (bx, by), (cx, cy), color, thickness, cv2.LINE_AA)


def _label(frame, text, x, y, color, scale=0.44, thick=1, pad=3):
    (tw, th), _ = cv2.getTextSize(text, _FONT, scale, thick)
    fh, fw = frame.shape[:2]
    x = max(pad, min(x, fw - tw - pad - 2))
    y = max(th + pad + 2, min(y, fh - pad - 2))
    cv2.rectangle(frame, (x - pad, y - th - pad), (x + tw + pad, y + pad),
                  (0, 0, 0), cv2.FILLED)
    cv2.putText(frame, text, (x, y), _FONT, scale, color, thick, cv2.LINE_AA)


def _pulse(base, period=1.2, lo=0.55, hi=1.0):
    f = lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * time.time() / period))
    return tuple(min(255, int(c * f)) for c in base)


# ══════════════════════════════════════════════════════════
# ROI file loader
# ══════════════════════════════════════════════════════════

_roi_cache: Optional[Dict[int, Tuple]] = None
_roi_lock  = threading.Lock()


def _ensure_cache() -> None:
    global _roi_cache
    if _roi_cache is not None:
        return
    roi_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "tools", "rois_top.json",
    )
    if not os.path.exists(roi_file):
        logger.warning(f"[Tracker] rois_top.json not found at {roi_file}")
        _roi_cache = {}
        return
    try:
        with open(roi_file) as f:
            data = json.load(f)
        _roi_cache = {i: tuple(int(v) for v in r) for i, r in enumerate(data)}
        logger.info(f"[Tracker] Loaded {len(_roi_cache)} top-cam ROIs from {roi_file}")
    except Exception as e:
        logger.error(f"[Tracker] Failed to load rois_top.json: {e}")
        _roi_cache = {}


def _load_top_roi(lid: int) -> Optional[Tuple]:
    with _roi_lock:
        _ensure_cache()
        return _roi_cache.get(lid)


def load_all_top_rois() -> Dict[int, Tuple]:
    with _roi_lock:
        _ensure_cache()
        return dict(_roi_cache) if _roi_cache else {}


# ══════════════════════════════════════════════════════════
# Overlay factories
# Only source, destination, and staging zones are drawn.
# Background slot grid is suppressed to reduce visual clutter.
# ══════════════════════════════════════════════════════════

def make_dvw_context_overlay(
    all_rois:   Dict[int, Tuple],
    source_lid: Optional[int],
    dest_lid:   int,
) -> Callable[[np.ndarray], None]:
    """DVW deposit/verify: only source and destination ROIs."""
    def draw(frame: np.ndarray) -> None:
        if source_lid is not None and source_lid in all_rois:
            rx, ry, rw, rh = all_rois[source_lid]
            _fill_alpha(frame, rx, ry, rw, rh, _COL_SOURCE, 0.12)
            _draw_dashed_rect(frame, rx, ry, rw, rh, _COL_SOURCE, 2)
            _label(frame, f"FROM  slot {source_lid + 1}", rx + 4, ry + rh - 6, _COL_SOURCE)

        if dest_lid in all_rois:
            rx, ry, rw, rh = all_rois[dest_lid]
            col = _pulse(_COL_DEST_BASE)
            _fill_alpha(frame, rx, ry, rw, rh, col, 0.13)
            _draw_dashed_rect(frame, rx, ry, rw, rh, col, 2)
            _corner_brackets(frame, rx, ry, rw, rh, col, arm=min(20, rw // 4, rh // 4))
            cx, cy = rx + rw // 2, ry + rh // 2
            cv2.arrowedLine(frame, (cx, cy - 16), (cx, cy + 16),
                            col, 2, cv2.LINE_AA, tipLength=0.35)
            _label(frame, f"\u25BC  SLOT {dest_lid + 1}  \u2014  PLACE HERE",
                   rx + 4, ry + rh + 17, col)
    return draw


def make_admin_session_overlay(
    all_slot_rois: Dict[int, Tuple],
    staging_rois:  List[Tuple],
    staged_pids:   List[Optional[str]],
    source_lid:    Optional[int] = None,
    dest_lid:      Optional[int] = None,
) -> Callable[[np.ndarray], None]:
    """
    Admin session: source, destination, and staging zones only.
    No background slot grid.
    """
    _staging = list(staging_rois)
    _pids    = list(staged_pids)

    def draw(frame: np.ndarray) -> None:
        # 1. Staging zones
        for i, roi in enumerate(_staging):
            if not roi or len(roi) < 4:
                continue
            rx, ry, rw, rh = roi
            pid      = _pids[i] if i < len(_pids) else None
            occupied = pid is not None
            col_e    = _COL_STAGING_EMPTY[i % len(_COL_STAGING_EMPTY)]
            col_o    = _COL_STAGING_OCC[i % len(_COL_STAGING_OCC)]
            col      = col_o if occupied else col_e
            name     = f"STAGING {i + 1}"
            if occupied:
                _fill_alpha(frame, rx, ry, rw, rh, col, 0.22)
                cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), col, 3)
                tail = pid[-8:] if pid and len(pid) > 8 else (pid or "")
                _label(frame, f"\u25CF {name}  [{tail}]",
                       rx + 4, ry + rh // 2 + 7, col)
            else:
                _draw_dashed_rect(frame, rx, ry, rw, rh, col, 2, dash=14, gap=6)
                _label(frame, f"\u25CB {name}  \u2014  empty",
                       rx + 4, ry + rh // 2 + 7, col)

        # 2. Source slot
        if source_lid is not None and source_lid in all_slot_rois:
            rx, ry, rw, rh = all_slot_rois[source_lid]
            same  = (source_lid == dest_lid)
            col   = _COL_SOURCE
            _fill_alpha(frame, rx, ry, rw, rh, col, 0.11)
            _draw_dashed_rect(frame, rx, ry, rw, rh, col, 2)
            label = f"FROM  slot {source_lid + 1}" + (" \u2194 RETURN HERE" if same else "")
            _label(frame, label, rx + 4, ry + rh - 6, col)
            if same:
                _corner_brackets(frame, rx, ry, rw, rh, col, arm=min(20, rw // 4, rh // 4))

        # 3. Destination slot (skip when same as source to avoid double-draw)
        if dest_lid is not None and dest_lid in all_slot_rois and dest_lid != source_lid:
            rx, ry, rw, rh = all_slot_rois[dest_lid]
            col = _pulse(_COL_DEST_BASE)
            _fill_alpha(frame, rx, ry, rw, rh, col, 0.13)
            _draw_dashed_rect(frame, rx, ry, rw, rh, col, 2)
            _corner_brackets(frame, rx, ry, rw, rh, col, arm=min(20, rw // 4, rh // 4))
            cx, cy = rx + rw // 2, ry + rh // 2
            cv2.arrowedLine(frame, (cx, cy - 16), (cx, cy + 16),
                            col, 2, cv2.LINE_AA, tipLength=0.35)
            _label(frame, f"\u25BC  SLOT {dest_lid + 1}  \u2014  PLACE HERE",
                   rx + 4, ry + rh + 17, col)
    return draw


# ══════════════════════════════════════════════════════════
# Hybrid tracker helpers
# ══════════════════════════════════════════════════════════

def _motion_bbox(prev_gray: np.ndarray, curr_gray: np.ndarray) -> Optional[Tuple[int,int,int,int]]:
    """
    Detect the largest moving object by frame-differencing.
    Returns (x, y, w, h) or None.
    """
    diff = cv2.absdiff(prev_gray, curr_gray)
    blur = cv2.GaussianBlur(diff, (MOTION_BLUR_K, MOTION_BLUR_K), 0)
    _, thr = cv2.threshold(blur, MOTION_THRESH, 255, cv2.THRESH_BINARY)
    thr = cv2.dilate(thr, None, iterations=MOTION_DILATE)
    contours, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < MOTION_MIN_AREA:
        return None
    return cv2.boundingRect(largest)


def _iou(a: Tuple[int,int,int,int], b: Tuple[int,int,int,int]) -> float:
    """Intersection-over-union of two (x,y,w,h) boxes."""
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _merge_bbox(csrt: Tuple[int,int,int,int],
                motion: Tuple[int,int,int,int],
                alpha: float = 0.4) -> Tuple[int,int,int,int]:
    """
    Blend CSRT and motion bboxes.
    alpha controls how much weight the motion bbox gets (0 = pure CSRT, 1 = pure motion).
    The size comes from CSRT (motion contours can be noisy); only the centre is blended.
    """
    cx_c = csrt[0]  + csrt[2]  / 2
    cy_c = csrt[1]  + csrt[3]  / 2
    cx_m = motion[0] + motion[2] / 2
    cy_m = motion[1] + motion[3] / 2
    cx = cx_c * (1 - alpha) + cx_m * alpha
    cy = cy_c * (1 - alpha) + cy_m * alpha
    w, h = csrt[2], csrt[3]
    return (int(cx - w / 2), int(cy - h / 2), w, h)


# ══════════════════════════════════════════════════════════
# PhoneTracker
# ══════════════════════════════════════════════════════════

class PhoneTracker:
    """
    Hybrid CSRT + motion-contour + QR-fallback phone tracker.

    CSRT tracks the whole object frame-to-frame.
    Motion contours independently detect the moving region and correct drift.
    QR scanning validates identity and detects placement (face-down).
    """

    def __init__(
        self,
        pid:              str,
        lid:              int,
        slot_roi:         Tuple,
        background_frame: np.ndarray,
        cancel_event:     threading.Event,
        socketio,
        client_id:        str,
        staging_rois:     Optional[List[Tuple]] = None,
    ):
        self._pid              = pid
        self._lid              = lid
        self._slot_roi         = slot_roi
        self._bg               = background_frame
        self._cancel_event     = cancel_event
        self._socketio         = socketio
        self._client_id        = client_id
        self._staging_rois:    List[Tuple] = staging_rois or []
        self._bbox:            Optional[Tuple] = None
        self._qr_visible:      bool = True
        self._on_success:      Optional[Callable] = None
        self._on_failure:      Optional[Callable] = None
        self._on_staged:       Optional[Callable] = None

    def start(self, on_success: Callable, on_failure: Callable,
              on_staged: Optional[Callable] = None) -> None:
        self._on_success = on_success
        self._on_failure = on_failure
        self._on_staged  = on_staged
        t = threading.Thread(
            target=self._run, daemon=True,
            name=f"PhoneTracker-{self._pid[:8]}-lid{self._lid}",
        )
        t.start()

    @staticmethod
    def _safe_frame(raw_fn, fallback_fn):
        frame = raw_fn()
        return frame if frame is not None else fallback_fn()

    def _run(self) -> None:
        from back_end.slot_monitor.camera.top_camera import top_camera
        try:
            logger.info(f"[Tracker] PID={self._pid} LID={self._lid} — detecting phone")
            bbox = self._detect_phone(top_camera)
            if bbox is None:
                reason = "cancelled" if self._cancel_event.is_set() else "detect_timeout"
                self._fail(reason, top_camera)
                return

            frame = self._safe_frame(top_camera.get_raw_frame, top_camera.get_frame)
            if frame is None:
                self._fail("detect_timeout", top_camera)
                return

            tracker = cv2.TrackerCSRT_create()
            tracker.init(frame, bbox)
            self._bbox = bbox
            top_camera.set_tracker_overlay(self._draw_overlay)
            logger.info(f"[Tracker] CSRT initialised at bbox={bbox}")
            self._track(tracker, top_camera)

        except Exception as e:
            logger.error(f"[Tracker] Unexpected error: {e}", exc_info=True)
            try:
                from back_end.slot_monitor.camera.top_camera import top_camera as tc
                tc.clear_tracker_overlay()
            except Exception:
                pass
            if self._on_failure:
                self._on_failure("error")

    def _detect_phone(self, top_camera) -> Optional[Tuple]:
        """Frame-diff detection against stored background. Uses raw frames."""
        bg_raw  = top_camera.get_raw_frame()
        bg_src  = bg_raw if bg_raw is not None else self._bg
        bg_gray = cv2.cvtColor(bg_src, cv2.COLOR_BGR2GRAY)
        bg_gray = cv2.GaussianBlur(bg_gray, (MOTION_BLUR_K, MOTION_BLUR_K), 0)
        deadline = time.time() + DETECT_TIMEOUT

        while time.time() < deadline:
            if self._cancel_event.is_set():
                return None
            if not top_camera.wait_for_frame(timeout=0.05):
                continue
            frame = self._safe_frame(top_camera.get_raw_frame, top_camera.get_frame)
            top_camera.clear_frame_event()
            if frame is None:
                continue
            gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY),
                                    (MOTION_BLUR_K, MOTION_BLUR_K), 0)
            result = _motion_bbox(bg_gray, gray)
            if result is not None:
                return result
        return None

    def _track(self, tracker, top_camera) -> None:
        """
        Hybrid tracking loop.

        Frame loop
        ----------
        Each frame:
          1. Update CSRT tracker.
          2. Compute motion bbox from consecutive raw frames.
          3. If motion and CSRT agree (IoU >= MOTION_IOU_MERGE): blend centres.
          4. Periodically re-init CSRT with blended bbox to correct drift.
          5. If CSRT fails: use motion bbox and re-init CSRT.
          6. If both fail: attempt QR re-detection; if found, re-init CSRT.

        QR validation (parallel, every N frames)
          Phase 1: QR must appear within QR_ABSENT_FAIL_TIMEOUT.
          Phase 3: QR disappearing = SUCCESS QR + SUCCESS TRACKER.

        Staging detection (Phase 1 only)
          bbox held in staging zone >= STAGING_HOLD_TIME -> on_staged().
        """
        deadline          = time.time() + PLACEMENT_TIMEOUT
        last_emit         = 0.0
        frame_count       = 0
        in_roi            = False
        roi_entry_ts:     Optional[float] = None
        phase3_ts:        Optional[float] = None
        staging_idx_held: int             = -1
        staging_hold_ts:  Optional[float] = None
        self._staging_qr_seen             = False

        # QR state
        last_qr_seen      = time.time()
        qr_confirmed_once = False

        # Previous raw frame for motion detection
        prev_raw: Optional[np.ndarray] = self._safe_frame(
            top_camera.get_raw_frame, top_camera.get_frame)
        prev_gray: Optional[np.ndarray] = (
            cv2.cvtColor(prev_raw, cv2.COLOR_BGR2GRAY) if prev_raw is not None else None
        )

        # CSRT state
        csrt_ok       = True
        frames_since_reinit = 0

        while time.time() < deadline:
            if self._cancel_event.is_set():
                self._fail("cancelled", top_camera)
                return

            if not top_camera.wait_for_frame(timeout=0.05):
                continue
            frame = self._safe_frame(top_camera.get_raw_frame, top_camera.get_frame)
            top_camera.clear_frame_event()
            if frame is None:
                continue

            fh, fw = frame.shape[:2]
            frame_count += 1

            # ── QR check (every N frames) ──────────────────────────────────
            if frame_count % _QR_CHECK_EVERY_N == 0:
                if self._check_qr(frame):
                    last_qr_seen      = time.time()
                    qr_confirmed_once = True
                self._qr_visible = (time.time() - last_qr_seen) < QR_ABSENT_FAIL_TIMEOUT

            qr_absent = time.time() - last_qr_seen

            # ── Motion bbox (current vs previous frame) ────────────────────
            curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            motion = None
            if prev_gray is not None:
                motion = _motion_bbox(prev_gray, curr_gray)
            prev_gray = curr_gray

            # ── CSRT update ────────────────────────────────────────────────
            if csrt_ok:
                ok, raw_bbox = tracker.update(frame)
                if ok:
                    bx, by, bw, bh = (int(v) for v in raw_bbox)
                    # Sanity-check: CSRT bbox still on-frame
                    if bx + bw <= 0 or bx >= fw or by + bh <= 0 or by >= fh:
                        csrt_ok = False
                    else:
                        frames_since_reinit += 1
                        # Blend with motion if they agree
                        if motion is not None:
                            iou = _iou((bx, by, bw, bh), motion)
                            if iou >= MOTION_IOU_MERGE:
                                bx, by, bw, bh = _merge_bbox((bx, by, bw, bh), motion)
                        # Periodic re-init to correct drift accumulation
                        if frames_since_reinit >= CSRT_REINIT_INTERVAL:
                            tracker = cv2.TrackerCSRT_create()
                            tracker.init(frame, (bx, by, bw, bh))
                            frames_since_reinit = 0
                else:
                    csrt_ok = False

            # ── CSRT failed: fallback to motion ────────────────────────────
            if not csrt_ok:
                if motion is not None:
                    bx, by, bw, bh = motion
                    # Re-init CSRT from motion bbox
                    tracker = cv2.TrackerCSRT_create()
                    tracker.init(frame, (bx, by, bw, bh))
                    csrt_ok             = True
                    frames_since_reinit = 0
                    logger.debug(f"[Tracker] PID={self._pid} CSRT re-init from motion")
                else:
                    # Both tracker and motion failed: try QR re-detection
                    if self._check_qr(frame):
                        # QR found — will reinitialise on next detect loop pass
                        # (don't block here; just log and continue)
                        logger.debug(f"[Tracker] PID={self._pid} both failed; QR still visible")
                        # If we've been without a bbox too long, fail
                        if qr_confirmed_once and qr_absent > QR_ABSENT_FAIL_TIMEOUT * 4:
                            self._fail("tracker_lost", top_camera)
                            return
                        # Update qr seen
                        last_qr_seen      = time.time()
                        qr_confirmed_once = True
                        self._qr_visible  = True
                        qr_absent         = 0.0
                        continue
                    else:
                        # Nothing to go on — fail
                        if in_roi:
                            # Last known good position was inside ROI -> SUCCESS TRACKER
                            top_camera.clear_tracker_overlay()
                            logger.info(f"[Tracker] PID={self._pid} lost inside ROI -> SUCCESS TRACKER")
                            if self._on_success:
                                self._on_success()
                        else:
                            self._fail("tracker_lost", top_camera)
                        return

            self._bbox = (bx, by, bw, bh)

            # ── Staging detection (Phase 1 only) ──────────────────────────
            # The bbox must:
            #   1. Overlap the staging zone for STAGING_HOLD_TIME seconds, AND
            #   2. The QR must be seen at least once during that hold.
            # This prevents false staging triggers from random motion in the
            # staging zone area (e.g. admin's arm passing over it).
            if not in_roi:
                cur_staging = self._in_which_staging(bx, by, bw, bh)
                if cur_staging >= 0:
                    if cur_staging != staging_idx_held:
                        staging_idx_held  = cur_staging
                        staging_hold_ts   = time.time()
                        # Reset QR-in-staging flag for this hold attempt
                        self._staging_qr_seen = False
                    else:
                        # Track QR visibility while held in staging zone
                        if self._qr_visible:
                            self._staging_qr_seen = True
                        if (staging_hold_ts and
                                (time.time() - staging_hold_ts) >= STAGING_HOLD_TIME and
                                getattr(self, '_staging_qr_seen', False)):
                            top_camera.clear_tracker_overlay()
                            logger.info(
                                f"[Tracker] PID={self._pid} QR-confirmed staging "
                                f"zone {staging_idx_held}"
                            )
                            if self._on_staged:
                                self._on_staged(staging_idx_held)
                            return
                else:
                    staging_idx_held          = -1
                    staging_hold_ts           = None
                    self._staging_qr_seen     = False

            # ── Phase 2: ROI entry (sustained) ─────────────────────────────
            now_in_roi = self._intersects_roi(bx, by, bw, bh)
            if now_in_roi:
                if roi_entry_ts is None:
                    roi_entry_ts = time.time()
                if not in_roi and (time.time() - roi_entry_ts) >= ROI_HOLD_DURATION:
                    in_roi    = True
                    phase3_ts = time.time()
                    logger.debug(f"[Tracker] PID={self._pid} Phase 3 started")
            else:
                roi_entry_ts = None
                if in_roi:
                    in_roi    = False
                    phase3_ts = None
                    logger.debug(f"[Tracker] PID={self._pid} exited ROI")

            # ── Phase 3: stabilisation ─────────────────────────────────────
            if in_roi:
                # QR disappeared while bbox in ROI = phone placed face-down
                if qr_confirmed_once and qr_absent > QR_ABSENT_FAIL_TIMEOUT:
                    top_camera.clear_tracker_overlay()
                    logger.info(f"[Tracker] PID={self._pid} QR gone inside ROI -> SUCCESS GLOBAL")
                    if self._on_success:
                        self._on_success()
                    return
                # TRACKER_SUCCESS_TIMEOUT: QR still visible too long
                if phase3_ts and (time.time() - phase3_ts) > TRACKER_SUCCESS_TIMEOUT:
                    self._fail("stabilization_timeout", top_camera)
                    return
            else:
                # Phase 1: QR must re-appear within tolerance (staging exempt)
                if qr_confirmed_once and qr_absent > QR_ABSENT_FAIL_TIMEOUT and staging_hold_ts is None:
                    self._fail("qr_lost", top_camera)
                    return

            # ── Progress update ────────────────────────────────────────────
            now = time.time()
            if now - last_emit > 0.5:
                self._emit_update(self._qr_visible, in_roi, qr_absent)
                last_emit = now

        self._fail("timeout", top_camera)

    def _intersects_roi(self, bx, by, bw, bh) -> bool:
        rx, ry, rw, rh = self._slot_roi
        ix1 = max(bx, rx);  iy1 = max(by, ry)
        ix2 = min(bx + bw, rx + rw);  iy2 = min(by + bh, ry + rh)
        if ix2 <= ix1 or iy2 <= iy1:
            return False
        inter     = (ix2 - ix1) * (iy2 - iy1)
        bbox_area = bw * bh
        return bbox_area > 0 and (inter / bbox_area) >= ROI_INTERSECT_THRESHOLD

    def _in_which_staging(self, bx, by, bw, bh) -> int:
        for i, roi in enumerate(self._staging_rois):
            if not roi or len(roi) < 4:
                continue
            rx, ry, rw, rh = int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])
            ix1 = max(bx, rx);  iy1 = max(by, ry)
            ix2 = min(bx + bw, rx + rw);  iy2 = min(by + bh, ry + rh)
            if ix2 <= ix1 or iy2 <= iy1:
                continue
            inter     = (ix2 - ix1) * (iy2 - iy1)
            bbox_area = bw * bh
            if bbox_area > 0 and (inter / bbox_area) >= ROI_INTERSECT_THRESHOLD:
                return i
        return -1

    def _check_qr(self, frame: np.ndarray) -> bool:
        """Check for this PID's QR code in the frame. UUID format only."""
        try:
            for obj in decode(frame):
                raw = obj.data.decode("utf-8", errors="ignore").strip()
                if raw.upper().startswith("PID:"):
                    raw = raw[4:].strip()
                if _UUID_RE_TRACKER.match(raw) and raw.lower() == self._pid.lower():
                    return True
        except Exception:
            pass
        return False

    def _fail(self, reason: str, top_camera=None) -> None:
        if top_camera is not None:
            top_camera.clear_tracker_overlay()
        logger.warning(f"[Tracker] PID={self._pid} LID={self._lid} failed: {reason}")
        if self._on_failure:
            self._on_failure(reason)

    def _emit_update(self, qr_visible, in_roi, qr_absent) -> None:
        try:
            self._socketio.emit(
                "tracking_update",
                {"pid": self._pid, "lid": self._lid,
                 "qr_visible": qr_visible, "in_roi": in_roi,
                 "qr_absent": round(qr_absent, 1)},
                to=self._client_id, namespace="/",
            )
        except Exception as e:
            logger.debug(f"[Tracker] emit tracking_update failed: {e}")

    def _draw_overlay(self, frame: np.ndarray) -> None:
        bbox = self._bbox
        if bbox is None:
            return
        bx, by, bw, bh = bbox
        col = _COL_BBOX_OK if self._qr_visible else _COL_BBOX_WARN
        cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), col, 2)
        arm = max(6, min(14, bw // 5, bh // 5))
        x2, y2 = bx + bw, by + bh
        # Corner brackets (4 L-shapes)
        for (ax, ay, ex, ey, cx, cy) in [
            (bx + arm, by,      bx,  by,      bx,  by + arm),
            (x2 - arm, by,      x2,  by,      x2,  by + arm),
            (bx,       y2-arm,  bx,  y2,      bx+arm,  y2),
            (x2,       y2-arm,  x2,  y2,      x2-arm,  y2),
        ]:
            cv2.line(frame, (ax, ay), (ex, ey), col, 2)
            cv2.line(frame, (ex, ey), (cx, cy), col, 2)
        _label(frame, "QR OK" if self._qr_visible else "QR NOT VISIBLE!", bx, y2 + 16, col)


# ══════════════════════════════════════════════════════════
# Public convenience
# ══════════════════════════════════════════════════════════

def create_tracker_for_operation(op, socketio) -> Optional[PhoneTracker]:
    from back_end.slot_monitor.camera.top_camera import top_camera
    if op.background_frame is None:
        top_camera.wait_for_frame(timeout=0.3)
        raw = top_camera.get_raw_frame()
        op.background_frame = raw if raw is not None else top_camera.get_frame()
    if op.background_frame is None:
        logger.warning(f"[Tracker] No background frame for PID={op.pid} lid={op.lid}")
        return None
    slot_roi = _load_top_roi(op.lid)
    if slot_roi is None:
        logger.warning(f"[Tracker] No top-cam ROI for lid={op.lid}")
        return None
    return PhoneTracker(
        pid              = op.pid,
        lid              = op.lid,
        slot_roi         = slot_roi,
        background_frame = op.background_frame,
        cancel_event     = op.cancel_event,
        socketio         = socketio,
        client_id        = op.client_id,
    )