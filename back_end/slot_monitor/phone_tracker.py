# ============================================================
# FILE: back_end/slot_monitor/phone_tracker.py
# ============================================================
"""
Phone Tracker — deposit + verify motion verification.

Optimization vs previous version
──────────────────────────────────
_check_qr (pyzbar) previously ran on every tracking frame.
It now runs on every second frame (alternating skip).
At 20 fps this halves pyzbar CPU usage with no meaningful security
impact — a substitution attack cannot be hidden in a single skipped
frame, and QR_LOST_TIMEOUT (2 s) gives 20 frames of tolerance.

Overlay system
──────────────
Two composable callable hooks on top_camera draw on every frame:

  _context_overlay  Drawn first.
    Set by ops_handler (DVW) or admin_ops_handler (admin session).
    Shows the static session context — slot grid, source, destination,
    staging zones with occupancy colors.

  _tracker_overlay  Drawn on top of context.
    Set by PhoneTracker once CSRT is live.
    Shows only the phone bounding box and QR status badge.

Public factory functions
────────────────────────
  make_dvw_context_overlay(all_rois, source_lid, dest_lid)
  make_admin_session_overlay(all_rois, staging_rois, staged_pids,
                              source_lid, dest_lid)
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

# ── Timing constants ──────────────────────────────────────
DETECT_TIMEOUT        = 8.0
PLACEMENT_TIMEOUT     = 30.0
QR_LOST_TIMEOUT       = 2.0

# ── ROI success constants ─────────────────────────────────
# Fraction of the bounding-box area that must overlap the destination
# ROI before we enter the stabilization phase (Phase 2→3).
ROI_INTERSECT_THRESHOLD = 0.30
# Maximum time (s) the phone may stay in the ROI with QR still visible.
# If exceeded the student didn't put the phone down properly → FAIL.
STABILIZATION_TIMEOUT  = 3.0

# ── Detection constants ───────────────────────────────────
MIN_PHONE_AREA   = 1500
DIFF_BLUR_KERNEL = 21
DIFF_THRESHOLD   = 25
DILATE_ITERS     = 3

# ── QR frame-skip ─────────────────────────────────────────
# Run pyzbar on every Nth tracking frame.
# N=2 halves CPU at the cost of up to one extra frame of QR absence
# before detection — negligible at 20 fps with a 2 s tolerance window.
_QR_CHECK_EVERY_N = 2

# ── Palette (BGR) ─────────────────────────────────────────
_COL_GRID          = (60,  60,  60)
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

def _draw_dashed_line(
    frame: np.ndarray,
    x1: int, y1: int, x2: int, y2: int,
    color: Tuple,
    thickness: int = 2,
    dash: int = 12, gap: int = 7,
) -> None:
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


def _draw_dashed_rect(
    frame: np.ndarray,
    x: int, y: int, w: int, h: int,
    color: Tuple,
    thickness: int = 2,
    dash: int = 12, gap: int = 7,
) -> None:
    x2, y2 = x + w, y + h
    _draw_dashed_line(frame, x,  y,  x2,  y, color, thickness, dash, gap)
    _draw_dashed_line(frame, x2, y,  x2, y2, color, thickness, dash, gap)
    _draw_dashed_line(frame, x2, y2,  x, y2, color, thickness, dash, gap)
    _draw_dashed_line(frame,  x, y2,  x,  y, color, thickness, dash, gap)


def _fill_alpha(
    frame: np.ndarray,
    x: int, y: int, w: int, h: int,
    color: Tuple, alpha: float = 0.15,
) -> None:
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x + w, y + h), color, cv2.FILLED)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


def _corner_brackets(
    frame: np.ndarray,
    x: int, y: int, w: int, h: int,
    color: Tuple, arm: int = 20, thickness: int = 2,
) -> None:
    x2, y2 = x + w, y + h
    for (ax, ay), (bx, by), (cx, cy) in [
        ((x + arm, y),     (x, y),     (x,  y + arm)),
        ((x2 - arm, y),    (x2, y),    (x2, y + arm)),
        ((x, y2 - arm),    (x, y2),    (x + arm, y2)),
        ((x2, y2 - arm),   (x2, y2),   (x2 - arm, y2)),
    ]:
        cv2.line(frame, (ax, ay), (bx, by), color, thickness, cv2.LINE_AA)
        cv2.line(frame, (bx, by), (cx, cy), color, thickness, cv2.LINE_AA)


def _label(
    frame: np.ndarray,
    text: str, x: int, y: int,
    color: Tuple,
    scale: float = 0.44, thick: int = 1, pad: int = 3,
) -> None:
    (tw, th), _ = cv2.getTextSize(text, _FONT, scale, thick)
    fh, fw = frame.shape[:2]
    x = max(pad, min(x, fw - tw - pad - 2))
    y = max(th + pad + 2, min(y, fh - pad - 2))
    cv2.rectangle(frame, (x - pad, y - th - pad), (x + tw + pad, y + pad),
                  (0, 0, 0), cv2.FILLED)
    cv2.putText(frame, text, (x, y), _FONT, scale, color, thick, cv2.LINE_AA)


def _pulse(base: Tuple, period: float = 1.2, lo: float = 0.55, hi: float = 1.0) -> Tuple:
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
# ══════════════════════════════════════════════════════════

def make_dvw_context_overlay(
    all_rois:   Dict[int, Tuple],
    source_lid: Optional[int],
    dest_lid:   int,
) -> Callable[[np.ndarray], None]:
    """
    DVW operation context (deposit or verify).
    • Dim gray reference grid for all other slots.
    • Source slot: orange dashed rect + label (verify only).
    • Destination: yellow pulsing dashed rect + corner brackets + arrow + label.
    """
    def draw(frame: np.ndarray) -> None:
        for lid, (rx, ry, rw, rh) in all_rois.items():
            if lid in (dest_lid, source_lid):
                continue
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), _COL_GRID, 1)
            _label(frame, str(lid + 1), rx + 3, ry + 14,
                   _COL_GRID, scale=0.33, thick=1)

        if source_lid is not None and source_lid in all_rois:
            rx, ry, rw, rh = all_rois[source_lid]
            _fill_alpha(frame, rx, ry, rw, rh, _COL_SOURCE, 0.12)
            _draw_dashed_rect(frame, rx, ry, rw, rh, _COL_SOURCE, 2)
            _label(frame, f"FROM  slot {source_lid + 1}",
                   rx + 4, ry + rh - 6, _COL_SOURCE)

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
    Admin resolution session context overlay.

    • Dim gray reference grid (all slots not currently active).
    • Staging zones — dashed when empty, solid fill when occupied.
    • Source slot  — orange dashed + "FROM slot N".
    • Destination  — yellow pulsing + corner brackets + "PLACE HERE".
    • Same-slot    — orange with corner brackets + "RETURN HERE".
    """
    _staging = list(staging_rois)
    _pids    = list(staged_pids)

    def draw(frame: np.ndarray) -> None:
        skip = set()
        if source_lid is not None:
            skip.add(source_lid)
        if dest_lid is not None:
            skip.add(dest_lid)

        # 1. Background grid
        for lid, (rx, ry, rw, rh) in all_slot_rois.items():
            if lid in skip:
                continue
            cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), _COL_GRID, 1)
            _label(frame, str(lid + 1), rx + 3, ry + 14,
                   _COL_GRID, scale=0.33, thick=1)

        # 2. Staging zones
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

        # 3. Source slot
        if source_lid is not None and source_lid in all_slot_rois:
            rx, ry, rw, rh = all_slot_rois[source_lid]
            same  = (source_lid == dest_lid)
            col   = _COL_SOURCE
            _fill_alpha(frame, rx, ry, rw, rh, col, 0.11)
            _draw_dashed_rect(frame, rx, ry, rw, rh, col, 2)
            label = f"FROM  slot {source_lid + 1}" + (" \u2194 RETURN HERE" if same else "")
            _label(frame, label, rx + 4, ry + rh - 6, col)
            if same:
                _corner_brackets(frame, rx, ry, rw, rh, col,
                                  arm=min(20, rw // 4, rh // 4))

        # 4. Destination slot (skip if same as source)
        if dest_lid is not None and dest_lid in all_slot_rois and dest_lid != source_lid:
            rx, ry, rw, rh = all_slot_rois[dest_lid]
            col = _pulse(_COL_DEST_BASE)
            _fill_alpha(frame, rx, ry, rw, rh, col, 0.13)
            _draw_dashed_rect(frame, rx, ry, rw, rh, col, 2)
            _corner_brackets(frame, rx, ry, rw, rh, col,
                              arm=min(20, rw // 4, rh // 4))
            cx, cy = rx + rw // 2, ry + rh // 2
            cv2.arrowedLine(frame, (cx, cy - 16), (cx, cy + 16),
                            col, 2, cv2.LINE_AA, tipLength=0.35)
            _label(frame, f"\u25BC  SLOT {dest_lid + 1}  \u2014  PLACE HERE",
                   rx + 4, ry + rh + 17, col)

    return draw


# ══════════════════════════════════════════════════════════
# PhoneTracker
# ══════════════════════════════════════════════════════════

class PhoneTracker:
    """
    Tracks a phone from QR-scan-success to slot-placement or staging.

    Draws only the bounding box and QR status via _tracker_overlay.
    The static context (dest, source, grid, staging) is drawn by
    _context_overlay, set in ops_handler before this tracker starts.

    QR check runs every _QR_CHECK_EVERY_N frames to reduce pyzbar CPU.

    Staging zone detection
    ──────────────────────
    If staging_rois is provided, the tracker also watches those zones.
    When the phone bbox substantially overlaps a staging zone and stays
    there for STAGING_HOLD_TIME seconds, on_staged(zone_idx) is called.
    This lets the admin physically place the phone in staging without
    pressing any button — the tracker detects it automatically.
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
        self._on_staged:       Optional[Callable] = None  # on_staged(zone_idx)

    def start(
        self,
        on_success: Callable,
        on_failure: Callable,
        on_staged:  Optional[Callable] = None,
    ) -> None:
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
        """Return raw frame if available; fall back to annotated frame.
        Uses explicit None-check to avoid numpy truth-value ambiguity."""
        frame = raw_fn()
        return frame if frame is not None else fallback_fn()

    def _run(self) -> None:
        from back_end.slot_monitor.camera.top_camera import top_camera
        try:
            logger.info(
                f"[Tracker] PID={self._pid} LID={self._lid} — "
                f"detecting phone (timeout={DETECT_TIMEOUT}s)"
            )
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
        bg_gray = cv2.cvtColor(self._bg, cv2.COLOR_BGR2GRAY)
        bg_blur = cv2.GaussianBlur(bg_gray, (DIFF_BLUR_KERNEL, DIFF_BLUR_KERNEL), 0)
        deadline = time.time() + DETECT_TIMEOUT

        while time.time() < deadline:
            if self._cancel_event.is_set():
                return None
            if not top_camera.wait_for_frame(timeout=0.05):
                continue
            frame = top_camera.get_frame()
            top_camera.clear_frame_event()
            if frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            blur = cv2.GaussianBlur(gray, (DIFF_BLUR_KERNEL, DIFF_BLUR_KERNEL), 0)
            diff = cv2.absdiff(bg_blur, blur)
            _, thr = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
            thr = cv2.dilate(thr, None, iterations=DILATE_ITERS)

            contours, _ = cv2.findContours(
                thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            largest = max(contours, key=cv2.contourArea)
            if cv2.contourArea(largest) < MIN_PHONE_AREA:
                continue
            x, y, w, h = cv2.boundingRect(largest)
            return (x, y, w, h)

        return None

    def _track(self, tracker, top_camera) -> None:
        """
        Three-phase tracking loop (per Thoughts spec).

        Phase 1  — Before ROI: QR must stay visible; QR lost → FAIL qr_lost.
        Phase 2  — Entering ROI: bbox intersects ROI by ≥ ROI_INTERSECT_THRESHOLD
                   fraction; transition to Phase 3.
        Phase 3  — Stabilization: inside ROI.
                   QR disappears (face-down) -> SUCCESS.
                   Tracker lost inside ROI -> SUCCESS.
                   Tracker lost / bbox exits frame outside ROI -> FAIL.
                   STABILIZATION_TIMEOUT exceeded (QR still visible) -> FAIL.

        Staging detection (parallel):
                   If bbox substantially overlaps a staging zone for
                   STAGING_HOLD_TIME seconds -> on_staged(zone_idx) called.
        """
        STAGING_HOLD_TIME = 1.5

        deadline          = time.time() + PLACEMENT_TIMEOUT
        last_qr_seen      = time.time()
        last_emit         = 0.0
        frame_count       = 0
        in_roi            = False
        roi_entry_ts: Optional[float] = None
        staging_idx_held: int           = -1
        staging_hold_ts:  Optional[float] = None

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
            ok, raw = tracker.update(frame)

            # ── Tracker lost ───────────────────────────────────────────────
            if not ok:
                if in_roi:
                    top_camera.clear_tracker_overlay()
                    logger.info(
                        f"[Tracker] PID={self._pid} tracker lost inside ROI "
                        "-> SUCCESS TRACKER"
                    )
                    if self._on_success:
                        self._on_success()
                elif staging_hold_ts is not None:
                    top_camera.clear_tracker_overlay()
                    logger.info(
                        f"[Tracker] PID={self._pid} tracker lost in staging "
                        f"zone {staging_idx_held} -> STAGED"
                    )
                    if self._on_staged:
                        self._on_staged(staging_idx_held)
                else:
                    self._fail("out_of_frame", top_camera)
                return

            bx, by, bw, bh = (int(v) for v in raw)

            # ── Bbox fully outside frame ───────────────────────────────────
            if bx + bw <= 0 or bx >= fw or by + bh <= 0 or by >= fh:
                if in_roi:
                    top_camera.clear_tracker_overlay()
                    logger.info(
                        f"[Tracker] PID={self._pid} left frame while inside "
                        "ROI -> SUCCESS TRACKER"
                    )
                    if self._on_success:
                        self._on_success()
                else:
                    self._fail("out_of_frame", top_camera)
                return

            self._bbox = (bx, by, bw, bh)

            # ── Staging zone detection ─────────────────────────────────────
            cur_staging = self._in_which_staging(bx, by, bw, bh)
            if cur_staging >= 0:
                if cur_staging != staging_idx_held:
                    staging_idx_held = cur_staging
                    staging_hold_ts  = time.time()
                elif staging_hold_ts and (time.time() - staging_hold_ts) >= STAGING_HOLD_TIME:
                    top_camera.clear_tracker_overlay()
                    logger.info(
                        f"[Tracker] PID={self._pid} held in staging zone "
                        f"{staging_idx_held} -> STAGED"
                    )
                    if self._on_staged:
                        self._on_staged(staging_idx_held)
                    return
            else:
                staging_idx_held = -1
                staging_hold_ts  = None

            # ── Destination ROI phase transitions ─────────────────────────
            now_in_roi = self._intersects_roi(bx, by, bw, bh)
            if now_in_roi and not in_roi:
                in_roi       = True
                roi_entry_ts = time.time()
                logger.debug(f"[Tracker] PID={self._pid} entered destination ROI")
            elif not now_in_roi and in_roi:
                in_roi       = False
                roi_entry_ts = None
                logger.debug(f"[Tracker] PID={self._pid} exited destination ROI")

            # ── QR check (every Nth frame) ─────────────────────────────────
            frame_count += 1
            if frame_count % _QR_CHECK_EVERY_N == 0:
                qr_now = self._check_qr(frame)
                if qr_now:
                    last_qr_seen = time.time()
                self._qr_visible = qr_now

            qr_absent = time.time() - last_qr_seen

            # ── Phase 3 logic (inside destination ROI) ────────────────────
            if in_roi:
                if qr_absent > QR_LOST_TIMEOUT:
                    top_camera.clear_tracker_overlay()
                    logger.info(
                        f"[Tracker] PID={self._pid} QR disappeared inside "
                        "ROI -> SUCCESS QR"
                    )
                    if self._on_success:
                        self._on_success()
                    return
                if roi_entry_ts and (time.time() - roi_entry_ts) > STABILIZATION_TIMEOUT:
                    self._fail("timeout", top_camera)
                    return
            else:
                # Phase 1: before destination ROI (QR must stay visible
                # unless phone is being held over a staging zone)
                if qr_absent > QR_LOST_TIMEOUT and staging_hold_ts is None:
                    self._fail("qr_lost", top_camera)
                    return

            # ── Progress update ────────────────────────────────────────────
            now = time.time()
            if now - last_emit > 0.5:
                self._emit_update(self._qr_visible, in_roi, qr_absent)
                last_emit = now

        self._fail("timeout", top_camera)

    def _intersects_roi(self, bx: int, by: int, bw: int, bh: int) -> bool:
        """
        Returns True when the fraction of the bounding-box area that overlaps
        the destination slot ROI is >= ROI_INTERSECT_THRESHOLD (default 30%).
        """
        rx, ry, rw, rh = self._slot_roi
        ix1 = max(bx, rx);  iy1 = max(by, ry)
        ix2 = min(bx + bw, rx + rw);  iy2 = min(by + bh, ry + rh)
        if ix2 <= ix1 or iy2 <= iy1:
            return False
        inter     = (ix2 - ix1) * (iy2 - iy1)
        bbox_area = bw * bh
        return bbox_area > 0 and (inter / bbox_area) >= ROI_INTERSECT_THRESHOLD

    def _in_which_staging(self, bx: int, by: int, bw: int, bh: int) -> int:
        """
        Returns the index of the staging zone the bbox substantially overlaps,
        or -1 if the bbox is not substantially inside any staging zone.
        Uses the same ROI_INTERSECT_THRESHOLD as the destination ROI check.
        """
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
        try:
            for obj in decode(frame.copy()):
                raw = obj.data.decode("utf-8", errors="ignore").strip()
                if raw.startswith("PID:"):
                    raw = raw[4:]
                if raw.isdigit() and str(int(raw)) == self._pid:
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
        for (ax, ay), (cx, cy) in [
            ((bx + arm,    by),        (bx,    by + arm)),
            ((bx+bw-arm,   by),        (bx+bw, by + arm)),
            ((bx,          by+bh-arm), (bx+arm,    by+bh)),
            ((bx+bw,       by+bh-arm), (bx+bw-arm, by+bh)),
        ]:
            left = ax < bx + bw // 2
            ex = bx if left else bx + bw
            cv2.line(frame, (ax, ay), (ex, ay), col, 2)
            cv2.line(frame, (ex, ay), (ex, cy), col, 2)

        qr_text = "QR \u2713 visible" if self._qr_visible else "QR \u2717  KEEP VISIBLE!"
        _label(frame, qr_text, bx, by + bh + 16, col)
        _label(frame, "TRACKING \u2014 move phone to target slot",
               8, frame.shape[0] - 8, _COL_TEXT)


# ══════════════════════════════════════════════════════════
# Public convenience
# ══════════════════════════════════════════════════════════

def create_tracker_for_operation(op, socketio) -> Optional[PhoneTracker]:
    """
    Build a PhoneTracker from an Operation object.

    If op.background_frame is None (race condition — camera not yet warm),
    waits up to 300 ms for a fresh frame before giving up.
    Returns None if background frame or slot ROI is unavailable.
    """
    from back_end.slot_monitor.camera.top_camera import top_camera

    if op.background_frame is None:
        top_camera.wait_for_frame(timeout=0.3)
        op.background_frame = top_camera.get_frame()

    if op.background_frame is None:
        logger.warning(
            f"[Tracker] No background frame for PID={op.pid} lid={op.lid} "
            "— top camera may not have started"
        )
        return None

    slot_roi = _load_top_roi(op.lid)
    if slot_roi is None:
        all_keys = list(_roi_cache.keys()) if _roi_cache else "cache empty"
        logger.warning(
            f"[Tracker] No top-cam ROI for lid={op.lid} "
            f"(available: {all_keys}) — run roi_calibration.py"
        )
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