#!/usr/bin/env python3
# ============================================================
# FILE: back_end/slot_monitor/tools/embed_calibration.py
# ============================================================
"""
Slot Baseline Calibration
=========================
Used ONCE during initial setup (and whenever the camera is repositioned)
to record the visual embeddings that the slot monitor compares against at
runtime.

This tool works exclusively with the BOTTOM camera (index 1).
ROI positions are read from rois_bottom_{slug}.json written by
roi_calibration.py, where {slug} is the PHONEBOX_BOX_SLUG env var
(or "box_1" as default).  Run roi_calibration.py first if that file
does not yet exist.

Options
───────
  1. Calibrate all slots   — records a fresh baseline for every lid
  2. Recalibrate specific  — re-records selected lids only
  3. Verify calibration    — compares current camera view to saved baselines
  4. Exit

Usage
─────
  python -m back_end.slot_monitor.tools.embed_calibration
  # or
  python back_end/slot_monitor/tools/embed_calibration.py
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from back_end.config import CameraConfig as _CC, CalibrationConfig as _CAL
import cv2
import numpy as np

from back_end.slot_monitor.slot_embed import compute_embedding, embedding_distance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────
_TOOLS_DIR = Path(__file__).parent


def _resolve_box_slug() -> str:
    """Resolve the active box slug from env → config → fallback."""
    slug = os.getenv("PHONEBOX_BOX_SLUG", "")
    if not slug:
        try:
            from back_end.config import ServerConfig as _SVC
            slug = getattr(_SVC, "BOX_SLUG", "") or ""
        except Exception:
            pass
    return slug or "box_1"


BOX_SLUG        = _resolve_box_slug()
ROI_FILE_BOTTOM = _TOOLS_DIR / f"rois_bottom_{BOX_SLUG}.json"


def _init_box_id(box_slug: str) -> int:
    """
    Resolve box_slug → box_id from the DB and call set_box_id() so that
    all SlotMonitorDB operations in this process use the correct box.

    This is the same call that server_main makes at startup.
    Without it, _BOX_ID stays at its default of 1 and every baseline
    written by calibrate_all_slots() goes to box 1.

    Returns the resolved box_id, or 1 if the DB is unavailable (with a
    loud warning so the operator knows to fix their setup before proceeding).
    """
    try:
        from back_end.Database.db import get_conn, put_conn
        from back_end.slot_monitor.db_interface import set_box_id
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT box_id FROM boxes WHERE box_slug = %s;",
                    (box_slug,),
                )
                row = cur.fetchone()
                if not row:
                    raise RuntimeError(
                        f"Box slug {box_slug!r} not found in boxes table. "
                        "Seed the boxes table or set PHONEBOX_BOX_SLUG correctly."
                    )
                box_id = int(row[0])
                set_box_id(box_id)
                logger.info(
                    f"[EmbedCalibration] box_slug={box_slug!r} → box_id={box_id} — "
                    "SlotMonitorDB scoped to this box."
                )
                return box_id
        finally:
            put_conn(conn)
    except Exception as exc:
        logger.error(
            f"[EmbedCalibration] Could not resolve box_id for slug={box_slug!r}: {exc}\n"
            "  All baselines will be written with box_id=1 (WRONG). Fix this before proceeding!"
        )
        return 1


# Resolve and wire up box identity immediately at module load so every
# SlotMonitorDB call in this file uses the correct box_id.
BOX_ID = _init_box_id(BOX_SLUG)


# ── Camera ─────────────────────────────────────────────────
CAMERA_ID     = _CC.BOTTOM_CAM_INDEX
CAMERA_WIDTH  = _CAL.EMBED_CAM_WIDTH
CAMERA_HEIGHT = _CAL.EMBED_CAM_HEIGHT


# ══════════════════════════════════════════════════════════
# ROI loading
# ══════════════════════════════════════════════════════════

def load_rois_from_calibration() -> Dict[int, Tuple[int, int, int, int]]:
    """
    Load bottom-camera ROIs from rois_bottom_{slug}.json
    (written by roi_calibration.py).

    Raises:
        FileNotFoundError: if the ROI file does not exist.
        ValueError: if the file is malformed.
    """
    if not ROI_FILE_BOTTOM.exists():
        raise FileNotFoundError(
            f"ROI file not found: {ROI_FILE_BOTTOM}\n"
            f"Run roi_calibration.py with PHONEBOX_BOX_SLUG={BOX_SLUG!r} first."
        )

    with open(ROI_FILE_BOTTOM, "r") as f:
        data = json.load(f)

    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"{ROI_FILE_BOTTOM} is empty or malformed.")

    rois: Dict[int, Tuple[int, int, int, int]] = {}
    for i, entry in enumerate(data):
        if not (isinstance(entry, list) and len(entry) == 4):
            raise ValueError(f"ROI entry {i} is not a [x, y, w, h] list: {entry}")
        rois[i] = tuple(int(v) for v in entry)  # type: ignore[assignment]

    logger.info(f"Loaded {len(rois)} ROIs from {ROI_FILE_BOTTOM} (box={BOX_SLUG!r})")
    return rois


# ══════════════════════════════════════════════════════════
# Camera frame buffer
# ══════════════════════════════════════════════════════════

class _FrameBuffer:
    """
    Background thread that continuously captures frames from one camera.
    get_frame() always returns the latest frame without blocking.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._frame:  Optional[np.ndarray] = None
        self._event   = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self, camera_id: int, width: int, height: int) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            args=(camera_id, width, height),
            daemon=True,
            name="CalibCam",
        )
        self._thread.start()

        logger.info("Waiting for first frame…")
        if not self._event.wait(timeout=8.0):
            self._running = False
            raise RuntimeError(
                f"Camera {camera_id} did not produce a frame within 8 s. "
                "Check the camera connection and index."
            )
        logger.info("Camera ready.")

    def _loop(self, camera_id: int, width: int, height: int) -> None:
        cap = cv2.VideoCapture(camera_id, _CC.resolve_backend(_CC.BOTTOM_CAM_BACKEND))
        if not cap.isOpened():
            logger.error(f"Cannot open camera {camera_id}")
            return

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_AUTOFOCUS,    1)

        for _ in range(20):      # discard auto-exposure warmup frames
            cap.read()

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        logger.info(f"Camera {camera_id}  {actual_w}×{actual_h}")

        while self._running:
            ret, frame = cap.read()
            if ret and frame is not None:
                with self._lock:
                    self._frame = frame
                self._event.set()
            time.sleep(0.01)

        cap.release()

    def get_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame.copy() if self._frame is not None else None

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)


# ══════════════════════════════════════════════════════════
# Embedding helper
# ══════════════════════════════════════════════════════════

def _compute_embedding_for_lid(
    buf: _FrameBuffer,
    lid: int,
    roi: Tuple[int, int, int, int],
    n_samples: int = 5,
    interval: float = 0.2,
) -> np.ndarray:
    """
    Capture n_samples frames, compute an embedding from each, average and
    normalise.  Returns a float32 1-D array.
    """
    x, y, w, h = roi
    embeddings: List[np.ndarray] = []

    for i in range(n_samples):
        frame = buf.get_frame()
        if frame is None:
            raise RuntimeError("No frame available from camera.")

        roi_img = frame[y : y + h, x : x + w]
        if roi_img.size == 0:
            raise ValueError(f"Empty ROI for lid {lid}: {roi}")

        embeddings.append(compute_embedding(roi_img))

        if i < n_samples - 1:
            time.sleep(interval)

    avg = np.mean(embeddings, axis=0)
    norm = np.linalg.norm(avg)
    if norm < 1e-8:
        raise ValueError(f"Zero-norm embedding for lid {lid} — check ROI covers the slot.")
    avg = (avg / norm).astype(np.float32)
    return avg


# ══════════════════════════════════════════════════════════
# Calibration actions
# ══════════════════════════════════════════════════════════

def calibrate_all_slots(
    buf: _FrameBuffer,
    db,
    rois: Dict[int, Tuple[int, int, int, int]],
) -> None:
    """Record a baseline for every lid."""
    logger.info("=" * 70)
    logger.info("FULL SYSTEM CALIBRATION")
    logger.info("=" * 70)

    occupied_slots = db.fetch_occupied_slots()
    occupied_lids  = {lid: pid for lid, pid in occupied_slots}

    logger.info(f"Slots to calibrate : {len(rois)}")
    logger.info(f"Occupied           : {len(occupied_lids)}")
    logger.info(f"Empty              : {len(rois) - len(occupied_lids)}")

    print()
    print("Please ensure:")
    print("  • All phones are in their correct slots")
    print("  • No hands or obstructions in front of camera")
    print("  • Lighting is at normal operating conditions")
    print()

    if input("Ready to begin calibration? (yes/no): ").strip().lower() != "yes":
        logger.info("Calibration cancelled.")
        return

    logger.info("Starting in 3 s…")
    time.sleep(3)

    ok = failed = 0

    for lid in sorted(rois):
        pid    = occupied_lids.get(lid)
        status = "OCCUPIED" if pid else "EMPTY"
        logger.info(f"  Slot {lid:3d}  {status}" + (f"  PID={pid}" if pid else ""))

        try:
            emb = _compute_embedding_for_lid(buf, lid, rois[lid])
            db.save_baseline(lid, emb)
            logger.info(f"         ✓ saved")
            ok += 1
        except Exception as e:
            logger.error(f"         ✗ {e}")
            failed += 1

        time.sleep(0.1)

    print()
    logger.info("=" * 70)
    logger.info("CALIBRATION COMPLETE")
    logger.info(f"  Calibrated : {ok}")
    logger.info(f"  Failed     : {failed}")
    logger.info(f"  Success    : {ok / len(rois) * 100:.1f}%")
    if failed:
        logger.warning(f"  {failed} slot(s) failed — check camera view for those ROIs.")
    else:
        logger.info("  All slots calibrated successfully.")
    logger.info("=" * 70)


def recalibrate_specific_slots(
    buf: _FrameBuffer,
    db,
    rois: Dict[int, Tuple[int, int, int, int]],
    lids: List[int],
) -> None:
    """Re-record baselines for a specific subset of lids."""
    logger.info("=" * 70)
    logger.info("SELECTIVE RECALIBRATION")
    logger.info(f"Lids: {lids}")
    logger.info("=" * 70)

    occupied_lids = {lid: pid for lid, pid in db.fetch_occupied_slots()}

    for lid in lids:
        if lid not in rois:
            logger.warning(f"  Lid {lid} not in ROI file — skipping.")
            continue

        pid    = occupied_lids.get(lid)
        status = "OCCUPIED" if pid else "EMPTY"
        print()
        print(f"Recalibrating slot {lid}  [{status}]" + (f"  PID={pid}" if pid else ""))
        print("Ensure slot is in its correct state, then press Enter…")
        input()

        try:
            emb = _compute_embedding_for_lid(buf, lid, rois[lid])
            db.save_baseline(lid, emb)
            logger.info(f"  Slot {lid}: ✓ baseline updated")
        except Exception as e:
            logger.error(f"  Slot {lid}: ✗ {e}")


def verify_calibration(
    buf: _FrameBuffer,
    db,
    rois: Dict[int, Tuple[int, int, int, int]],
) -> None:
    """Compare current camera view to stored baselines and report distances."""
    logger.info("=" * 70)
    logger.info("CALIBRATION VERIFICATION")
    logger.info("=" * 70)

    baselines = db.fetch_all_baselines()
    logger.info(f"Slots in ROI file  : {len(rois)}")
    logger.info(f"Slots with baseline: {len(baselines)}")

    missing = set(rois) - set(baselines)
    if missing:
        logger.warning(f"  No baseline for lids: {sorted(missing)}")

    print()
    high: List[Tuple[int, float]] = []

    for lid in sorted(baselines):
        if lid not in rois:
            logger.warning(f"  Lid {lid} has a baseline but no ROI — skipping.")
            continue
        try:
            frame = buf.get_frame()
            if frame is None:
                raise RuntimeError("No frame.")
            x, y, w, h = rois[lid]
            roi_img = frame[y : y + h, x : x + w]
            emb  = compute_embedding(roi_img)
            dist = embedding_distance(emb, baselines[lid])

            mark = "✓" if dist < 0.05 else ("⚠" if dist < 0.15 else "✗")
            logger.info(f"  Slot {lid:3d}  dist={dist:.4f}  {mark}")

            if dist >= 0.15:
                high.append((lid, dist))
        except Exception as e:
            logger.error(f"  Slot {lid:3d}  ERROR: {e}")

    print()
    if high:
        logger.warning(f"  {len(high)} slot(s) with high distance — consider recalibrating:")
        for lid, dist in high:
            logger.warning(f"    Slot {lid}: {dist:.4f}")
    else:
        logger.info("  All slots verified — distances within normal range.")
    logger.info("=" * 70)


# ══════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("SLOT BASELINE CALIBRATION")
    print("=" * 70)
    print()
    print(f"  Box slug   : {BOX_SLUG!r}  (set PHONEBOX_BOX_SLUG to change)")
    print(f"  ROI source : {ROI_FILE_BOTTOM}")
    print( "  Camera     : bottom camera (index 1)")
    print()
    print("  1. Calibrate all slots   (initial setup)")
    print("  2. Recalibrate specific  (maintenance)")
    print("  3. Verify calibration")
    print("  4. Exit")
    print()

    choice = input("Choice (1-4): ").strip()
    if choice == "4":
        raise SystemExit(0)

    # ── Shared initialisation ─────────────────────────────
    buf = _FrameBuffer()
    try:
        rois = load_rois_from_calibration()
    except (FileNotFoundError, ValueError) as e:
        print(f"\n✗ {e}")
        raise SystemExit(1)

    # ── Validate ROI count against DB ─────────────────────
    # Catch the common mistake of using an ROI file from a different box
    # or a previous calibration run with the wrong slot count.
    try:
        from back_end.Database.db import get_conn, put_conn as _put_conn
        _conn = get_conn()
        try:
            with _conn.cursor() as _cur:
                _cur.execute(
                    "SELECT box_id FROM boxes WHERE box_slug = %s;", (BOX_SLUG,)
                )
                _row = _cur.fetchone()
                if _row:
                    _cur.execute(
                        "SELECT COUNT(*) FROM locations WHERE box_id = %s;",
                        (_row[0],),
                    )
                    _cnt = int(_cur.fetchone()[0])
                    if _cnt > 0 and _cnt != len(rois):
                        print(
                            f"\n⚠  WARNING: ROI file has {len(rois)} entries but "
                            f"the DB has {_cnt} locations for box={BOX_SLUG!r}.\n"
                            f"   Run roi_calibration.py first to regenerate the ROI file "
                            f"with {_cnt} slots.\n"
                        )
                        if input("Continue anyway? (yes/no): ").strip().lower() != "yes":
                            raise SystemExit(0)
                else:
                    print(
                        f"\n⚠  Box slug {BOX_SLUG!r} not found in boxes table.\n"
                        f"   Check PHONEBOX_BOX_SLUG and ensure the boxes table is seeded.\n"
                    )
        finally:
            _put_conn(_conn)
    except SystemExit:
        raise
    except Exception as _exc:
        print(f"  (DB validation skipped: {_exc})")

    try:
        buf.start(CAMERA_ID, CAMERA_WIDTH, CAMERA_HEIGHT)
    except RuntimeError as e:
        print(f"\n✗ {e}")
        raise SystemExit(1)

    from back_end.slot_monitor.db_interface import SlotMonitorDB
    db = SlotMonitorDB()

    # ── Dispatch ──────────────────────────────────────────
    try:
        if choice == "1":
            calibrate_all_slots(buf, db, rois)

        elif choice == "2":
            raw = input("Slot IDs to recalibrate (comma-separated, e.g. 0,2,5): ")
            try:
                lids = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError:
                print("✗ Invalid input — enter integers separated by commas.")
                raise SystemExit(1)
            recalibrate_specific_slots(buf, db, rois, lids)

        elif choice == "3":
            verify_calibration(buf, db, rois)

        else:
            print("✗ Invalid choice.")

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        buf.stop()