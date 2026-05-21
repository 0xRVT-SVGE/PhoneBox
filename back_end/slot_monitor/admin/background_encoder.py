# ============================================================
# FILE: back_end/slot_monitor/admin/background_encoder.py
# ============================================================
"""
Background video encoder singleton.  Opt #40 — ProcessPoolExecutor.

Problem solved (original)
──────────────────────────
All encode jobs submitted here so the Flask/SocketIO thread is never
stalled waiting for cv2.VideoWriter.

Opt #40 improvement
────────────────────
The original implementation ran encoding in a daemon *thread*.
cv2.imdecode() + writer.write() hold the Python GIL — so a 30-second
alarm clip stole ~30 s of CPU time from the scanner loop, slot workers,
and WebRTC threads even though it "ran in the background".

Fix: the encode function (`_encode_worker`) is a module-level function
that runs in a `ProcessPoolExecutor` worker process.  Worker processes
have their own GIL, so encoding never competes with any thread in the
main Flask process.

Architecture
────────────
  Coordinator thread (daemon)   — drains the job queue, submits each
                                   encode job to the pool, waits for
                                   the future, then fires the callback.
  ProcessPoolExecutor(1 worker) — one worker avoids concurrent disk I/O
                                   and keeps memory predictable.
  _encode_worker()              — module-level function; picklable;
                                   pure bytes-in / file-out.

Queue overflow
──────────────
MAX_QUEUE jobs max.  On overflow the oldest job is dropped (drop-oldest
policy preserves the most recent evidence).

Callbacks
─────────
The on_done callback runs in the coordinator thread (main process)
after the worker returns — safe for DB inserts, file moves, etc.
"""

import cv2
import logging
import queue
import threading
import time
from concurrent.futures import ProcessPoolExecutor, Future
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
from back_end.config import BgEncoderConfig as _BEC

logger = logging.getLogger(__name__)

_YIELD_EVERY  = _BEC.YIELD_EVERY    # not used in subprocess; kept for compat
_YIELD_SLEEP  = _BEC.YIELD_SLEEP
_MAX_QUEUE    = _BEC.MAX_QUEUE
_FOURCC_ORDER = [cv2.VideoWriter_fourcc(*n) for n in _BEC.FOURCC_ORDER]

_EncodeJob = Tuple[
    List[Tuple[float, bytes]],          # (timestamp, jpeg_bytes) frames
    Path,                               # output path
    float,                              # fps
    Optional[Callable[[Path], None]],   # on_done callback (None = no-op)
    bool,                               # delete_on_done flag
]


# ============================================================
# MODULE-LEVEL WORKER FUNCTION  (must be picklable → top-level)
# ============================================================

def _encode_worker(
    frames:    List[Tuple[float, bytes]],
    path_str:  str,
    fps:       float,
) -> bool:
    """
    Encode a list of (timestamp, jpeg_bytes) frames into an mp4.

    Runs in a subprocess (ProcessPoolExecutor).  No GIL contention
    with the Flask/SocketIO main process.

    Returns True on success, False on failure.
    """
    import cv2, numpy as np
    from pathlib import Path

    if not frames:
        return False

    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Decode first frame to get output dimensions
    first = cv2.imdecode(np.frombuffer(frames[0][1], np.uint8), cv2.IMREAD_COLOR)
    if first is None:
        return False
    h, w = first.shape[:2]

    fourcc_order = [cv2.VideoWriter_fourcc(*n) for n in ("XVID", "mp4v")]
    writer = None
    for fourcc in fourcc_order:
        attempt = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
        if attempt.isOpened():
            writer = attempt
            break
        attempt.release()

    if writer is None:
        return False

    try:
        writer.write(first)
        for _, jpeg_bytes in frames[1:]:
            bgr = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
            if bgr is not None:
                writer.write(bgr)
    finally:
        writer.release()

    return True


# ============================================================
# BACKGROUND ENCODER SINGLETON
# ============================================================

class BackgroundEncoder:
    """
    Singleton background encoder — Opt #40: ProcessPoolExecutor.

    Usage:
        BackgroundEncoder.instance().submit(frames, path, fps)
        BackgroundEncoder.instance().submit(
            frames, path, fps,
            callback=lambda p: db_insert(p),
            delete_on_done=False,
        )
    """

    _inst: Optional["BackgroundEncoder"] = None
    _init_lock = threading.Lock()

    # ── Singleton ────────────────────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "BackgroundEncoder":
        if cls._inst is None:
            with cls._init_lock:
                if cls._inst is None:
                    cls._inst = cls()
        return cls._inst

    def __init__(self) -> None:
        self._q: queue.SimpleQueue[_EncodeJob] = queue.SimpleQueue()
        self._qsize   = 0
        self._dropped = 0

        # Opt #40: one subprocess worker — its own GIL, no contention with Flask
        self._pool = ProcessPoolExecutor(max_workers=1)

        # Coordinator thread: dequeues jobs, submits to pool, fires callbacks
        t = threading.Thread(
            target=self._coordinator, daemon=True, name="BgEncodeCoordinator"
        )
        t.start()
        logger.info(
            "[BgEncoder] Opt #40: ProcessPoolExecutor(1) started — "
            "encoding runs in subprocess (no GIL contention)"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def submit(
        self,
        frames:         List[Tuple[float, bytes]],
        path:           Path,
        fps:            float,
        callback:       Optional[Callable[[Path], None]] = None,
        delete_on_done: bool = False,
    ) -> bool:
        """
        Non-blocking.  Returns True if queued, False if dropped (queue full).

        frames:         List of (timestamp, jpeg_bytes) from RollingBuffer.
        path:           Destination file path (parent dir created if needed).
        fps:            Output video frame rate.
        callback:       Called with `path` after encoding completes.
        delete_on_done: If True, delete the file after encoding + callback.
        """
        if not frames:
            return False

        if self._qsize >= _MAX_QUEUE:
            self._dropped += 1
            logger.warning(
                f"[BgEncoder] Queue full — dropping oldest, queuing: {path.name} "
                f"(total dropped: {self._dropped})"
            )
            try:
                self._q.get_nowait()
                self._qsize = max(0, self._qsize - 1)
            except Exception:
                pass

        self._q.put((list(frames), path, fps, callback, delete_on_done))
        self._qsize += 1
        return True

    # ── Coordinator thread ────────────────────────────────────────────────────

    def _coordinator(self) -> None:
        """
        Drains the job queue.

        For each job:
          1. Submit _encode_worker() to the ProcessPoolExecutor
          2. Block until the future resolves  (coordinator thread blocks,
             NOT the main Flask thread — no event-loop stall)
          3. Fire callback in this thread (main process, safe for DB ops)
          4. Handle delete_on_done
        """
        while True:
            frames, path, fps, callback, delete_flag = self._q.get()
            self._qsize = max(0, self._qsize - 1)

            # Submit to subprocess
            future: Future = self._pool.submit(
                _encode_worker,
                frames,      # picklable: list of (float, bytes)
                str(path),   # picklable: str
                fps,         # picklable: float
            )

            ok = False
            try:
                ok = future.result()   # blocks coordinator thread, not Flask
                if ok:
                    n = len(frames)
                    logger.info(
                        f"[BgEncoder] Encoded {n} frames → {path.name} "
                        f"({n/fps:.1f}s @ {fps:.0f}fps) [subprocess]"
                    )
                else:
                    logger.error(f"[BgEncoder] Encode failed: {path.name}")
            except Exception as e:
                logger.error(f"[BgEncoder] Worker error for {path.name}: {e}", exc_info=True)

            # Callback runs in coordinator thread (main process)
            if callback:
                try:
                    callback(path)
                except Exception as e:
                    logger.warning(f"[BgEncoder] Callback error for {path.name}: {e}")

            # Cleanup
            if delete_flag:
                try:
                    if path.exists():
                        path.unlink()
                        logger.debug(f"[BgEncoder] Deleted (delete_on_done): {path.name}")
                except Exception as e:
                    logger.warning(f"[BgEncoder] Could not delete {path.name}: {e}")

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "queued":  self._qsize,
            "dropped": self._dropped,
            "backend": "ProcessPoolExecutor(1)",
        }

    def shutdown(self, wait: bool = False) -> None:
        """Clean shutdown — call from server _shutdown() if desired."""
        self._pool.shutdown(wait=wait)
        logger.info("[BgEncoder] ProcessPoolExecutor shut down")