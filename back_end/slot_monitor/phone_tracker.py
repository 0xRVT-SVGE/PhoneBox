# ============================================================
# FILE: back_end/slot_monitor/phone_tracker.py
# ============================================================
"""
PhoneTracker — frame-difference detection + CSRT tracking on the top camera.

Used by ops_handler (deposit / verify) and admin_ops_handler (resolution).

Detection + tracking pipeline
──────────────────────────────
  Phase 1 — Motion detection
    Compares each new frame against a captured background using frame
    differencing.  Scanning is restricted to the target ROI (+margin)
    so movements elsewhere in the frame are ignored.  When a contour
    large enough to be a phone is found, the bounding box is returned
    and Phase 2 begins.

  Phase 2 — CSRT tracking
    OpenCV CSRT tracker follows the phone frame by frame.  After each
    update the current bbox is tested against the target ROI with IoU.
    When IoU >= overlap_threshold for stable_frames consecutive frames,
    on_confirmed() is fired and the tracker exits cleanly.

Overlay architecture
─────────────────────
  The tracker registers a _tracker_overlay on top_camera for its
  live bounding box.  The caller supplies a _context_overlay (slot
  highlights, grid) separately via top_camera.set_context_overlay().
  Both are cleared when tracking ends.

Public API
───────────
  PhoneTracker(...)     Create instance (does not start tracking)
  tracker.start()       Spawn daemon thread — returns immediately
  tracker.stop()        Request abort — joins thread with 2 s timeout
  tracker.is_running()  True while background thread is alive

Callbacks (all called from tracker thread, not the main thread)
────────────────────────────────────────────────────────────────
  on_detected(bbox)           Phone appeared in frame, CSRT initialised
  on_progress(bbox, overlap)  Each frame: current bbox + IoU vs target
  on_confirmed()              Phone stable in target ROI — success
  on_timeout()                Timed out before confirmation
  on_failed(reason: str)      Camera error, CSRT lost, etc.

Context overlay factories
──────────────────────────
  make_operation_context_overlay(...)   Slot grid + source/target highlights
  make_staging_context_overlay(...)     Staging zones with occupancy colours
"""

import cv2
import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Tunable parameters ────────────────────────────────────────────────────────

OVERLAP_THRESHOLD  = 0.50   # IoU fraction needed to count as "in target"
STABLE_FRAMES      = 10     # consecutive overlapping frames → confirmed
DETECT_TIMEOUT     = 15.0   # seconds to find the phone before giving up
TRACK_TIMEOUT      = 55.0   # total seconds the tracker is allowed to run
TARGET_FPS         = 15     # tracker loop rate (frames/s)
MIN_MOTION_AREA    = 600    # px² — smaller blobs are ignored as noise
LOST_TRACK_LIMIT   = 8      # consecutive lost frames before tracker gives up
DETECTION_MARGIN   = 40     # px of padding around target ROI during detection


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _iou(a: Tuple[int, int, int, int],
         b: Tuple[int, int, int, int]) -> float:
    """IoU of two (x, y, w, h) rectangles. Returns value in [0, 1]."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b

    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter == 0:
        return 0.0

    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _detect_motion_in_roi(
    background: np.ndarray,
    current:    np.ndarray,
    roi:        Tuple[int, int, int, int],
    margin:     int = DETECTION_MARGIN,
    min_area:   int = MIN_MOTION_AREA,
) -> Optional[Tuple[int, int, int, int]]:
    """
    Frame-difference motion detector restricted to roi (+margin).

    Returns a bounding box (x, y, w, h) in full-frame coordinates,
    or None if no significant motion is found.
    """
    fh, fw = current.shape[:2]
    rx, ry, rw, rh = roi

    # Expand search window by margin (clamped to frame bounds)
    sx = max(0, rx - margin)
    sy = max(0, ry - margin)
    ex = min(fw, rx + rw + margin)
    ey = min(fh, ry + rh + margin)

    bg_crop  = background[sy:ey, sx:ex]
    cur_crop = current[sy:ey, sx:ex]

    diff  = cv2.absdiff(bg_crop, cur_crop)
    gray  = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    blur  = cv2.GaussianBlur(gray, (21, 21), 0)
    _, thresh = cv2.threshold(blur, 18, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(
        thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        return None

    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < min_area:
        return None

    bx, by, bw, bh = cv2.boundingRect(largest)
    # Convert crop-local coords back to full-frame coords
    return (sx + bx, sy + by, bw, bh)


# ── PhoneTracker ──────────────────────────────────────────────────────────────

class PhoneTracker:
    """
    Tracks a phone on the top-down camera and auto-confirms placement.

    Thread safety
    ─────────────
    All callbacks are invoked from the tracker thread.  If you need to
    emit SocketIO events from them, use socketio.emit() directly (it is
    thread-safe) or schedule via the Flask-SocketIO background task API.

    At most one PhoneTracker should be active at a time per top_camera.
    ops_handler ensures this by always cancelling the current operation
    (via cancel_event) before starting a new one.
    """

    def __init__(
        self,
        target_roi:        Tuple[int, int, int, int],
        cancel_event:      threading.Event,
        background_frame:  Optional[np.ndarray]    = None,
        on_detected:       Optional[Callable]      = None,
        on_progress:       Optional[Callable]      = None,
        on_confirmed:      Optional[Callable]      = None,
        on_timeout:        Optional[Callable]      = None,
        on_failed:         Optional[Callable]      = None,
        stable_frames:     int                     = STABLE_FRAMES,
        overlap_threshold: float                   = OVERLAP_THRESHOLD,
        detect_timeout:    float                   = DETECT_TIMEOUT,
        track_timeout:     float                   = TRACK_TIMEOUT,
    ):
        self._target_roi        = target_roi
        self._cancel_event      = cancel_event
        self._background        = background_frame

        # Callbacks — default to no-ops so callers only need to pass what they use
        self._on_detected       = on_detected   or (lambda bbox: None)
        self._on_progress       = on_progress   or (lambda bbox, overlap: None)
        self._on_confirmed      = on_confirmed  or (lambda: None)
        self._on_timeout        = on_timeout    or (lambda: None)
        self._on_failed         = on_failed     or (lambda reason: None)

        self._stable_frames     = stable_frames
        self._overlap_threshold = overlap_threshold
        self._detect_timeout    = detect_timeout
        self._track_timeout     = track_timeout

        self._thread: Optional[threading.Thread] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn tracker daemon thread. Returns immediately."""
        if self._running:
            logger.warning("[PhoneTracker] start() called while already running")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="PhoneTracker"
        )
        self._thread.start()
        logger.info("[PhoneTracker] Started")

    def stop(self) -> None:
        """Signal abort and wait up to 2 s for the thread to exit."""
        self._running = False
        self._cancel_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("[PhoneTracker] Stopped")

    def is_running(self) -> bool:
        return self._running and (
            self._thread is not None and self._thread.is_alive()
        )

    # ── Main ──────────────────────────────────────────────────────────────────

    def _run(self) -> None:
        from back_end.slot_monitor.camera.top_camera import top_camera

        try:
            init_bbox = self._phase_detect(top_camera)
            if init_bbox is None:
                return  # callbacks already fired in the phase

            confirmed = self._phase_track(top_camera, init_bbox)
            if confirmed:
                logger.info("[PhoneTracker] ✓ Placement confirmed")
                self._on_confirmed()

        except Exception as e:
            logger.error(f"[PhoneTracker] Unexpected error: {e}", exc_info=True)
            self._on_failed(f"internal_error: {e}")
        finally:
            from back_end.slot_monitor.camera.top_camera import top_camera
            top_camera.clear_tracker_overlay()
            self._running = False

    # ── Phase 1: motion detection ─────────────────────────────────────────────

    def _phase_detect(
        self, cam
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Wait for the phone to appear in (or near) the target ROI.

        We restrict detection to the target ROI + margin so the user's
        hand or other motion outside the target area doesn't trigger a
        false start.

        Returns the initial bounding box in full-frame coordinates,
        or None if detection timed out / was cancelled.
        """
        deadline      = time.time() + self._detect_timeout
        frame_interval = 1.0 / TARGET_FPS
        background     = self._background   # may be None → captured lazily

        while time.time() < deadline and self._running:
            if self._cancel_event.is_set():
                return None

            t0 = time.time()

            if not cam.wait_for_frame(timeout=0.08):
                continue

            frame = cam.get_frame()
            cam.clear_frame_event()
            if frame is None:
                continue

            # Capture lazy background from first real frame
            if background is None:
                background = frame.copy()
                self._background = background
                sleep = frame_interval - (time.time() - t0)
                if sleep > 0:
                    time.sleep(sleep)
                continue

            bbox = _detect_motion_in_roi(
                background, frame, self._target_roi
            )
            if bbox is not None:
                logger.info(f"[PhoneTracker] Motion detected: bbox={bbox}")
                self._on_detected(bbox)
                return bbox

            sleep = frame_interval - (time.time() - t0)
            if sleep > 0:
                time.sleep(sleep)

        # Timed out or cancelled
        if self._running and not self._cancel_event.is_set():
            logger.warning("[PhoneTracker] Detection timeout — no motion seen")
            self._on_timeout()
        return None

    # ── Phase 2: CSRT tracking ────────────────────────────────────────────────

    def _phase_track(
        self, cam, init_bbox: Tuple[int, int, int, int]
    ) -> bool:
        """
        CSRT-track the phone and confirm when it is stable inside the target ROI.

        Returns True if placement was confirmed, False otherwise.
        Fires on_timeout or on_failed (never both) before returning False.
        """
        from back_end.slot_monitor.camera.top_camera import top_camera as _tc

        # Initialise tracker on the latest available frame
        frame = cam.get_frame()
        if frame is None:
            self._on_failed("no_frame_for_csrt_init")
            return False

        tracker = cv2.TrackerCSRT_create()
        tracker.init(frame, init_bbox)

        stable_count = 0
        lost_count   = 0
        deadline      = time.time() + self._track_timeout
        frame_interval = 1.0 / TARGET_FPS

        while time.time() < deadline and self._running:
            if self._cancel_event.is_set():
                return False

            t0 = time.time()

            if not cam.wait_for_frame(timeout=0.08):
                continue

            frame = cam.get_frame()
            cam.clear_frame_event()
            if frame is None:
                continue

            ok, raw_bbox = tracker.update(frame)

            if not ok:
                lost_count += 1
                stable_count = 0

                if lost_count >= LOST_TRACK_LIMIT:
                    logger.warning("[PhoneTracker] Track lost — too many failures")
                    _tc.set_tracker_overlay(_draw_lost_overlay)
                    self._on_failed("tracking_lost")
                    return False

                _tc.set_tracker_overlay(_draw_lost_overlay)

                sleep = frame_interval - (time.time() - t0)
                if sleep > 0:
                    time.sleep(sleep)
                continue

            lost_count = 0
            bbox = tuple(int(v) for v in raw_bbox)
            overlap = _iou(bbox, self._target_roi)

            # Build overlay with current state (closure captures snapshot)
            _tc.set_tracker_overlay(
                _make_bbox_overlay(bbox, overlap, stable_count, self._stable_frames)
            )

            self._on_progress(bbox, overlap)

            if overlap >= self._overlap_threshold:
                stable_count += 1
                if stable_count >= self._stable_frames:
                    _tc.clear_tracker_overlay()
                    return True
            else:
                stable_count = max(0, stable_count - 1)   # decay, not instant reset

            sleep = frame_interval - (time.time() - t0)
            if sleep > 0:
                time.sleep(sleep)

        if self._running and not self._cancel_event.is_set():
            self._on_timeout()
        return False


# ── Overlay draw functions ────────────────────────────────────────────────────

def _draw_lost_overlay(frame: np.ndarray) -> None:
    cv2.putText(
        frame, "Re-locating phone...", (16, 40),
        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 60, 255), 2, cv2.LINE_AA,
    )


def _make_bbox_overlay(
    bbox:         Tuple[int, int, int, int],
    overlap:      float,
    stable_count: int,
    stable_total: int,
) -> Callable[[np.ndarray], None]:
    """Return a draw callable that captures the current tracking state."""
    _b = bbox
    _ov = overlap
    _sc = stable_count
    _st = stable_total

    def _draw(frame: np.ndarray) -> None:
        x, y, w, h = _b
        pct = min(100, int(_sc / _st * 100)) if _st > 0 else 0

        # Colour transitions green as confirmation approaches
        g = int(50 + 205 * pct / 100)
        r = int(255 * (1 - pct / 100))
        color = (0, g, r)

        # Bounding box
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)

        # Overlap percentage label
        label = f"IoU {int(_ov * 100)}%  stable {pct}%"
        (tw, th), _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
        )
        ly = max(y - 6, th + 4)
        cv2.rectangle(
            frame, (x - 1, ly - th - 3), (x + tw + 2, ly + 3),
            (0, 0, 0), cv2.FILLED,
        )
        cv2.putText(
            frame, label, (x, ly),
            cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA,
        )

        # Horizontal progress bar beneath the bbox
        bar_y = y + h + 5
        bar_w = w
        filled = int(bar_w * pct / 100)
        cv2.rectangle(frame, (x, bar_y), (x + bar_w, bar_y + 6), (40, 40, 40), cv2.FILLED)
        if filled > 0:
            cv2.rectangle(frame, (x, bar_y), (x + filled, bar_y + 6), color, cv2.FILLED)
        cv2.rectangle(frame, (x, bar_y), (x + bar_w, bar_y + 6), color, 1)

    return _draw


# ── Context overlay factories ─────────────────────────────────────────────────

def make_operation_context_overlay(
    slot_rois:  Dict[int, Tuple[int, int, int, int]],
    target_lid: Optional[int] = None,
    source_lid: Optional[int] = None,
) -> Callable[[np.ndarray], None]:
    """
    Context overlay for deposit / withdraw / verify operations.

    Draws:
      • Dim grey grid for all non-participating slots (reference)
      • Yellow outline for the source slot (phone being removed from here)
      • Pulsing green fill + corner brackets for the target slot
    """
    _rois   = dict(slot_rois)
    _target = target_lid
    _source = source_lid
    _t0     = time.time()

    def _draw(frame: np.ndarray) -> None:
        # 1. Dim all non-participating slots
        for lid, (x, y, w, h) in _rois.items():
            if lid in (_target, _source):
                continue
            cv2.rectangle(frame, (x, y), (x + w, y + h), (55, 55, 55), 1)
            cv2.putText(
                frame, str(lid), (x + 3, y + 13),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (70, 70, 70), 1, cv2.LINE_AA,
            )

        # 2. Source slot — yellow
        if _source is not None and _source in _rois:
            x, y, w, h = _rois[_source]
            cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 200, 200), 2)
            _label_box(frame, f"FROM {_source}", x, y, (0, 200, 200))

        # 3. Target slot — pulsing green + corner brackets
        if _target is not None and _target in _rois:
            x, y, w, h = _rois[_target]
            pulse = int(100 + 100 * np.sin((time.time() - _t0) * 4.0))
            color = (0, 180 + pulse // 3, 0)

            # Semi-transparent fill
            ov = frame.copy()
            cv2.rectangle(ov, (x, y), (x + w, y + h), color, cv2.FILLED)
            cv2.addWeighted(ov, 0.12, frame, 0.88, 0, frame)

            # Outline
            cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)

            # Corner bracket decorations
            sz = min(w, h) // 5
            for (cx, cy, dx, dy) in [
                (x,     y,     1,  1),
                (x + w, y,    -1,  1),
                (x,     y + h, 1, -1),
                (x + w, y + h, -1, -1),
            ]:
                cv2.line(frame, (cx, cy), (cx + dx * sz, cy), color, 3)
                cv2.line(frame, (cx, cy), (cx, cy + dy * sz), color, 3)

            _label_box(frame, f"SLOT {_target}", x, y, color)

    return _draw


def make_staging_context_overlay(
    staging_rois: List[Tuple[int, int, int, int]],
    staged_pids:  Dict[str, int],
    slot_rois:    Optional[Dict[int, Tuple[int, int, int, int]]] = None,
) -> Callable[[np.ndarray], None]:
    """
    Admin staging overlay with per-zone occupancy colours.

    staged_pids: {pid: zone_index}  — which zone each staged phone is in.
    slot_rois:   optional reference grid (drawn dim).
    """
    _staging = list(staging_rois)
    _staged  = dict(staged_pids)
    _slots   = dict(slot_rois) if slot_rois else {}

    ZONE_COLORS: List[Tuple[int, int, int]] = [
        (255, 140,   0),   # zone 0 — amber
        (  0, 140, 255),   # zone 1 — blue
    ]
    ZONE_NAMES = ["STAGING 1", "STAGING 2"]

    def _draw(frame: np.ndarray) -> None:
        # Reference slot grid (very dim)
        for lid, (x, y, w, h) in _slots.items():
            cv2.rectangle(frame, (x, y), (x + w, y + h), (35, 35, 35), 1)

        # Build per-zone occupancy count
        counts = [0] * len(_staging)
        for pid, zone_idx in _staged.items():
            if 0 <= zone_idx < len(counts):
                counts[zone_idx] += 1

        for i, (x, y, w, h) in enumerate(_staging):
            color = ZONE_COLORS[i % len(ZONE_COLORS)]
            occupied = counts[i] > 0

            if occupied:
                ov = frame.copy()
                cv2.rectangle(ov, (x, y), (x + w, y + h), color, cv2.FILLED)
                cv2.addWeighted(ov, 0.18, frame, 0.82, 0, frame)
                cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
                label = f"{ZONE_NAMES[i]}  ({counts[i]})"
            else:
                dim = tuple(max(0, c // 2) for c in color)
                cv2.rectangle(frame, (x, y), (x + w, y + h), dim, 1)
                label = ZONE_NAMES[i]

            _label_box(frame, label, x, y, color if occupied else
                       tuple(max(0, c // 2) for c in color))

    return _draw


# ── Utility ───────────────────────────────────────────────────────────────────

def _label_box(
    frame: np.ndarray,
    text:  str,
    x:     int,
    y:     int,
    color: Tuple[int, int, int],
    font_scale: float = 0.45,
) -> None:
    """Draw a text label with a black background just above (x, y)."""
    (tw, th), _ = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1
    )
    ly = max(y - 4, th + 4)
    cv2.rectangle(
        frame, (x - 1, ly - th - 3), (x + tw + 2, ly + 3),
        (0, 0, 0), cv2.FILLED,
    )
    cv2.putText(
        frame, text, (x, ly),
        cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, 1, cv2.LINE_AA,
    )