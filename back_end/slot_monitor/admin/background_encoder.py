# ============================================================
# FILE: back_end/slot_monitor/admin/background_encoder.py
# ============================================================
"""
Background video encoder singleton.

Problem solved
──────────────
When an alarm fires, the system previously spawned two threads that each
called RollingBuffer.save_to_mp4().  Decoding 900 × 1080p JPEG frames and
re-encoding to XVID takes ~30 s of CPU on modest hardware — running in
parallel with the scanner loop and slot-monitor workers, it caused visible
lag for 5-30 s after every alarm.

Solution
────────
All encode jobs (alarm clips, session pre-buffer clips, face clips) are
submitted to this singleton queue.  A single daemon worker drains the queue
sequentially, inserting a short sleep every YIELD_EVERY frames so real-time
threads (30-fps scanner, async slot workers) are never starved.

The caller returns immediately after submit(); encoding happens later,
completely transparently, in the background.

Queue overflow
──────────────
If more than MAX_QUEUE jobs pile up (burst of alarms), the oldest job is
replaced and a warning is logged.  Evidence integrity is preserved for the
most recent events.
"""

import cv2
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
from back_end.config import BgEncoderConfig as _BEC

logger = logging.getLogger(__name__)

# Edit back_end/config.py → BgEncoderConfig to change these.
_YIELD_EVERY = _BEC.YIELD_EVERY
_YIELD_SLEEP = _BEC.YIELD_SLEEP

_MAX_QUEUE = _BEC.MAX_QUEUE

_FOURCC_ORDER = [
    cv2.VideoWriter_fourcc(*name) for name in _BEC.FOURCC_ORDER
]

_EncodeJob = Tuple[
    List[Tuple[float, bytes]],   # (timestamp, jpeg_bytes) frames
    Path,                         # output path
    float,                        # fps
    Optional[Callable[[Path], None]],  # on_done callback (or None)
    bool,                         # delete_on_done flag
]


class BackgroundEncoder:
    """
    Singleton background encoder.

    Usage:
        BackgroundEncoder.instance().submit(frames, path, fps)
        BackgroundEncoder.instance().submit(frames, path, fps,
                                            callback=lambda p: log(p))
    """

    _inst: Optional["BackgroundEncoder"] = None
    _init_lock = threading.Lock()

    # ── Singleton ─────────────────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "BackgroundEncoder":
        if cls._inst is None:
            with cls._init_lock:
                if cls._inst is None:
                    cls._inst = cls()
        return cls._inst

    def __init__(self) -> None:
        self._q: queue.SimpleQueue[_EncodeJob] = queue.SimpleQueue()
        self._qsize = 0                     # approximate, updated without lock
        self._dropped = 0
        t = threading.Thread(
            target=self._worker, daemon=True, name="BgVideoEncoder"
        )
        t.start()
        logger.info("[BgEncoder] Background video encoder started")

    # ── Public API ─────────────────────────────────────────────────────────────

    def submit(
        self,
        frames: List[Tuple[float, bytes]],
        path: Path,
        fps: float,
        callback: Optional[Callable[[Path], None]] = None,
        delete_on_done: bool = False,
    ) -> bool:
        """
        Non-blocking.  Returns True if queued, False if dropped (queue full).

        Args:
            frames:         List of (timestamp, jpeg_bytes) from RollingBuffer.
            path:           Destination file path (parent dir created if needed).
            fps:            Output video frame rate.
            callback:       Called with `path` after encoding completes.
                            Useful for DB inserts that need the file to exist.
            delete_on_done: If True, delete the file after encoding + callback
                            (used when keep=False is decided after submit).
        """
        if not frames:
            return False

        if self._qsize >= _MAX_QUEUE:
            self._dropped += 1
            logger.warning(
                f"[BgEncoder] Queue full — dropping oldest job, "
                f"queuing new: {path.name}  (total dropped: {self._dropped})"
            )
            # Drain one slot so the new job goes in (drop-oldest policy).
            try:
                self._q.get_nowait()
                self._qsize = max(0, self._qsize - 1)
            except Exception:
                pass

        self._q.put((list(frames), path, fps, callback, delete_on_done))
        self._qsize += 1
        return True

    # ── Worker ─────────────────────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            frames, path, fps, callback, delete_flag = self._q.get()
            self._qsize = max(0, self._qsize - 1)

            try:
                self._encode(frames, path, fps)
            except Exception as e:
                logger.error(
                    f"[BgEncoder] Encode failed for {path.name}: {e}",
                    exc_info=True,
                )

            if callback:
                try:
                    callback(path)
                except Exception as e:
                    logger.warning(f"[BgEncoder] Callback error for {path.name}: {e}")

            if delete_flag:
                try:
                    if path.exists():
                        path.unlink()
                        logger.debug(f"[BgEncoder] Deleted (delete_on_done): {path.name}")
                except Exception as e:
                    logger.warning(f"[BgEncoder] Could not delete {path.name}: {e}")

    def _encode(self, frames: List[Tuple[float, bytes]], path: Path, fps: float) -> None:
        if not frames:
            return

        path.parent.mkdir(parents=True, exist_ok=True)

        # Decode the first frame once to learn the output dimensions.
        first_bgr = cv2.imdecode(
            np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR
        )
        if first_bgr is None:
            logger.warning(f"[BgEncoder] Cannot decode first frame for {path.name}")
            return
        h, w = first_bgr.shape[:2]

        # Try codec candidates until one opens successfully.
        writer = None
        for fourcc in _FOURCC_ORDER:
            attempt = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
            if attempt.isOpened():
                writer = attempt
                break
            attempt.release()

        if writer is None:
            logger.error(f"[BgEncoder] No usable VideoWriter codec for {path.name}")
            return

        n = len(frames)
        try:
            writer.write(first_bgr)

            for i, (_, jpeg_bytes) in enumerate(frames[1:], 1):
                bgr = cv2.imdecode(
                    np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR
                )
                if bgr is not None:
                    writer.write(bgr)

                # Yield CPU periodically so real-time threads are not starved.
                if i % _YIELD_EVERY == 0:
                    time.sleep(_YIELD_SLEEP)

        finally:
            writer.release()

        logger.info(
            f"[BgEncoder] Encoded {n} frames {path.name}  "
            f"({n / fps:.1f}s @ {fps:.0f}fps)"
        )

    # ── Diagnostics ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "queued":  self._qsize,
            "dropped": self._dropped,
        }