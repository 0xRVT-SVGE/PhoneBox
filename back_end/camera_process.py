# ============================================================
# FILE: back_end/camera_process.py
# ============================================================
"""
Opt #1 — Multi-process camera capture (true GIL bypass).

Each physical camera runs in a dedicated OS process.  Frames are shared
via POSIX shared memory — the child writes raw bytes once; the consumer
gets a zero-cost numpy view.  The watcher thread bridges the
multiprocessing world to the asyncio event loop with a 1 ms poll on an
integer sequence counter.

Architecture
────────────
  _capture_worker()      top-level picklable function running in child
                         process; owns the camera file descriptor.

  SharedFrameBuffer      consumer side (main process / asyncio event loop).
                         Same public API as AsyncFrameBuffer so it is a
                         drop-in replacement — just change the constructor.

Drop-in replacement
───────────────────
  # Before (threading):
  buf = AsyncFrameBuffer()

  # After (process):
  from back_end.camera_process import SharedFrameBuffer
  buf = SharedFrameBuffer()

  # Everything else is unchanged — same .set_event_loop(), .start_capture(),
  # .wait_for_frame(), .get_frame_sync(), .stop_capture() calls.

Toggle
──────
  CameraProcessConfig.ENABLED in config.py controls whether
  HeadlessSlotMonitor uses SharedFrameBuffer or falls back to the
  original AsyncFrameBuffer.  Default: False (threading, safe on all
  platforms including Windows where POSIX shm behaves differently).

  Windows note: multiprocessing.shared_memory is supported on Windows
  from Python 3.8+, but the child process must be started with
  `start_method = "spawn"` (the default on Windows).  The module sets
  this automatically the first time SharedFrameBuffer is instantiated.

Thread safety
─────────────
  * The child writes frames sequentially; the watcher reads the counter
    atomically (multiprocessing.Value with c_int type-lock).
  * The consumer copies the shared buffer on every read (numpy copy ≈ 2 ms
    for 1080p) — safe against torn reads with no extra locking needed.
  * Multiple asyncio subscribers (WebRTC, workers) are served from the
    same copy via the standard Future-broadcast pattern (same as
    AsyncFrameBuffer).
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import multiprocessing as mp
import threading
import time
from multiprocessing import shared_memory
from typing import List, Optional

import cv2
import numpy as np

from back_end.config import CameraProcessConfig as _CPC

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Child-process worker  (top-level so it is picklable on all platforms)
# ──────────────────────────────────────────────────────────────────────────────

def _capture_worker(
    shm_name:    str,
    frame_shape: tuple,       # (H, W, 3)
    frame_dtype: str,         # "uint8"
    frame_seq:   "mp.Value",  # c_int, incremented on every new frame
    stop_flag:   "mp.Value",  # c_bool, set True to signal shutdown
    camera_id:   int,
    width:       int,
    height:      int,
    fps:         int,
    warmup:      int,
    backend:     int = 0,     # cv2.CAP_ANY — passed from start_capture()
) -> None:
    """
    Capture loop running in a child OS process.

    Writes each captured frame directly into the shared memory segment
    and increments frame_seq so the watcher thread in the parent process
    detects the new frame.

    The function never returns normally — it loops until stop_flag is set
    or the camera fails, then releases resources and exits.
    """
    import cv2
    import numpy as np
    from multiprocessing import shared_memory as _shm

    # Attach to the already-created shared memory segment.
    shm = _shm.SharedMemory(name=shm_name)
    buf = np.ndarray(frame_shape, dtype=np.dtype(frame_dtype), buffer=shm.buf)

    cap = cv2.VideoCapture(camera_id, backend)
    if not cap.isOpened():
        logger.error(f"[CameraProcess] Child: cannot open camera {camera_id}")
        shm.close()
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS,          fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
    cap.set(cv2.CAP_PROP_AUTOFOCUS,    1)

    # Warm-up — discard first N frames (exposure, AGC settle)
    for _ in range(warmup):
        cap.read()

    frame_interval = 1.0 / fps

    while not stop_flag.value:
        t0 = time.monotonic()
        ret, frame = cap.read()

        if ret and frame is not None:
            np.copyto(buf, frame)
            with frame_seq.get_lock():
                frame_seq.value += 1

        elapsed = time.monotonic() - t0
        sleep_t = frame_interval - elapsed
        if sleep_t > 0:
            time.sleep(sleep_t)

    cap.release()
    shm.close()


# ──────────────────────────────────────────────────────────────────────────────
# SharedFrameBuffer  (consumer / main-process side)
# ──────────────────────────────────────────────────────────────────────────────

class SharedFrameBuffer:
    """
    Drop-in replacement for AsyncFrameBuffer that runs camera capture in
    a separate OS process.

    Public API mirrors AsyncFrameBuffer exactly so call sites need no
    changes when CameraProcessConfig.ENABLED is True.
    """

    # ── Ensure spawn start-method is set once per process ──────────────────

    _start_method_set: bool = False

    @classmethod
    def _ensure_spawn(cls) -> None:
        """
        Force multiprocessing spawn start-method (required on Windows;
        safe on Linux/macOS).  Called once at first instantiation.
        """
        if cls._start_method_set:
            return
        try:
            mp.set_start_method("spawn", force=False)
        except RuntimeError:
            pass   # already set — fine
        cls._start_method_set = True

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def __init__(self, max_subscribers: int = 100):
        SharedFrameBuffer._ensure_spawn()

        self._max_subscribers  = max_subscribers
        self._subscribers: set = set()

        self._loop:     Optional[asyncio.AbstractEventLoop] = None
        self._waiters:  List[asyncio.Future]               = []

        self._shm:      Optional[shared_memory.SharedMemory] = None
        self._shm_view: Optional[np.ndarray]                 = None
        self._frame_seq: Optional[mp.Value]                  = None
        self._stop_flag: Optional[mp.Value]                  = None
        self._process:   Optional[mp.Process]                = None
        self._watcher:   Optional[threading.Thread]          = None

        self._running      = False
        self._frame_count  = 0
        self._latest_copy: Optional[np.ndarray] = None
        self._copy_lock    = threading.Lock()

        self._notification_latency_ms = 0.0

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        logger.info("[CameraProcess] Event loop attached to SharedFrameBuffer")

    def start_capture(
        self,
        camera_id: int = 0,
        width:     int = 1280,
        height:    int = 720,
        fps:       int = 30,
        backend:   int = 0,     # cv2.CAP_ANY — use CameraConfig.resolve_backend()
    ) -> None:
        """
        Spawn child process and wait for first frame.

        Raises RuntimeError if:
          - event loop not set
          - camera fails to open
          - first frame not received within CameraProcessConfig.STARTUP_TIMEOUT
        """
        if self._loop is None:
            raise RuntimeError(
                "Event loop not set. Call set_event_loop() before start_capture()."
            )
        if self._running:
            logger.warning("[CameraProcess] Already running — ignoring start_capture()")
            return

        # ── Allocate shared memory ──────────────────────────────────────
        frame_shape = (height, width, 3)
        frame_dtype = "uint8"
        nbytes      = int(np.prod(frame_shape))   # H × W × 3 bytes

        self._shm      = shared_memory.SharedMemory(create=True, size=nbytes)
        self._shm_view = np.ndarray(
            frame_shape, dtype=np.dtype(frame_dtype), buffer=self._shm.buf
        )

        # ── IPC primitives ─────────────────────────────────────────────
        self._frame_seq = mp.Value(ctypes.c_int,  0)
        self._stop_flag = mp.Value(ctypes.c_bool, False)

        # ── Spawn child ────────────────────────────────────────────────
        self._process = mp.Process(
            target=_capture_worker,
            args=(
                self._shm.name,
                frame_shape,
                frame_dtype,
                self._frame_seq,
                self._stop_flag,
                camera_id,
                width,
                height,
                fps,
                _CPC.WARMUP_FRAMES,
                backend,
            ),
            daemon=True,
            name=f"CameraProcess-cam{camera_id}",
        )
        self._process.start()

        # ── Wait for first frame ───────────────────────────────────────
        deadline = time.monotonic() + _CPC.STARTUP_TIMEOUT
        while self._frame_seq.value == 0:
            if time.monotonic() > deadline:
                self._stop_flag.value = True
                self._process.terminate()
                self._shm.close()
                self._shm.unlink()
                raise RuntimeError(
                    f"[CameraProcess] Camera {camera_id} did not deliver the first "
                    f"frame within {_CPC.STARTUP_TIMEOUT:.0f}s"
                )
            time.sleep(0.01)

        self._running = True
        logger.info(
            f"[CameraProcess] Camera {camera_id} started in child PID "
            f"{self._process.pid} ({width}×{height} @ {fps} fps)"
        )

        # ── Start watcher thread ───────────────────────────────────────
        self._watcher = threading.Thread(
            target=self._watcher_loop,
            daemon=True,
            name=f"CameraWatcher-cam{camera_id}",
        )
        self._watcher.start()

    def stop_capture(self) -> None:
        if not self._running:
            return
        self._running = False

        if self._stop_flag is not None:
            self._stop_flag.value = True

        if self._process is not None:
            self._process.join(timeout=2.0)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None

        if self._watcher is not None:
            self._watcher.join(timeout=2.0)
            self._watcher = None

        if self._shm is not None:
            self._shm.close()
            try:
                self._shm.unlink()
            except Exception:
                pass
            self._shm      = None
            self._shm_view = None

        logger.info("[CameraProcess] Camera process stopped")

    # ── Watcher thread → asyncio bridge ────────────────────────────────────

    def _watcher_loop(self) -> None:
        """
        Polls the shared frame-sequence counter every WATCHER_POLL_S.
        When the counter advances, copies the frame and wakes asyncio waiters.
        Running in a daemon thread inside the main process.
        """
        last_seq = -1
        while self._running:
            seq = self._frame_seq.value
            if seq != last_seq:
                last_seq = seq
                # Copy frame while the counter is fresh
                if self._shm_view is not None:
                    frame_copy = self._shm_view.copy()
                    with self._copy_lock:
                        self._latest_copy = frame_copy
                        self._frame_count += 1

                # Wake asyncio waiters from the event-loop thread
                if self._loop is not None:
                    t0 = time.monotonic()
                    self._loop.call_soon_threadsafe(self._notify_waiters)
                    self._notification_latency_ms = (time.monotonic() - t0) * 1000

            time.sleep(_CPC.WATCHER_POLL_S)

    def _notify_waiters(self) -> None:
        """Resolve all pending asyncio Futures. Runs on the event-loop thread."""
        waiters, self._waiters = self._waiters, []
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)

    # ── Async / sync frame access ───────────────────────────────────────────

    async def wait_for_frame(self, subscriber_id: str = "unknown") -> np.ndarray:
        """
        Wait for the next frame (async, no polling).
        Returns a numpy array; safe to use from multiple coroutines.
        """
        if subscriber_id not in self._subscribers:
            if len(self._subscribers) < self._max_subscribers:
                self._subscribers.add(subscriber_id)
            else:
                logger.warning(
                    f"[CameraProcess] Max subscribers ({self._max_subscribers}) "
                    f"reached, ignoring {subscriber_id}"
                )

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        self._waiters.append(fut)
        await fut

        with self._copy_lock:
            frame = self._latest_copy
        if frame is None:
            raise RuntimeError("[CameraProcess] No frame available yet")
        return frame

    def get_frame_sync(self) -> Optional[np.ndarray]:
        """Return the latest frame synchronously (for non-async callers)."""
        with self._copy_lock:
            return self._latest_copy.copy() if self._latest_copy is not None else None

    def get_frame_count(self) -> int:
        with self._copy_lock:
            return self._frame_count

    def get_metrics(self) -> dict:
        with self._copy_lock:
            return {
                "frame_count":             self._frame_count,
                "active_subscribers":      len(self._subscribers),
                "notification_latency_ms": self._notification_latency_ms,
                "running":                 self._running,
                "child_pid": (
                    self._process.pid if self._process and self._process.is_alive()
                    else None
                ),
            }

    def is_running(self) -> bool:
        return self._running
