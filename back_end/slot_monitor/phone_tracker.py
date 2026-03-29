# ============================================================
# FILE: back_end/slot_monitor/phone_tracker.py
# ============================================================
"""
Phone Tracker — deposit-only motion verification.

After QR scan succeeds and the target slot is confirmed, this module
tracks the phone across the top-down camera until it lands in the correct
slot.  It enforces two security constraints:

  1. QR must remain visible the entire journey.
     If the QR disappears for more than QR_LOST_TIMEOUT seconds while
     the phone is NOT yet intersecting the target slot ROI, the operation
     is aborted.  This detects substitution attacks (different phone
     placed) and obstruction attacks (hand covering the QR mid-motion).

  2. Phone must not leave the camera frame.
     If the tracker loses the bounding box or the box goes fully off-screen,
     the operation fails.

  3. Placement timeout.
     If the phone does not reach the slot ROI within PLACEMENT_TIMEOUT
     seconds the operation fails.

Success condition:
  Bounding box centre is inside the target slot ROI.  At this point the
  phone is face-down in the slot so the QR naturally disappears.

Algorithm:
  Phase 1 — Detection (frame differencing vs background captured at op start):
    Absolute-difference background subtraction finds the first foreground
    blob large enough to be a phone.  This is faster and more predictable
    than MOG2 for the "one object enters an otherwise static scene" case.

  Phase 2 — Tracking (OpenCV CSRT):
    CSRT is initialised from the detection bbox and tracks the phone
    across subsequent frames.  It handles partial occlusion and scale
    change better than KCF at ~30 fps for a single object.

  Phase 3 — QR check (pyzbar) runs in parallel on every frame.

Annotations:
  The tracker draws its bounding box, the target slot ROI, and a status
  line on every top-camera frame via top_camera.set_tracker_overlay().
  Because top_camera feeds the AdminVideoTrack WebRTC stream, the
  annotations appear in real time for any admin watching.
"""

import json
import logging
import os
import threading
import time
from typing import Callable, Optional, Tuple

import cv2
import numpy as np
from pyzbar.pyzbar import decode

logger = logging.getLogger(__name__)

# ── Timing constants ─────────────────────────────────────
DETECT_TIMEOUT    = 8.0    # seconds to wait for phone to enter frame
PLACEMENT_TIMEOUT = 30.0   # seconds from tracker start to successful placement
QR_LOST_TIMEOUT   = 2.0    # seconds QR may be absent before failure (outside ROI)

# ── Detection constants ───────────────────────────────────
MIN_PHONE_AREA   = 1500    # px² — minimum blob to consider as phone
DIFF_BLUR_KERNEL = 21      # Gaussian blur kernel size for frame diff
DIFF_THRESHOLD   = 25      # per-pixel diff threshold (0-255)
DILATE_ITERS     = 3       # morphological dilation iterations

# ── Annotation colours (BGR) ─────────────────────────────
_COL_BOX     = (0,  255,  0)    # green  — tracked bbox
_COL_ROI     = (0,  200, 255)   # amber  — target slot ROI
_COL_WARN    = (0,   0,  255)   # red    — QR lost warning
_COL_TEXT    = (255, 255, 255)  # white  — status text
_FONT        = cv2.FONT_HERSHEY_SIMPLEX


# ── Top-camera ROI loader ─────────────────────────────────

_top_rois_cache: Optional[dict] = None
_top_rois_lock  = threading.Lock()


def _load_top_roi(lid: int) -> Optional[Tuple[int, int, int, int]]:
    """
    Return the top-camera ROI (x,y,w,h) for a given lid, loaded from
    rois_top.json.  Cached after first load.
    """
    global _top_rois_cache
    with _top_rois_lock:
        if _top_rois_cache is None:
            tools_dir = os.path.join(os.path.dirname(__file__), "slot_monitor", "tools")
            roi_file  = os.path.normpath(os.path.join(tools_dir, "rois_top.json"))
            if not os.path.exists(roi_file):
                logger.warning(f"[Tracker] rois_top.json not found at {roi_file}")
                _top_rois_cache = {}
            else:
                try:
                    with open(roi_file) as f:
                        data = json.load(f)
                    _top_rois_cache = {
                        i: tuple(int(v) for v in r)
                        for i, r in enumerate(data)
                    }
                    logger.info(f"[Tracker] Loaded {len(_top_rois_cache)} top-cam ROIs")
                except Exception as e:
                    logger.error(f"[Tracker] Failed to load rois_top.json: {e}")
                    _top_rois_cache = {}

        return _top_rois_cache.get(lid)  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════
# PhoneTracker
# ══════════════════════════════════════════════════════════

class PhoneTracker:
    """
    Tracks a phone from QR-scan-success to slot-placement.

    Usage:
        tracker = PhoneTracker(pid, lid, slot_roi, background_frame,
                               cancel_event, socketio, client_id)
        tracker.start(on_success=..., on_failure=...)
        # returns immediately; tracking runs in a daemon thread
    """

    def __init__(
        self,
        pid:              str,
        lid:              int,
        slot_roi:         Tuple[int, int, int, int],   # (x, y, w, h) in top-cam coords
        background_frame: np.ndarray,
        cancel_event:     threading.Event,
        socketio,
        client_id:        str,
    ):
        self._pid              = pid
        self._lid              = lid
        self._slot_roi         = slot_roi
        self._background_frame = background_frame
        self._cancel_event     = cancel_event
        self._socketio         = socketio
        self._client_id        = client_id

        # Mutable state read by annotation callback (GIL makes these atomic)
        self._bbox:       Optional[Tuple[int, int, int, int]] = None
        self._qr_visible: bool = True
        self._in_roi:     bool = False

        self._on_success: Optional[Callable] = None
        self._on_failure: Optional[Callable] = None

    # ── Public ────────────────────────────────────────────

    def start(
        self,
        on_success: Callable,
        on_failure: Callable[[str], None],
    ) -> None:
        """
        Start the tracker in a background daemon thread.
        on_success() is called when the phone reaches the target slot.
        on_failure(reason) is called on any error; reason is one of:
          "qr_lost", "out_of_frame", "timeout", "detect_timeout", "cancelled"
        """
        self._on_success = on_success
        self._on_failure = on_failure
        t = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"PhoneTracker-{self._pid[:8]}-lid{self._lid}",
        )
        t.start()

    # ── Main thread ───────────────────────────────────────

    def _run(self) -> None:
        from back_end.slot_monitor.camera.top_camera import top_camera
        try:
            # Phase 1: detect phone entering frame
            logger.info(
                f"[Tracker] PID={self._pid} LID={self._lid} — "
                f"waiting for phone to enter frame (timeout={DETECT_TIMEOUT}s)"
            )
            bbox = self._detect_phone(top_camera)
            if bbox is None:
                reason = "cancelled" if self._cancel_event.is_set() else "detect_timeout"
                self._fail(reason, top_camera)
                return

            logger.info(
                f"[Tracker] Phone detected at {bbox} — "
                f"initialising CSRT tracker"
            )

            # Phase 2: init CSRT on the frame where phone was first detected
            frame = top_camera.get_frame()
            if frame is None:
                self._fail("detect_timeout", top_camera)
                return

            tracker = cv2.TrackerCSRT_create()
            tracker.init(frame, bbox)
            self._bbox = bbox

            top_camera.set_tracker_overlay(self._draw_overlay)

            # Phase 3: track + verify
            self._track(tracker, top_camera)

        except Exception as e:
            logger.error(f"[Tracker] Unexpected error: {e}", exc_info=True)
            try:
                from back_end.slot_monitor.camera.top_camera import top_camera
                top_camera.clear_tracker_overlay()
            except Exception:
                pass
            if self._on_failure:
                self._on_failure("error")

    # ── Phase 1: frame-diff detection ─────────────────────

    def _detect_phone(
        self,
        top_camera,
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Wait for a foreground blob large enough to be a phone.
        Uses absolute-difference vs the background frame captured at
        operation start — fast, deterministic, no adaptive learning.

        Returns (x, y, w, h) of the first qualifying blob, or None.
        """
        bg_gray = cv2.cvtColor(self._background_frame, cv2.COLOR_BGR2GRAY)
        bg_blur = cv2.GaussianBlur(
            bg_gray, (DIFF_BLUR_KERNEL, DIFF_BLUR_KERNEL), 0
        )

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

            diff   = cv2.absdiff(bg_blur, blur)
            _, thr = cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)
            thr    = cv2.dilate(thr, None, iterations=DILATE_ITERS)

            contours, _ = cv2.findContours(
                thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            if not contours:
                continue

            largest = max(contours, key=cv2.contourArea)
            if cv2.contourArea(largest) < MIN_PHONE_AREA:
                continue

            x, y, w, h = cv2.boundingRect(largest)
            return (x, y, w, h)

        return None

    # ── Phase 2: CSRT tracking loop ───────────────────────

    def _track(self, tracker: cv2.Tracker, top_camera) -> None:
        deadline       = time.time() + PLACEMENT_TIMEOUT
        last_qr_seen   = time.time()   # QR is visible when tracking starts
        last_emit_time = 0.0

        while time.time() < deadline:
            # Cancellation check
            if self._cancel_event.is_set():
                self._fail("cancelled", top_camera)
                return

            # Wait for next top-cam frame
            if not top_camera.wait_for_frame(timeout=0.05):
                continue
            frame = top_camera.get_frame()
            top_camera.clear_frame_event()
            if frame is None:
                continue

            fh, fw = frame.shape[:2]

            # CSRT update
            ok, raw_bbox = tracker.update(frame)
            if not ok:
                # CSRT lost the object
                self._fail("out_of_frame", top_camera)
                return

            bx, by, bw, bh = (int(v) for v in raw_bbox)

            # Out-of-frame check: bbox must have at least one pixel inside frame
            if bx + bw <= 0 or bx >= fw or by + bh <= 0 or by >= fh:
                self._fail("out_of_frame", top_camera)
                return

            self._bbox = (bx, by, bw, bh)

            # ── Slot intersection (success condition) ──────
            if self._centre_in_roi(bx, by, bw, bh):
                self._in_roi = True
                # Phone is in the slot — success
                top_camera.clear_tracker_overlay()
                logger.info(
                    f"[Tracker] PID={self._pid} reached slot {self._lid} "
                    f"bbox=({bx},{by},{bw},{bh})"
                )
                if self._on_success:
                    self._on_success()
                return

            # ── QR visibility check ────────────────────────
            qr_now = self._check_qr(frame)
            if qr_now:
                last_qr_seen   = time.time()
            self._qr_visible = qr_now

            qr_absent = time.time() - last_qr_seen
            if qr_absent > QR_LOST_TIMEOUT:
                self._fail("qr_lost", top_camera)
                return

            # ── Periodic socket update (throttled 500 ms) ──
            now = time.time()
            if now - last_emit_time > 0.5:
                self._emit_update(qr_now, False, qr_absent)
                last_emit_time = now

        self._fail("timeout", top_camera)

    # ── Helpers ───────────────────────────────────────────

    def _centre_in_roi(self, bx: int, by: int, bw: int, bh: int) -> bool:
        """True if the centre of bbox lies inside the target slot ROI."""
        cx = bx + bw // 2
        cy = by + bh // 2
        rx, ry, rw, rh = self._slot_roi
        return rx <= cx <= rx + rw and ry <= cy <= ry + rh

    def _check_qr(self, frame: np.ndarray) -> bool:
        """
        Run pyzbar on the full frame. Returns True if the known PID's
        QR code is detected anywhere in the frame.
        """
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
        logger.warning(f"[Tracker] PID={self._pid} LID={self._lid} — failed: {reason}")
        if self._on_failure:
            self._on_failure(reason)

    def _succeed(self) -> None:
        if self._on_success:
            self._on_success()

    # ── Socket ────────────────────────────────────────────

    def _emit_update(self, qr_visible: bool, in_roi: bool, qr_absent: float) -> None:
        try:
            self._socketio.emit(
                "tracking_update",
                {
                    "pid":        self._pid,
                    "lid":        self._lid,
                    "qr_visible": qr_visible,
                    "in_roi":     in_roi,
                    "qr_absent":  round(qr_absent, 1),
                },
                to=self._client_id,
                namespace="/",
            )
        except Exception as e:
            logger.debug(f"[Tracker] emit tracking_update failed: {e}")

    # ── Frame annotation ──────────────────────────────────

    def _draw_overlay(self, frame: np.ndarray) -> None:
        """
        Called by top_camera._run() on every frame before storage.
        Draws bounding box, target slot ROI, and status text.
        Reads self._bbox / self._qr_visible / self._in_roi which are
        updated by the tracking loop (GIL makes tuple assignment atomic).
        """
        # Target slot ROI
        rx, ry, rw, rh = self._slot_roi
        cv2.rectangle(frame, (rx, ry), (rx + rw, ry + rh), _COL_ROI, 2)
        cv2.putText(
            frame,
            f"Target: slot {self._lid + 1}",
            (rx, max(ry - 6, 14)),
            _FONT, 0.5, _COL_ROI, 1, cv2.LINE_AA,
        )

        # Tracked bounding box
        bbox = self._bbox
        if bbox is not None:
            bx, by, bw, bh = bbox
            colour = _COL_BOX if self._qr_visible else _COL_WARN
            cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), colour, 2)

            # QR status badge
            qr_text = "QR OK" if self._qr_visible else "QR MISSING"
            badge_colour = _COL_BOX if self._qr_visible else _COL_WARN
            (tw, th), _ = cv2.getTextSize(qr_text, _FONT, 0.45, 1)
            bby = max(by + bh + th + 4, th + 4)
            cv2.rectangle(
                frame,
                (bx, bby - th - 2), (bx + tw + 4, bby + 2),
                (0, 0, 0), cv2.FILLED,
            )
            cv2.putText(
                frame, qr_text, (bx + 2, bby),
                _FONT, 0.45, badge_colour, 1, cv2.LINE_AA,
            )

        # Status line (top of frame)
        status = "TRACKING — place phone in target slot"
        cv2.putText(
            frame, status, (8, frame.shape[0] - 10),
            _FONT, 0.5, _COL_TEXT, 1, cv2.LINE_AA,
        )


# ── Module-level convenience ──────────────────────────────

def create_tracker_for_operation(op, socketio) -> Optional["PhoneTracker"]:
    """
    Build a PhoneTracker from an Operation object.
    Returns None if the top-cam ROI for the op's lid is not available
    or if the background frame was not captured.
    """
    if op.background_frame is None:
        logger.warning(
            f"[Tracker] No background frame for op PID={op.pid} — "
            "tracker unavailable"
        )
        return None

    slot_roi = _load_top_roi(op.lid)
    if slot_roi is None:
        logger.warning(
            f"[Tracker] No top-cam ROI for lid={op.lid} — "
            "run roi_calibration.py to create rois_top.json"
        )
        return None

    return PhoneTracker(
        pid=op.pid,
        lid=op.lid,
        slot_roi=slot_roi,
        background_frame=op.background_frame,
        cancel_event=op.cancel_event,
        socketio=socketio,
        client_id=op.client_id,
    )