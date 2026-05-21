# ============================================================
# FILE: back_end/slot_monitor/admin/staging_calibration.py
# ============================================================
"""
Staging Zone Calibration Tool
==============================
Standalone tool for the admin to visually position the two physical
staging zones on the box lid as seen by the top-down camera.

What it shows
─────────────
  ● Slot ROIs (from rois_top.json)   — GREEN, read-only reference.
    These are the slot destination zones used by the phone tracker.
    They cannot be selected or moved — they are shown only so the
    admin knows where slots are when positioning staging zones.

  ● Staging zone ROIs               — BLUE (zone 1) and ORANGE (zone 2).
    These are what the tool edits. They are saved to staging_rois.json.

Controls
─────────
  TAB / 1 / 2              → switch selected staging zone
  Left-drag inside box     → move selected zone
  Left-drag bottom-right ▪ → resize selected zone
  Right-drag anywhere      → move selected zone without touching it
  R                        → reset both staging zones to defaults
  X / Enter                → save and exit
  Esc                      → exit without saving

Output
──────
  back_end/slot_monitor/admin/staging_rois.json

  Format: list of exactly 2 entries, each [x, y, w, h] (0-based pixels).
  Example:
    [[120, 40, 180, 160], [420, 40, 180, 160]]

  Entry 0 → STAGING 1, Entry 1 → STAGING 2.

Usage
─────
  python -m back_end.slot_monitor.admin.staging_calibration
  # or
  python back_end/slot_monitor/admin/staging_calibration.py
"""

import cv2
import json
import logging
import os
import sys
from typing import List, Optional, Tuple
from Backup.back_end.config import CameraConfig as _CC, CalibrationConfig as _CAL

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── File paths ────────────────────────────────────────────
_ADMIN_DIR       = os.path.dirname(os.path.abspath(__file__))
_TOOLS_DIR       = os.path.normpath(os.path.join(_ADMIN_DIR, "..", "tools"))
STAGING_ROI_FILE = os.path.join(_ADMIN_DIR, "staging_rois.json")
SLOT_ROI_FILE    = os.path.join(_TOOLS_DIR, "rois_top.json")

# ── Camera ────────────────────────────────────────────────
TOP_CAMERA_INDEX = _CC.TOP_CAM_INDEX
WARMUP_FRAMES    = _CAL.TOP_CAM_WARMUP   # discard auto-exposure warmup

# ── Visual constants ──────────────────────────────────────
_RESIZE_MARGIN = 12    # px — corner grab zone
_FONT          = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE    = 0.52
_FONT_THICK    = 1

# Slot ROIs — read-only reference
_COL_SLOT      = (0, 220, 0)       # green
_COL_SLOT_TXT  = (0, 200, 0)

# Staging zones — editable
_ZONE_COLORS = [
    (255, 140, 0),    # zone 0 → STAGING 1 — blue
    (0, 140, 255),    # zone 1 → STAGING 2 — orange
]
_ZONE_NAMES = ["STAGING 1", "STAGING 2"]
_ZONE_SEL_BOOST = 40   # brighten selected zone colour

# HUD
_COL_HUD  = (220, 220, 0)
_COL_SEL  = (100, 220, 255)


# ══════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════

def _capture_frame(camera_index: int, warmup: int) -> Optional[np.ndarray]:
    cap = cv2.VideoCapture(camera_index, _CC.resolve_backend(_CC.TOP_CAM_BACKEND))
    if not cap.isOpened():
        logger.error(f"Cannot open camera {camera_index}")
        return None
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    return frame if (ret and frame is not None) else None


def _load_slot_rois(filepath: str) -> List[Tuple[int, int, int, int]]:
    """Load slot ROIs for reference display. Returns [] on any failure."""
    if not os.path.exists(filepath):
        logger.warning(
            f"[Staging] rois_top.json not found at {filepath} — "
            "slot reference overlays will not be shown."
        )
        return []
    try:
        with open(filepath) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [tuple(int(v) for v in r) for r in data]
    except Exception as e:
        logger.warning(f"[Staging] Failed to load slot ROIs: {e}")
    return []


def _default_staging_rois(fw: int, fh: int) -> List[List[int]]:
    """
    Two equal-width staging zones side by side, centred vertically,
    occupying roughly the middle third of the frame height.
    """
    w  = fw // 5
    h  = fh // 4
    y  = (fh - h) // 2
    x1 = fw // 8
    x2 = fw - fw // 8 - w
    return [[x1, y, w, h], [x2, y, w, h]]


def _load_staging_rois(filepath: str, fw: int, fh: int) -> List[List[int]]:
    if os.path.exists(filepath):
        try:
            with open(filepath) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) == 2:
                return [[int(v) for v in r] for r in data]
            logger.info(
                "[Staging] staging_rois.json had wrong entry count — "
                "using defaults."
            )
        except Exception as e:
            logger.warning(f"[Staging] Failed to load {filepath}: {e}")
    return _default_staging_rois(fw, fh)


def _save_staging_rois(filepath: str, rois: List[List[int]]) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(rois, f, indent=2)
    logger.info(f"[Staging] Saved staging ROIs to {filepath}")


def _clamp(roi: List[int], fw: int, fh: int) -> List[int]:
    x, y, w, h = roi
    w = max(10, w);  h = max(10, h)
    x = max(0, min(x, fw - w))
    y = max(0, min(y, fh - h))
    return [x, y, w, h]


def _brighten(color: Tuple[int, int, int], amount: int) -> Tuple[int, int, int]:
    return tuple(min(255, c + amount) for c in color)


def _near_resize_handle(mx: int, my: int, roi: List[int]) -> bool:
    x, y, w, h = roi
    return (x + w - _RESIZE_MARGIN <= mx <= x + w + _RESIZE_MARGIN and
            y + h - _RESIZE_MARGIN <= my <= y + h + _RESIZE_MARGIN)


# ══════════════════════════════════════════════════════════
# Editor
# ══════════════════════════════════════════════════════════

class _StagingEditor:
    """
    Two-zone frozen-frame staging ROI editor.

    Left-drag inside selected box  → move
    Left-drag bottom-right corner  → resize
    Right-drag anywhere            → move selected zone without touching it
    TAB / 1 / 2                    → switch zone
    R                              → reset to defaults
    X / Enter                      → confirm
    Esc                            → abort (no save)
    """

    TITLE = "Staging Zone Calibration — X/Enter: save   Esc: cancel"

    def __init__(
        self,
        frame: np.ndarray,
        staging_rois: List[List[int]],
        slot_rois: List[Tuple[int, int, int, int]],
    ):
        self._base        = frame.copy()
        self._staging     = [list(r) for r in staging_rois]  # mutable
        self._slots       = slot_rois                          # read-only
        self._fh, self._fw = frame.shape[:2]

        self._sel  = 0        # index of selected staging zone (0 or 1)
        self._ldrag = None    # ("move"|"resize", sx, sy, orig_roi)
        self._rdrag = None    # (sx, sy, orig_roi)

    # ── Public ────────────────────────────────────────────

    def run(self) -> Optional[List[List[int]]]:
        """
        Block until user confirms (X/Enter) or cancels (Esc).
        Returns the edited staging ROI list, or None on cancel.
        """
        cv2.namedWindow(self.TITLE, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self.TITLE, min(self._fw, 1280), min(self._fh, 720))
        cv2.setMouseCallback(self.TITLE, self._mouse_cb)
        self._redraw()

        while True:
            key = cv2.waitKey(30) & 0xFF

            if key in (ord('x'), ord('X'), 13):          # confirm
                cv2.destroyWindow(self.TITLE)
                return [list(r) for r in self._staging]

            elif key == 27:                               # Esc — cancel
                cv2.destroyWindow(self.TITLE)
                return None

            elif key == 9:                               # TAB
                self._sel = 1 - self._sel
                self._redraw()

            elif key == ord('1'):
                self._sel = 0
                self._redraw()

            elif key == ord('2'):
                self._sel = 1
                self._redraw()

            elif key in (ord('r'), ord('R')):
                self._staging = _default_staging_rois(self._fw, self._fh)
                self._redraw()

    # ── Mouse ─────────────────────────────────────────────

    def _mouse_cb(self, event: int, mx: int, my: int, *_) -> None:
        if event == cv2.EVENT_LBUTTONDOWN:
            self._on_left_down(mx, my)

        elif event == cv2.EVENT_MOUSEMOVE:
            moved = False
            if self._ldrag is not None:
                self._apply_left_drag(mx, my)
                moved = True
            if self._rdrag is not None:
                sx, sy, orig = self._rdrag
                x0, y0, w0, h0 = orig
                self._staging[self._sel] = _clamp(
                    [x0 + mx - sx, y0 + my - sy, w0, h0],
                    self._fw, self._fh,
                )
                moved = True
            if moved:
                self._redraw()

        elif event == cv2.EVENT_LBUTTONUP:
            self._ldrag = None

        elif event == cv2.EVENT_RBUTTONDOWN:
            # Start right-drag on the currently selected zone
            orig = list(self._staging[self._sel])
            self._rdrag = (mx, my, orig)

        elif event == cv2.EVENT_RBUTTONUP:
            self._rdrag = None

    def _on_left_down(self, mx: int, my: int) -> None:
        """
        Priority:
          1. Resize handle of currently selected zone
          2. Click inside a staging zone → select it + start move drag
          3. Click outside both zones → no action (slot ROIs are read-only)
        """
        # 1. Resize handle of selected zone
        if _near_resize_handle(mx, my, self._staging[self._sel]):
            self._ldrag = ("resize", mx, my, list(self._staging[self._sel]))
            return

        # 2. Click inside any staging zone
        for i, (x, y, w, h) in enumerate(self._staging):
            if x <= mx <= x + w and y <= my <= y + h:
                self._sel = i
                self._ldrag = ("move", mx, my, list(self._staging[i]))
                self._redraw()
                return

    def _apply_left_drag(self, mx: int, my: int) -> None:
        mode, sx, sy, orig = self._ldrag
        dx, dy = mx - sx, my - sy
        x0, y0, w0, h0 = orig
        if mode == "move":
            new = [x0 + dx, y0 + dy, w0, h0]
        else:
            new = [x0, y0, max(10, w0 + dx), max(10, h0 + dy)]
        self._staging[self._sel] = _clamp(new, self._fw, self._fh)

    # ── Drawing ───────────────────────────────────────────

    def _redraw(self) -> None:
        canvas = self._base.copy()

        # ── Slot ROIs — read-only reference (green) ───────
        for i, roi in enumerate(self._slots):
            x, y, w, h = roi
            cv2.rectangle(canvas, (x, y), (x + w, y + h), _COL_SLOT, 1)
            label = f"slot {i + 1}"
            (tw, th), _ = cv2.getTextSize(label, _FONT, _FONT_SCALE * 0.85, 1)
            # Label inside the box, top-left corner
            lx, ly = x + 4, y + th + 4
            cv2.rectangle(
                canvas,
                (lx - 2, ly - th - 2), (lx + tw + 2, ly + 2),
                (0, 0, 0), cv2.FILLED,
            )
            cv2.putText(
                canvas, label, (lx, ly),
                _FONT, _FONT_SCALE * 0.85, _COL_SLOT_TXT, 1, cv2.LINE_AA,
            )

        # ── Staging zones — editable ──────────────────────
        for i, (x, y, w, h) in enumerate(self._staging):
            is_sel = (i == self._sel)
            base_color = _ZONE_COLORS[i]
            color = _brighten(base_color, _ZONE_SEL_BOOST) if is_sel else base_color
            thick = 2 if is_sel else 1

            # Box
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, thick)

            # Resize handle (filled square, bottom-right)
            hx, hy = x + w, y + h
            cv2.rectangle(
                canvas,
                (hx - _RESIZE_MARGIN, hy - _RESIZE_MARGIN),
                (hx, hy),
                color, cv2.FILLED,
            )

            # Zone label inside the box, centred
            name = _ZONE_NAMES[i]
            (tw, th), _ = cv2.getTextSize(name, _FONT, _FONT_SCALE, _FONT_THICK)
            lx = x + (w - tw) // 2
            ly = y + (h + th) // 2
            # Dark background for readability
            pad = 3
            cv2.rectangle(
                canvas,
                (lx - pad, ly - th - pad),
                (lx + tw + pad, ly + pad),
                (0, 0, 0), cv2.FILLED,
            )
            cv2.putText(
                canvas, name, (lx, ly),
                _FONT, _FONT_SCALE, color, _FONT_THICK, cv2.LINE_AA,
            )

            # "SELECTED" badge above box when this zone is active
            if is_sel:
                badge = "SELECTED"
                (bw, bh), _ = cv2.getTextSize(badge, _FONT, 0.42, 1)
                bx = x
                by = max(y - 6, bh + 4)
                cv2.rectangle(
                    canvas,
                    (bx - 1, by - bh - 2), (bx + bw + 2, by + 2),
                    (0, 0, 0), cv2.FILLED,
                )
                cv2.putText(
                    canvas, badge, (bx, by),
                    _FONT, 0.42, color, 1, cv2.LINE_AA,
                )

        # ── HUD ───────────────────────────────────────────
        sel_name  = _ZONE_NAMES[self._sel]
        sel_color = _brighten(_ZONE_COLORS[self._sel], _ZONE_SEL_BOOST)

        lines = [
            "GREEN boxes = slot ROIs (read-only reference from rois_top.json)",
            f"Editing: {sel_name}   |   TAB / 1 / 2 : switch zone   R : reset",
            "L-drag inside: move   L-drag corner: resize   R-drag anywhere: move",
            "X / Enter : save & exit        Esc : cancel (no save)",
        ]
        for j, line in enumerate(lines):
            yy = 20 + j * 19
            color = sel_color if j == 1 else _COL_HUD
            cv2.putText(
                canvas, line, (8, yy),
                _FONT, 0.43, color, 1, cv2.LINE_AA,
            )

        # Coordinates readout for selected zone
        x, y, w, h = self._staging[self._sel]
        coord_text = f"{sel_name}  x={x} y={y} w={w} h={h}"
        cv2.putText(
            canvas, coord_text,
            (8, self._fh - 10),
            _FONT, 0.44, sel_color, 1, cv2.LINE_AA,
        )

        cv2.imshow(self.TITLE, canvas)


# ══════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════

def run() -> None:
    logger.info("[Staging] Capturing top camera frame…")
    frame = _capture_frame(TOP_CAMERA_INDEX, warmup=WARMUP_FRAMES)

    if frame is None:
        logger.error(
            f"[Staging] Cannot open camera {TOP_CAMERA_INDEX}. "
            "Make sure the top-down camera is connected and not in use."
        )
        sys.exit(1)

    fh, fw = frame.shape[:2]
    logger.info(f"[Staging] Frame captured ({fw}×{fh})")

    slot_rois    = _load_slot_rois(SLOT_ROI_FILE)
    staging_rois = _load_staging_rois(STAGING_ROI_FILE, fw, fh)

    if slot_rois:
        logger.info(f"[Staging] Showing {len(slot_rois)} slot ROIs as reference")
    else:
        logger.info("[Staging] No slot ROIs to show (run roi_calibration.py first)")

    logger.info(
        "[Staging] Editor open — position the two staging zones, "
        "then press X or Enter to save."
    )

    editor = _StagingEditor(
        frame=frame,
        staging_rois=staging_rois,
        slot_rois=slot_rois,
    )
    result = editor.run()

    if result is None:
        logger.info("[Staging] Cancelled — no changes saved.")
        sys.exit(0)

    result = [_clamp(r, fw, fh) for r in result]
    _save_staging_rois(STAGING_ROI_FILE, result)
    logger.info("[Staging] Done.")


if __name__ == "__main__":
    run()