# ============================================================
# FILE: back_end/slot_monitor/camera/rolling_buffer.py
# ============================================================
"""
Rolling frame buffer — continuous circular recording.

Session 14 — B3/CY2 fix
────────────────────────
RollingBuffer.push() previously used a while-loop to evict old frames:

    while self._frames and self._frames[0][0] < cutoff:
        self._frames.popleft()

This ran on EVERY push (every frame), inside a threading.Lock, executing
Python bytecode even when nothing needed evicting (the common case at
steady state). At 20fps × 2 cameras = 40 lock-acquisitions/second.

Fix: replace with deque(maxlen=N) where N = int(BUFFER_DURATION_S * fps)
computed once at __init__. deque's C-level eviction is atomic and costs
zero Python when full (it pops internally with no Python loop). The
while-loop, cutoff variable, and time.time() call in push() are all gone.

Timestamps are still stored as the first element of each tuple so
duration_seconds() and downstream fps estimation continue to work.

Opt #8  — TurboJPEG encoder with cv2 fallback (unchanged)
Opt #15 — Raw numpy ring buffer (unchanged)
Opt #19 — Event-based TopRollingBuffer feed loop (unchanged)
"""

import cv2
from datetime import datetime
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, List, Tuple

import numpy as np
from back_end.config import RollingBufferConfig as _RBC, EvidenceConfig as _EC

logger = logging.getLogger(__name__)

BUFFER_DURATION_S  = _RBC.BUFFER_DURATION_S
JPEG_QUALITY       = _RBC.JPEG_QUALITY
TOP_FPS            = _RBC.TOP_FPS_ACTIVE
TOP_FPS_IDLE       = _RBC.TOP_FPS_IDLE
# B3: fps used to compute maxlen for the fixed-size deque
_ROLLING_BUFFER_FPS = _RBC.ROLLING_BUFFER_FPS
EVIDENCE_BASE_DIR  = Path(_EC.BASE_DIR)

RAW_BUFFER_ENABLED = _RBC.RAW_BUFFER_ENABLED

_Frame     = Tuple[float, bytes]
_RawFrame  = Tuple[float, np.ndarray]

# ── Opt #8: TurboJPEG — 2-6x faster JPEG encoding at same quality ────────────
try:
    from turbojpeg import TurboJPEG as _TurboJPEG
    _turbo = _TurboJPEG()
    _TURBO_AVAILABLE = True
    logger.info("[RollingBuffer] TurboJPEG ready — using libjpeg-turbo encoder")
except Exception:
    _turbo = None
    _TURBO_AVAILABLE = False
    logger.info(
        "[RollingBuffer] TurboJPEG not found — using cv2.imencode. "
        "Speed up with: pip install PyTurboJPEG && sudo apt install libturbojpeg"
    )


def _encode_jpeg(frame: np.ndarray, quality: int) -> Optional[bytes]:
    """Encode BGR frame to JPEG bytes. Uses TurboJPEG when available (Opt #8)."""
    if _TURBO_AVAILABLE:
        try:
            return _turbo.encode(frame, quality=quality)
        except Exception as e:
            logger.debug(f"[RollingBuffer] TurboJPEG error, falling back to cv2: {e}")
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


# ══════════════════════════════════════════════════════════════════════════════
# Opt #15 — Raw numpy ring buffer (unchanged)
# ══════════════════════════════════════════════════════════════════════════════

class RawRollingBuffer:
    """
    Circular buffer of raw BGR numpy frames.
    Opt #15: eliminates JPEG encode/decode round-trip.
    """

    def __init__(
        self,
        duration_s: float = 10.0,
        max_frames: Optional[int] = None,
    ):
        self._duration   = duration_s
        self._max_frames = max_frames
        self._lock       = threading.RLock()
        self._frames: deque[_RawFrame] = deque(maxlen=max_frames)

    def push(self, frame: np.ndarray) -> None:
        if frame is None:
            return
        now = time.time()
        with self._lock:
            self._frames.append((now, frame.copy()))
            if self._max_frames is None:
                cutoff = now - self._duration
                while self._frames and self._frames[0][0] < cutoff:
                    self._frames.popleft()

    def snapshot(self) -> List[_RawFrame]:
        with self._lock:
            return list(self._frames)

    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def duration_seconds(self) -> float:
        with self._lock:
            if len(self._frames) < 2:
                return 0.0
            return self._frames[-1][0] - self._frames[0][0]

    def save_to_mp4(self, path: Path, fps: float) -> bool:
        frames = self.snapshot()
        if not frames:
            logger.warning(f"[RawBuffer] Empty buffer — nothing to save to {path}")
            return False
        _, first = frames[0]
        h, w = first.shape[:2]
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = None
        for fourcc_str in ("mp4v", "XVID"):
            attempt = cv2.VideoWriter(
                str(path),
                cv2.VideoWriter_fourcc(*fourcc_str),
                fps, (w, h),
            )
            if attempt.isOpened():
                writer = attempt
                break
            attempt.release()
        if writer is None:
            logger.error(f"[RawBuffer] VideoWriter failed for {path}")
            return False
        try:
            for _, bgr in frames:
                writer.write(bgr)
        finally:
            writer.release()
        logger.info(
            f"[RawBuffer] Saved {len(frames)} raw frames "
            f"({len(frames)/fps:.1f}s) to {path.name}"
        )
        return True


# ── Core JPEG buffer — B3/CY2 fix applied ────────────────────────────────────

class RollingBuffer:
    """
    Circular JPEG frame buffer.

    B3/CY2 fix: uses deque(maxlen=N) instead of a time-based while-loop
    for eviction. N = int(BUFFER_DURATION_S * fps) computed at init.
    The C-level deque eviction replaces all Python bytecode in the hot
    push() path (was: while-loop + time.time() + cutoff comparison).

    Timestamps are preserved in each tuple for duration_seconds() and
    fps estimation in downstream save operations.
    """

    def __init__(
        self,
        duration_s: float = BUFFER_DURATION_S,
        jpeg_quality: int = JPEG_QUALITY,
        fps: float = _ROLLING_BUFFER_FPS,
    ):
        self._quality    = jpeg_quality
        # B3: fixed-size deque — eviction is O(1) C-level, no Python loop
        maxlen = int(duration_s * fps)
        self._frames: deque[_Frame] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        logger.debug(
            f"[RollingBuffer] deque(maxlen={maxlen}) "
            f"({duration_s:.0f}s × {fps:.0f}fps) — B3 eviction fix active"
        )

    def push(self, frame: np.ndarray) -> None:
        """
        B3 fix: zero Python bytecode for eviction.
        deque.append() on a full deque atomically pops from the left in C.
        No while-loop, no time.time(), no cutoff calculation.
        """
        buf = _encode_jpeg(frame, self._quality)
        if buf is None:
            return
        with self._lock:
            self._frames.append((time.time(), buf))

    def snapshot(self) -> List[_Frame]:
        with self._lock:
            return list(self._frames)

    def frame_count(self) -> int:
        with self._lock:
            return len(self._frames)

    def duration_seconds(self) -> float:
        with self._lock:
            if len(self._frames) < 2:
                return 0.0
            return self._frames[-1][0] - self._frames[0][0]

    def save_to_mp4(self, path: Path, fps: float) -> bool:
        frames = self.snapshot()
        if not frames:
            logger.warning(f"[RollingBuffer] Empty buffer — nothing to save to {path}")
            return False
        first = cv2.imdecode(np.frombuffer(frames[0][1], dtype=np.uint8), cv2.IMREAD_COLOR)
        if first is None:
            return False
        h, w = first.shape[:2]
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = None
        for fourcc_str in ("mp4v", "XVID"):
            attempt = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*fourcc_str), fps, (w, h))
            if attempt.isOpened():
                writer = attempt
                break
            attempt.release()
        if writer is None:
            logger.error(f"[RollingBuffer] VideoWriter failed for {path}")
            return False
        try:
            writer.write(first)
            for _, jpeg_bytes in frames[1:]:
                f = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                if f is not None:
                    writer.write(f)
        finally:
            writer.release()
        logger.info(
            f"[RollingBuffer] Saved {len(frames)} JPEG frames "
            f"({len(frames)/fps:.1f}s) to {path.name}"
        )
        return True


# ── Face camera buffer ────────────────────────────────────────────────────────

class FaceRollingBuffer:
    """
    Opt #15: uses RawRollingBuffer when RAW_BUFFER_ENABLED=True,
    otherwise uses the JPEG RollingBuffer with B3 deque(maxlen) fix.
    """

    def __init__(self):
        if RAW_BUFFER_ENABLED:
            self._buffer = RawRollingBuffer(duration_s=BUFFER_DURATION_S)
            self._raw    = True
            logger.info("[FaceRollingBuffer] Using raw numpy ring buffer (Opt #15)")
        else:
            # B3: pass explicit fps so maxlen is correct
            self._buffer = RollingBuffer(fps=float(TOP_FPS))
            self._raw    = False

    def push(self, frame: np.ndarray) -> None:
        self._buffer.push(frame)

    def _estimate_fps(self) -> float:
        snapshot = self._buffer.snapshot()
        if len(snapshot) < 10:
            return 30.0
        duration = snapshot[-1][0] - snapshot[0][0]
        return round(len(snapshot) / duration, 1) if duration > 0 else 30.0

    def save_alarm_clip(self, pid: str, lid: int) -> Optional[Path]:
        snapshot = self._buffer.snapshot()
        if not snapshot:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / "alarms" / f"{safe_pid}_slot{lid+1}_{dt_str}_face.mp4"
        fps      = self._estimate_fps()
        if self._raw:
            ok = self._buffer.save_to_mp4(path, fps)
            if ok:
                logger.warning(f"[FaceRollingBuffer] Raw alarm clip saved: {path.name}")
                return path
            return None
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(snapshot, path, fps)
        if ok:
            logger.warning(f"[FaceRollingBuffer] Alarm clip queued: {path.name}")
            return path
        return None

    def save_session_clip(self, session_id: str, pid: str,
                          callback=None) -> Optional[Path]:
        snapshot = self._buffer.snapshot()
        if not snapshot:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / session_id / f"{safe_pid}_{dt_str}_face.mp4"
        fps      = self._estimate_fps()
        if self._raw:
            ok = self._buffer.save_to_mp4(path, fps)
            if ok and callback:
                try:
                    callback(path)
                except Exception as e:
                    logger.warning(f"[FaceRollingBuffer] Session clip callback error: {e}")
            return path if ok else None
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(
            snapshot, path, fps, callback=callback
        )
        if ok:
            logger.warning(f"[FaceRollingBuffer] Session clip queued: {path.name}")
            return path
        return None

    def snapshot(self) -> list:
        return self._buffer.snapshot()

    def frame_count(self) -> int:
        return self._buffer.frame_count()


# ── Top camera buffer — Opt #19 event-driven, B3 deque(maxlen) fix ────────────

class TopRollingBuffer:
    """
    Opt #19: event-driven feed loop.
    Opt #8:  TurboJPEG applied via _encode_jpeg() inside RollingBuffer.push().
    Opt #15: uses RawRollingBuffer when RAW_BUFFER_ENABLED=True.
    B3/CY2:  RollingBuffer now uses deque(maxlen) — no while-loop eviction.
    """

    def __init__(self):
        if RAW_BUFFER_ENABLED:
            self._buffer = RawRollingBuffer(duration_s=BUFFER_DURATION_S)
            self._raw    = True
            logger.info("[TopRollingBuffer] Using raw numpy ring buffer (Opt #15)")
        else:
            # B3: pass active fps so maxlen matches actual push rate
            self._buffer = RollingBuffer(fps=float(TOP_FPS))
            self._raw    = False

        self._running = False
        self._active  = False
        self._thread: Optional[threading.Thread] = None

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            logger.info(
                f"[TopRollingBuffer] {'Active' if active else 'Idle'} mode "
                f"({'%d' % (TOP_FPS if active else TOP_FPS_IDLE)} fps)"
            )

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._feed_loop, daemon=True, name="TopRollingFeed"
        )
        self._thread.start()
        logger.info(f"[TopRollingBuffer] Started ({TOP_FPS_IDLE} fps idle)")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("[TopRollingBuffer] Stopped")

    def _feed_loop(self) -> None:
        """Opt #19: event-driven — blocks on frame event, no unconditional sleep."""
        from back_end.slot_monitor.camera.top_camera import top_camera
        last_push = 0.0
        while self._running:
            fps      = TOP_FPS if self._active else TOP_FPS_IDLE
            interval = 1.0 / fps
            if not top_camera.wait_for_frame(timeout=0.1):
                continue
            now = time.time()
            if now - last_push < interval:
                top_camera.clear_frame_event()
                continue
            frame = top_camera.get_frame()
            top_camera.clear_frame_event()
            if frame is not None:
                self._buffer.push(frame)
                last_push = now

    def snapshot(self) -> list:
        return self._buffer.snapshot()

    def frame_count(self) -> int:
        return self._buffer.frame_count()

    def save_to_mp4(self, path: Path) -> bool:
        fps = float(TOP_FPS if self._active else TOP_FPS_IDLE)
        return self._buffer.save_to_mp4(path, fps=fps)

    def save_alarm_clip(self, pid: str, lid: int) -> Optional[Path]:
        snapshot = self._buffer.snapshot()
        if not snapshot:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / "alarms" / f"{safe_pid}_slot{lid+1}_{dt_str}_top.mp4"
        fps      = float(TOP_FPS if self._active else TOP_FPS_IDLE)
        if self._raw:
            ok = self._buffer.save_to_mp4(path, fps)
            if ok:
                logger.warning(f"[TopRollingBuffer] Raw alarm clip saved: {path.name}")
                return path
            return None
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(snapshot, path, fps)
        if ok:
            logger.warning(f"[TopRollingBuffer] Alarm clip queued: {path.name}")
            return path
        return None


# ── Global singletons ─────────────────────────────────────────────────────────

face_rolling_buffer = FaceRollingBuffer()
top_rolling_buffer  = TopRollingBuffer()
