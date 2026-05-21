# ============================================================
# FILE: back_end/slot_monitor/tools/roi_calibration.py
# ============================================================
"""
ROI Calibration Tool
====================
Runs at server startup to let the operator verify and adjust the slot
ROI rectangles for BOTH cameras before monitoring begins.

Calibration files are saved PER BOX using the box slug, e.g.:
  rois_bottom_year_1.json   (bottom camera, box whose slug is "year_1")
  rois_top_year_1.json      (top camera,    box whose slug is "year_1")

This lets multiple cabinets share the same tools/ directory without
overwriting each other's calibration.

Two separate frozen-frame editors are shown one after the other:
  1. Bottom camera  (camera index 1)  → rois_bottom_{slug}.json
  2. Top-down camera (camera index 2)  → rois_top_{slug}.json

Controls
────────────────────────────────────────────────────────────
  LEFT-CLICK inside any box           → select that box
                                        (if boxes overlap, repeated clicks
                                         cycle through all boxes at that point
                                         in z-order, deepest first)
  LEFT-DRAG from inside selected box  → move it
  LEFT-DRAG from bottom-right corner  → resize it

  RIGHT-DRAG (anywhere on screen)     → move the CURRENTLY SELECTED box
                                        without needing to touch it
                                        (useful when boxes overlap)

  E / right arrow                     → select next lid
  A / left arrow                      → select previous lid
  R                                   → reset all ROIs to equal grid
  X / Enter                           → confirm and move to next camera
  Esc                                 → abort calibration (exits program)

The selected ROI is highlighted in red; all others are green.
The lid number is printed above each box.
"""

import cv2
import json
import logging
import os
import sys
from typing import List, Tuple, Optional

import numpy as np

logger = logging.getLogger(__name__)

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def _roi_paths(box_slug: str):
    """Return (bottom_path, top_path) for a given box slug."""
    bottom = os.path.join(_TOOLS_DIR, f"rois_bottom_{box_slug}.json")
    top    = os.path.join(_TOOLS_DIR, f"rois_top_{box_slug}.json")
    return bottom, top

_RESIZE_MARGIN = 12
_LABEL_OFFSET  = 18
_FONT          = cv2.FONT_HERSHEY_SIMPLEX
_FONT_SCALE    = 0.55
_FONT_THICK    = 1
_COLOR_SEL     = (0,   0,   255)
_COLOR_OK      = (0,   200,  0)
_COLOR_TEXT    = (255, 255, 255)
_BORDER        = 2


# ══════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════

def _capture_frame(camera_index: int, warmup: int = 20) -> Optional[np.ndarray]:
    # Pick the backend that matches this camera's role.
    if camera_index == _CC.BOTTOM_CAM_INDEX:
        backend = _CC.resolve_backend(_CC.BOTTOM_CAM_BACKEND)
    elif camera_index == _CC.TOP_CAM_INDEX:
        backend = _CC.resolve_backend(_CC.TOP_CAM_BACKEND)
    else:
        import cv2 as _cv2_local
        backend = _cv2_local.CAP_ANY
    cap = cv2.VideoCapture(camera_index, backend)
    if not cap.isOpened():
        logger.warning(f"[ROI Calibration] Cannot open camera {camera_index}")
        return None
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    return frame if (ret and frame is not None) else None


def _default_rois(num_lids: int, fw: int, fh: int, spacing: int = 8) -> List[List[int]]:
    cols = max(1, int(np.ceil(np.sqrt(num_lids))))
    rows = max(1, int(np.ceil(num_lids / cols)))
    cw   = (fw - spacing * (cols + 1)) // cols
    ch   = (fh - spacing * (rows + 1)) // rows
    rois: List[List[int]] = []
    for r in range(rows):
        for c in range(cols):
            if len(rois) >= num_lids:
                break
            rois.append([
                spacing + c * (cw + spacing),
                spacing + r * (ch + spacing),
                cw, ch,
            ])
    return rois


def _load_rois(filepath: str, num_lids: int, fw: int, fh: int) -> List[List[int]]:
    if os.path.exists(filepath):
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            if (isinstance(data, list) and len(data) == num_lids and
                    all(isinstance(r, list) and len(r) == 4 for r in data)):
                return [[int(v) for v in r] for r in data]
            logger.info(
                f"[ROI Calibration] {filepath}: expected {num_lids} entries, "
                f"got {len(data) if isinstance(data, list) else '?'}. Regenerating."
            )
        except Exception as e:
            logger.warning(f"[ROI Calibration] Failed to load {filepath}: {e}. Regenerating.")
    return _default_rois(num_lids, fw, fh)


def _save_rois(filepath: str, rois: List[List[int]]) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w") as f:
        json.dump(rois, f, indent=2)
    logger.info(f"[ROI Calibration] Saved {len(rois)} ROIs to {filepath}")


def _clamp(roi: List[int], fw: int, fh: int) -> List[int]:
    x, y, w, h = roi
    w = max(10, w);  h = max(10, h)
    x = max(0, min(x, fw - w))
    y = max(0, min(y, fh - h))
    return [x, y, w, h]


def _boxes_at(mx: int, my: int, rois: List[List[int]]) -> List[int]:
    """Return indices of all boxes that contain (mx, my), outermost first."""
    return [i for i, (x, y, w, h) in enumerate(rois)
            if x <= mx <= x + w and y <= my <= y + h]


def _near_resize_handle(mx: int, my: int, roi: List[int]) -> bool:
    x, y, w, h = roi
    return (x + w - _RESIZE_MARGIN <= mx <= x + w + _RESIZE_MARGIN and
            y + h - _RESIZE_MARGIN <= my <= y + h + _RESIZE_MARGIN)


# ══════════════════════════════════════════════════════════
# Editor
# ══════════════════════════════════════════════════════════

class _ROIEditor:
    """
    Frozen-frame ROI editor for one camera.

    Left-click selection logic when boxes overlap
    ──────────────────────────────────────────────
    Each click at a point where N boxes overlap cycles through them.
    We track _click_pos and _click_cycle_idx: if the next click lands
    within a small radius of the last click position we advance the
    cycle index instead of restarting from 0.  Moving the mouse more
    than _CYCLE_RADIUS pixels resets the cycle so the operator can
    start a fresh selection elsewhere.
    """

    _CYCLE_RADIUS = 8   # px — how close two clicks must be to count as cycling

    def __init__(self, title: str, frame: np.ndarray,
                 rois: List[List[int]], num_lids: int):
        self._title    = title
        self._base     = frame.copy()
        self._rois     = [list(r) for r in rois]
        self._num_lids = num_lids
        self._fh, self._fw = frame.shape[:2]

        self._current  = 0        # selected lid index

        # Left-drag state
        self._ldrag     = None    # ("move"|"resize", start_mx, start_my, orig_roi)

        # Right-drag state — always moves the current box from any position
        self._rdrag     = None    # (start_mx, start_my, orig_roi)

        # Overlap-cycle state
        self._last_click_pos: Optional[Tuple[int, int]] = None
        self._cycle_candidates: List[int] = []
        self._cycle_idx: int = 0

    # ── Public ────────────────────────────────────────────

    def run(self) -> List[List[int]]:
        cv2.namedWindow(self._title, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(self._title, min(self._fw, 1280), min(self._fh, 720))
        cv2.setMouseCallback(self._title, self._mouse_cb)
        self._redraw()

        while True:
            key = cv2.waitKey(30) & 0xFF
            if key in (ord('x'), ord('X'), 13):          # confirm
                break
            elif key == 27:                               # abort
                cv2.destroyWindow(self._title)
                logger.warning("[ROI Calibration] Aborted by user (Esc).")
                sys.exit(1)
            elif key in (ord('e'), ord('E'), 83, 0xFF & ord('>')):
                self._select((self._current + 1) % self._num_lids)
            elif key in (ord('a'), ord('A'), 81, 0xFF & ord('<')):
                self._select((self._current - 1) % self._num_lids)
            elif key in (ord('r'), ord('R')):
                self._rois = _default_rois(self._num_lids, self._fw, self._fh)
                self._select(0)

        cv2.destroyWindow(self._title)
        return [list(r) for r in self._rois]

    def _select(self, idx: int) -> None:
        self._current = idx % self._num_lids
        self._last_click_pos   = None
        self._cycle_candidates = []
        self._cycle_idx        = 0
        self._redraw()

    # ── Mouse callback ────────────────────────────────────

    def _mouse_cb(self, event: int, mx: int, my: int, flags: int, *_) -> None:
        # ── LEFT BUTTON DOWN ──────────────────────────────
        if event == cv2.EVENT_LBUTTONDOWN:
            self._on_left_down(mx, my)

        # ── LEFT DRAG ─────────────────────────────────────
        elif event == cv2.EVENT_MOUSEMOVE and self._ldrag is not None:
            self._apply_left_drag(mx, my)
            self._redraw()

        # ── LEFT BUTTON UP ────────────────────────────────
        elif event == cv2.EVENT_LBUTTONUP:
            self._ldrag = None

        # ── RIGHT BUTTON DOWN ─────────────────────────────
        elif event == cv2.EVENT_RBUTTONDOWN:
            # Start a right-drag that will move the current box
            # from wherever the mouse is — no need to be inside the box.
            self._rdrag = (mx, my, list(self._rois[self._current]))

        # ── RIGHT DRAG ────────────────────────────────────
        elif event == cv2.EVENT_MOUSEMOVE and self._rdrag is not None:
            sx, sy, orig = self._rdrag
            dx, dy = mx - sx, my - sy
            x0, y0, w0, h0 = orig
            self._rois[self._current] = _clamp(
                [x0 + dx, y0 + dy, w0, h0], self._fw, self._fh
            )
            self._redraw()

        # ── RIGHT BUTTON UP ───────────────────────────────
        elif event == cv2.EVENT_RBUTTONUP:
            self._rdrag = None

    def _on_left_down(self, mx: int, my: int) -> None:
        """
        Selection + drag initiation on left mouse button down.

        Resize handle takes absolute priority over everything else —
        if the cursor is near the corner of the currently selected box,
        start a resize drag immediately.

        Otherwise determine which box(es) contain the click point:
          • If this click is 'near' the previous click (within _CYCLE_RADIUS px),
            advance through the same candidate list (overlap cycling).
          • If the click is somewhere new, build a fresh candidate list.
        The candidate list is ordered so that the currently selected box
        is checked last — clicking in an overlap first picks any OTHER box,
        giving the operator a way to reach boxes underneath without needing
        to use E/A keys.

        After selecting, start a move drag for the selected box only if the
        cursor is actually inside it (standard UX expectation for left-drag).
        """
        # Priority 1: resize handle of CURRENT box
        if _near_resize_handle(mx, my, self._rois[self._current]):
            self._ldrag = ("resize", mx, my, list(self._rois[self._current]))
            return

        # Build or advance candidate list
        if (self._last_click_pos is None or
                abs(mx - self._last_click_pos[0]) > self._CYCLE_RADIUS or
                abs(my - self._last_click_pos[1]) > self._CYCLE_RADIUS):
            # Fresh click — build new candidate list
            hits = _boxes_at(mx, my, self._rois)
            if not hits:
                # Click in empty space — deselect cycling but keep current
                self._last_click_pos   = None
                self._cycle_candidates = []
                self._cycle_idx        = 0
                return
            # Put currently selected box LAST so first click picks something else
            if self._current in hits:
                hits.remove(self._current)
                hits.append(self._current)
            self._cycle_candidates = hits
            self._cycle_idx        = 0
        else:
            # Same location — advance cycle
            self._cycle_idx = (self._cycle_idx + 1) % len(self._cycle_candidates)

        self._last_click_pos = (mx, my)
        self._current = self._cycle_candidates[self._cycle_idx]
        self._redraw()

        # Start move drag only if cursor is inside the selected box
        x, y, w, h = self._rois[self._current]
        if x <= mx <= x + w and y <= my <= y + h:
            self._ldrag = ("move", mx, my, list(self._rois[self._current]))

    def _apply_left_drag(self, mx: int, my: int) -> None:
        mode, sx, sy, orig = self._ldrag
        dx, dy = mx - sx, my - sy
        x0, y0, w0, h0 = orig
        if mode == "move":
            new = [x0 + dx, y0 + dy, w0, h0]
        else:
            new = [x0, y0, max(10, w0 + dx), max(10, h0 + dy)]
        self._rois[self._current] = _clamp(new, self._fw, self._fh)

    # ── Drawing ───────────────────────────────────────────

    def _redraw(self) -> None:
        canvas = self._base.copy()

        for i, (x, y, w, h) in enumerate(self._rois):
            is_sel = (i == self._current)
            color  = _COLOR_SEL if is_sel else _COLOR_OK
            thick  = _BORDER + (1 if is_sel else 0)

            # Box outline
            cv2.rectangle(canvas, (x, y), (x + w, y + h), color, thick)

            # Resize handle (filled square at bottom-right)
            cv2.rectangle(
                canvas,
                (x + w - _RESIZE_MARGIN, y + h - _RESIZE_MARGIN),
                (x + w, y + h),
                color, cv2.FILLED,
            )

            # Lid label with dark background for readability
            label = f"LID {i}"
            (tw, th), _ = cv2.getTextSize(label, _FONT, _FONT_SCALE, _FONT_THICK)
            ty = max(y - 4, _LABEL_OFFSET)
            cv2.rectangle(
                canvas,
                (x - 1, ty - th - 3), (x + tw + 2, ty + 3),
                (0, 0, 0), cv2.FILLED,
            )
            cv2.putText(
                canvas, label, (x, ty),
                _FONT, _FONT_SCALE,
                _COLOR_SEL if is_sel else _COLOR_TEXT,
                _FONT_THICK, cv2.LINE_AA,
            )

        # HUD — instructions
        overlap_hint = ""
        if self._cycle_candidates and len(self._cycle_candidates) > 1:
            overlap_hint = (
                f"  [OVERLAP: {len(self._cycle_candidates)} boxes here — "
                f"click again to cycle]"
            )

        lines = [
            "L-click: select / move   L-drag corner: resize   R-drag (anywhere): move selected",
            "E / → : next lid         A / ← : previous lid   R: reset grid",
            f"X / Enter: confirm       Esc: abort{overlap_hint}",
            f"Selected: LID {self._current} of {self._num_lids - 1}",
        ]
        for j, line in enumerate(lines):
            yy = 20 + j * 20
            cv2.putText(
                canvas, line, (8, yy),
                _FONT, 0.44,
                (220, 220, 0) if j < 3 else (100, 220, 255),
                1, cv2.LINE_AA,
            )

        cv2.imshow(self._title, canvas)


# ══════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════
#from back_end.camera_manager import cam_mgr
from Backup.back_end.config import CameraConfig as _CC

def run_calibration(num_lids: int, box_slug: str = "") -> None:
    """
    Show two sequential frozen-frame ROI editors and save results.

    Args:
        num_lids:  Total number of storage slots for this box.
        box_slug:  Box identifier (PHONEBOX_BOX_SLUG).  Used to derive
                   per-box ROI filenames, e.g. rois_bottom_year_1.json.
                   Falls back to ServerConfig.BOX_SLUG when empty.
    """
    if num_lids < 1:
        logger.warning("[ROI Calibration] num_lids < 1 — skipping.")
        return

    if not box_slug:
        from Backup.back_end.config import ServerConfig as _SVC
        box_slug = _SVC.BOX_SLUG or "box_1"

    roi_file_bottom, roi_file_top = _roi_paths(box_slug)
    logger.info(
        f"[ROI Calibration] Box slug={box_slug!r}  "
        f"bottom={os.path.basename(roi_file_bottom)}  "
        f"top={os.path.basename(roi_file_top)}"
    )

    cameras = [
        {"index": _CC.BOTTOM_CAM_INDEX, "name": f"BOTTOM CAMERA [{box_slug}]", "file": roi_file_bottom},
        {"index": _CC.TOP_CAM_INDEX,    "name": f"TOP-DOWN CAMERA [{box_slug}]", "file": roi_file_top},
    ]

    for cam in cameras:
        logger.info(f"[ROI Calibration] Capturing frame from camera {cam['index']}…")
        frame = _capture_frame(cam["index"])

        if frame is None:
            logger.error(
                f"[ROI Calibration] Cannot open camera {cam['index']}. "
                f"Using existing or default ROI file."
            )
            fh, fw = 720, 1280
            rois = _load_rois(cam["file"], num_lids, fw, fh)
            _save_rois(cam["file"], rois)
            continue

        fh, fw = frame.shape[:2]
        rois = _load_rois(cam["file"], num_lids, fw, fh)

        logger.info(
            f"[ROI Calibration] Editing {cam['name']} "
            f"({fw}×{fh}, {num_lids} lids). Press X/Enter to confirm."
        )

        editor = _ROIEditor(
            title=cam["name"],
            frame=frame,
            rois=rois,
            num_lids=num_lids,
        )
        confirmed = editor.run()
        confirmed = [_clamp(r, fw, fh) for r in confirmed]
        _save_rois(cam["file"], confirmed)

    logger.info(f"[ROI Calibration] Complete for box {box_slug!r}.")


# ══════════════════════════════════════════════════════════
# DB helper — resolve num_lids for a box slug
# ══════════════════════════════════════════════════════════

def _query_num_lids(box_slug: str) -> Optional[int]:
    """
    Query the database for the number of locations belonging to box_slug.
    Returns None if the DB is unreachable or the slug is not found.
    """
    try:
        from Backup.back_end.Database.db import get_conn, put_conn
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Resolve slug → box_id
                cur.execute(
                    "SELECT box_id FROM boxes WHERE box_slug = %s;",
                    (box_slug,),
                )
                row = cur.fetchone()
                if not row:
                    logger.warning(
                        f"[ROI Calibration] Box slug {box_slug!r} not found in "
                        "boxes table — is the slug correct?"
                    )
                    return None
                box_id = row[0]
                # Count locations for this box
                cur.execute(
                    "SELECT COUNT(*) FROM locations WHERE box_id = %s;",
                    (box_id,),
                )
                count_row = cur.fetchone()
                count = int(count_row[0]) if count_row else 0
                if count == 0:
                    logger.warning(
                        f"[ROI Calibration] No locations found for box_id={box_id} "
                        f"(slug={box_slug!r}). Seed the locations table first."
                    )
                    return None
                return count
        finally:
            put_conn(conn)
    except Exception as exc:
        logger.warning(f"[ROI Calibration] DB query failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════
# Standalone  (python roi_calibration.py [num_lids])
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    from Backup.back_end.config import ServerConfig as _SVC
    _slug = os.getenv("PHONEBOX_BOX_SLUG", "") or _SVC.BOX_SLUG or "box_1"

    if len(sys.argv) > 1:
        # Explicit override wins — useful for testing without DB
        n = int(sys.argv[1])
        print(f"Running standalone ROI calibration: box={_slug!r}, {n} lids (CLI override).")
    else:
        # Query the DB for the real slot count for this box
        n = _query_num_lids(_slug)
        if n is None:
            n = _SVC.FALLBACK_NUM_LIDS
            print(
                f"⚠  Could not resolve slot count from DB for box={_slug!r}.\n"
                f"   Falling back to FALLBACK_NUM_LIDS={n}.\n"
                f"   To fix: ensure PHONEBOX_BOX_SLUG is set and the boxes/locations "
                f"tables are seeded."
            )
        else:
            print(f"Running standalone ROI calibration: box={_slug!r}, {n} lids (from DB).")

    print(f"  Override: python roi_calibration.py <num_lids>")
    print(f"  Override box: set PHONEBOX_BOX_SLUG env var")
    run_calibration(n, box_slug=_slug)
