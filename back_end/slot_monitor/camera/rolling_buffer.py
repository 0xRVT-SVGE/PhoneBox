# ============================================================
# FILE: back_end/slot_monitor/camera/rolling_buffer.py
# ============================================================
"""
Rolling frame buffer — continuous circular recording.

Two buffers, two feed strategies:

    face_rolling_buffer (FaceRollingBuffer)
        Camera: front/face camera (camera 0)
        Feed:   PUSH-BASED — scanner_loop.process_frame() calls
                face_rolling_buffer.push(frame) once per frame.
                No background thread, zero polling overhead.
        Saved:  on alarm trigger  → evidence/alarms/{pid}_lid{N}_{ts}_face.mp4
                on admin session kept → evidence/{session_id}/{pid}_face_{ts}.mp4

    top_rolling_buffer (TopRollingBuffer)
        Camera: top-down camera (camera 2)
        Feed:   thread-based feeder reading from top_camera buffer
        Saved:  prepended to admin evidence clips (pre-pickup footage)

Memory estimate (JPEG quality 70, 30s buffer):
    Face (1080p, 30fps):  ~60KB/frame × 900 frames ≈ 54MB
    Top  (720p,  20fps):  ~40KB/frame × 600 frames ≈ 24MB
    Total ≈ 78MB at peak
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

# Edit back_end/config.py → RollingBufferConfig / EvidenceConfig to change these.
BUFFER_DURATION_S = _RBC.BUFFER_DURATION_S
JPEG_QUALITY      = _RBC.JPEG_QUALITY
TOP_FPS           = _RBC.TOP_FPS_ACTIVE
TOP_FPS_IDLE      = _RBC.TOP_FPS_IDLE
EVIDENCE_BASE_DIR = Path(_EC.BASE_DIR)

_Frame = Tuple[float, bytes]


# ── Core buffer ───────────────────────────────────────────────────────────────

class RollingBuffer:
    def __init__(self, duration_s: float = BUFFER_DURATION_S,
                 jpeg_quality: int = JPEG_QUALITY):
        self._duration = duration_s
        self._quality  = jpeg_quality
        self._lock     = threading.Lock()
        self._frames: deque[_Frame] = deque()

    def push(self, frame: np.ndarray):
        ok, buf = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._quality]
        )
        if not ok:
            return
        now = time.time()
        with self._lock:
            self._frames.append((now, buf.tobytes()))
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
        first = cv2.imdecode(
            np.frombuffer(frames[0][1], dtype=np.uint8), cv2.IMREAD_COLOR
        )
        if first is None:
            return False
        h, w = first.shape[:2]
        path.parent.mkdir(parents=True, exist_ok=True)
        writer = None
        for fourcc_str in ("mp4v", "XVID"):
            attempt = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*fourcc_str), fps, (w, h)
            )
            if attempt.isOpened():
                writer = attempt
                break
            attempt.release()
        if writer is None:
            logger.error(f"[RollingBuffer] VideoWriter failed for {path}")
            return False
        try:
            for _, jpeg_bytes in frames:
                f = cv2.imdecode(
                    np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if f is not None:
                    writer.write(f)
        finally:
            writer.release()
        logger.info(
            f"[RollingBuffer] Saved {len(frames)} frames "
            f"({len(frames)/fps:.1f}s) to {path.name}"
        )
        return True


# ── Face camera buffer (push-based, no thread) ────────────────────────────────

class FaceRollingBuffer:
    """
    Rolling buffer for the front camera.

    NO background thread — scanner_loop.process_frame() pushes frames
    directly via face_rolling_buffer.push(frame).  This is faster and
    simpler than polling scanner_state from a separate thread.
    """

    def __init__(self):
        self._buffer = RollingBuffer()

    def push(self, frame: np.ndarray):
        """Called from scanner_loop every frame. Thread-safe, ~0.1ms overhead."""
        self._buffer.push(frame)

    def save_alarm_clip(self, pid: str, lid: int) -> Optional[Path]:
        """
        Queue an alarm clip for background encoding.

        Returns the path immediately (file will exist once encoding completes).
        Non-blocking — does NOT stall the alarm trigger thread.
        """
        frames = self._buffer.snapshot()
        if not frames:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        slot_num = lid + 1
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / "alarms" / f"{safe_pid}_slot{slot_num}_{dt_str}_face.mp4"
        fps      = self._estimate_fps()

        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(frames, path, fps)
        if ok:
            logger.warning(f"[FaceRollingBuffer] Alarm clip queued: {path.name}")
            return path
        return None

    def save_session_clip(
        self,
        session_id: str,
        pid: str,
        callback=None,
    ) -> Optional[Path]:
        """
        Queue a session evidence clip for background encoding.

        Args:
            callback: Called with `path` once encoding completes.
                      Use this to do DB inserts that need the file to exist.

        Returns the path immediately (file will exist once encoding completes).
        """
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
        """Infer fps from buffer timestamps; falls back to 30."""
        frames = self._buffer.snapshot()
        if len(frames) < 10:
            return 30.0
        duration = frames[-1][0] - frames[0][0]
        return round(len(frames) / duration, 1) if duration > 0 else 30.0


# ── Top camera buffer (thread-based) ─────────────────────────────────────────

class TopRollingBuffer:
    """
    Rolling buffer for the top-down camera (camera 2).

    Samples at TOP_FPS_IDLE (5 fps) by default for power efficiency.
    Switches to TOP_FPS (20 fps) when any DVW or admin operation is active.

    Call set_active(True)  when an operation starts.
    Call set_active(False) when it ends.

    The actual frame rate is only a buffer sampling rate — the top_camera
    capture thread and the WebRTC stream always run at full hardware fps.
    """

    def __init__(self):
        self._buffer  = RollingBuffer()
        self._running = False
        self._active  = False   # True = operation in progress → full fps
        self._thread: Optional[threading.Thread] = None

    def set_active(self, active: bool):
        """
        Switch between idle (5 fps) and active (20 fps) recording.

        active=True  → called when a DVW op or admin session starts
        active=False → called when it ends (success, failure, or close)
        """
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
        self._thread  = threading.Thread(
            target=self._feed_loop, daemon=True, name="TopRollingFeed"
        )
        self._thread.start()
        logger.info(f"[TopRollingBuffer] Started ({TOP_FPS_IDLE} fps idle)")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("[TopRollingBuffer] Stopped")

    def _feed_loop(self):
        from back_end.slot_monitor.camera.top_camera import top_camera
        while self._running:
            t0       = time.time()
            fps      = TOP_FPS if self._active else TOP_FPS_IDLE
            interval = 1.0 / fps

            got = top_camera.wait_for_frame(timeout=0.1)
            if got:
                frame = top_camera.get_frame()
                top_camera.clear_frame_event()
                if frame is not None:
                    self._buffer.push(frame)

            sleep = interval - (time.time() - t0)
            if sleep > 0:
                time.sleep(sleep)

    def snapshot(self) -> List[_Frame]:
        return self._buffer.snapshot()

    def frame_count(self) -> int:
        return self._buffer.frame_count()

    def save_to_mp4(self, path: Path) -> bool:
        """Synchronous save — kept for backward compat. Prefer save_alarm_clip."""
        fps = TOP_FPS if self._active else TOP_FPS_IDLE
        return self._buffer.save_to_mp4(path, fps=fps)

    def save_alarm_clip(self, pid: str, lid: int) -> Optional[Path]:
        """
        Queue a top-cam alarm clip for background encoding.

        Returns the path immediately (file will exist once encoding completes).
        Non-blocking — does NOT stall the alarm trigger thread.
        """
        frames = self._buffer.snapshot()
        if not frames:
            return None
        dt_str   = datetime.now().strftime("%Y%m%d-%H%M%S")
        slot_num = lid + 1
        safe_pid = pid.replace("-", "")[:16]
        path     = EVIDENCE_BASE_DIR / "alarms" / f"{safe_pid}_slot{slot_num}_{dt_str}_top.mp4"
        fps      = float(TOP_FPS if self._active else TOP_FPS_IDLE)

        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder
        ok = BackgroundEncoder.instance().submit(frames, path, fps)
        if ok:
            logger.warning(f"[TopRollingBuffer] Alarm clip queued: {path.name}")
            return path
        return None


# ── Global singletons ─────────────────────────────────────────────────────────

face_rolling_buffer = FaceRollingBuffer()   # push-based, no thread
top_rolling_buffer  = TopRollingBuffer()    # thread-based, starts with top_camera