# ============================================================
# FILE: back_end/slot_monitor/camera/rolling_buffer.py
# ============================================================
"""
Rolling frame buffer — continuous circular recording.

Opt #8  — TurboJPEG encoder with cv2 fallback
----------------------------------------------
libjpeg-turbo is 2-6× faster than the reference libjpeg used by
cv2.imencode at equivalent quality.  Every push() call encodes a JPEG
frame; at 30 fps × 2 cameras this is the hottest non-CV path in the
process.

Install:   pip install PyTurboJPEG
           Ubuntu/Debian: sudo apt install libturbojpeg
Verify:    python -c "from turbojpeg import TurboJPEG; TurboJPEG()"

Falls back to cv2.imencode if PyTurboJPEG is not installed.
No caller changes required.

Opt #19 — Event-based TopRollingBuffer feed loop (already in source)
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

BUFFER_DURATION_S = _RBC.BUFFER_DURATION_S
JPEG_QUALITY      = _RBC.JPEG_QUALITY
TOP_FPS           = _RBC.TOP_FPS_ACTIVE
TOP_FPS_IDLE      = _RBC.TOP_FPS_IDLE
EVIDENCE_BASE_DIR = Path(_EC.BASE_DIR)

_Frame = Tuple[float, bytes]

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
    """
    Encode a BGR frame to JPEG bytes.
    Uses TurboJPEG when available (Opt #8), otherwise cv2.imencode.
    Returns bytes or None on failure.
    """
    if _TURBO_AVAILABLE:
        try:
            return _turbo.encode(frame, quality=quality)
        except Exception as e:
            logger.debug(f"[RollingBuffer] TurboJPEG error, falling back to cv2: {e}")
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


# ── Core buffer ───────────────────────────────────────────────────────────────

class RollingBuffer:
    def __init__(self, duration_s: float = BUFFER_DURATION_S,
                 jpeg_quality: int = JPEG_QUALITY):
        self._duration = duration_s
        self._quality  = jpeg_quality
        self._lock     = threading.Lock()
        self._frames: deque[_Frame] = deque()

    def push(self, frame: np.ndarray):
        buf = _encode_jpeg(frame, self._quality)
        if buf is None:
            return
        now = time.time()
        with self._lock:
            self._frames.append((now, buf))
            cutoff = now - self._duration
            while self._frames and self._frames[0][0] < cutoff:
                self._frames.popleft()

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
            for _, jpeg_bytes in frames:
                f = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                if f is not None:
                    writer.write(f)
        finally:
            writer.release()
        logger.info(f"[RollingBuffer] Saved {len(frames)} frames ({len(frames)/fps:.1f}s) to {path.name}")
        return True


# ── Face camera buffer (push-based, no thread) ────────────────────────────────

class FaceRollingBuffer:
    def __init__(self):
        self._buffer = RollingBuffer()

    def push(self, frame: np.ndarray):
        self._buffer.push(frame)

    def save_alarm_clip(self, pid: str, lid: int) -> Optional[Path]:
        frames = self._buffer.snapshot()
        if not frames:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / "alarms" / f"{safe_pid}_slot{lid+1}_{dt_str}_face.mp4"
        fps      = self._estimate_fps()
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(frames, path, fps)
        if ok:
            logger.warning(f"[FaceRollingBuffer] Alarm clip queued: {path.name}")
            return path
        return None

    def save_session_clip(self, session_id: str, pid: str, callback=None) -> Optional[Path]:
        frames = self._buffer.snapshot()
        if not frames:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / session_id / f"{safe_pid}_{dt_str}_face.mp4"
        fps      = self._estimate_fps()
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(frames, path, fps, callback=callback)
        if ok:
            logger.warning(f"[FaceRollingBuffer] Session clip queued: {path.name}")
            return path
        return None

    def snapshot(self) -> List[_Frame]:
        return self._buffer.snapshot()

    def frame_count(self) -> int:
        return self._buffer.frame_count()

    def _estimate_fps(self) -> float:
        frames = self._buffer.snapshot()
        if len(frames) < 10:
            return 30.0
        duration = frames[-1][0] - frames[0][0]
        return round(len(frames) / duration, 1) if duration > 0 else 30.0


# ── Top camera buffer (thread-based, event-driven) ───────────────────────────

class TopRollingBuffer:
    """
    Opt #19: event-driven feed loop.
    Opt #8 applies automatically via _encode_jpeg inside RollingBuffer.push().
    """

    def __init__(self):
        self._buffer  = RollingBuffer()
        self._running = False
        self._active  = False
        self._thread: Optional[threading.Thread] = None

    def set_active(self, active: bool):
        if self._active != active:
            self._active = active
            logger.info(
                f"[TopRollingBuffer] {'Active' if active else 'Idle'} mode "
                f"({'%d' % (TOP_FPS if active else TOP_FPS_IDLE)} fps)"
            )

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._feed_loop, daemon=True, name="TopRollingFeed")
        self._thread.start()
        logger.info(f"[TopRollingBuffer] Started ({TOP_FPS_IDLE} fps idle)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("[TopRollingBuffer] Stopped")

    def _feed_loop(self):
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

    def snapshot(self) -> List[_Frame]:
        return self._buffer.snapshot()

    def frame_count(self) -> int:
        return self._buffer.frame_count()

    def save_to_mp4(self, path: Path) -> bool:
        fps = TOP_FPS if self._active else TOP_FPS_IDLE
        return self._buffer.save_to_mp4(path, fps=fps)

    def save_alarm_clip(self, pid: str, lid: int) -> Optional[Path]:
        frames = self._buffer.snapshot()
        if not frames:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / "alarms" / f"{safe_pid}_slot{lid+1}_{dt_str}_top.mp4"
        fps      = float(TOP_FPS if self._active else TOP_FPS_IDLE)
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(frames, path, fps)
        if ok:
            logger.warning(f"[TopRollingBuffer] Alarm clip queued: {path.name}")
            return path
        return None


# ── Global singletons ─────────────────────────────────────────────────────────

face_rolling_buffer = FaceRollingBuffer()
top_rolling_buffer  = TopRollingBuffer()