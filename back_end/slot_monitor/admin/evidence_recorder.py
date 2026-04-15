# ============================================================
# FILE: back_end/slot_monitor/admin/evidence_recorder.py
# ============================================================
"""
Evidence Recorder — per-phone video clips for admin resolution sessions.

Recording model
───────────────
A video clip is started for each phone the moment the admin physically
picks it up (admin_remove_phone).  The clip records continuously from the
top camera while the admin identifies and resolves the phone.

The clip is kept or deleted based on outcome:

    admin_place_phone success       → DELETE   (normal resolution)
    admin_no_qr_found               → KEEP     (unidentified object)
    admin_declare_missing           → KEEP     (phone never found)
    needs_deposit (no DB record)    → KEEP     (anomalous state)
    session closed with warnings    → KEEP ALL remaining clips

If a clip is already being recorded when a new phone is picked up (shouldn't
happen in normal flow but possible during error recovery), the previous clip
is finalised first.

Storage
───────
    evidence/{session_id}/{pid}_{lid}_{timestamp}.mp4
    DB: evidence_sessions + evidence_items (session-level metadata only)

Clip recording
──────────────
A background thread reads frames from top_camera at the camera's native
rate and writes them to an mp4 using cv2.VideoWriter.  The thread is
lightweight — it only runs while a phone is in the admin's hand.

TODO: Configurable codec (currently tries mp4v then falls back to XVID).
TODO: Configurable fps (currently tracks elapsed time to estimate real fps).
TODO: Add supervisor notification on kept sessions.
"""

import cv2
import json
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from back_end.Database.db import get_conn, put_conn
from back_end.slot_monitor.camera.top_camera import top_camera

logger = logging.getLogger(__name__)

EVIDENCE_BASE_DIR = Path("evidence")
# Target recording FPS — camera may deliver fewer; we record what we get
RECORD_FPS = 20.0


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db_create_session(session_id: str, opened_at: datetime):
    conn = get_conn()
    # DB column is varchar — truncate to 64 chars defensively
    db_sid = session_id[:64]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evidence_sessions (session_id, opened_at)
                VALUES (%s, %s) ON CONFLICT (session_id) DO NOTHING;
                """,
                (db_sid, opened_at),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[Evidence] Failed to create session record: {e}")
    finally:
        put_conn(conn)


def _db_insert_clip(
    session_id: str,
    pid: str,
    lid: int,
    clip_path: str,
    outcome: str,
    duration_s: float,
):
    """Record one video clip in evidence_items."""
    db_sid = session_id[:64]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO evidence_items
                    (session_id, event_type, lid, pid, photo_path, metadata)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    db_sid,
                    "video_clip",
                    lid,
                    pid,
                    clip_path,
                    json.dumps({"outcome": outcome, "duration_s": round(duration_s, 1)}),
                ),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[Evidence] Failed to insert clip record: {e}")
    finally:
        put_conn(conn)


def _db_close_session(session_id: str, outcome: str, warnings: list, kept: bool):
    db_sid = session_id[:64]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE evidence_sessions
                SET closed_at = NOW(), outcome = %s, warnings = %s, kept = %s
                WHERE session_id = %s;
                """,
                (outcome, json.dumps(warnings), kept, db_sid),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[Evidence] Failed to close session record: {e}")
    finally:
        put_conn(conn)


def _db_delete_session(session_id: str):
    db_sid = session_id[:64]
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM evidence_items WHERE session_id = %s;", (db_sid,))
            cur.execute("DELETE FROM evidence_sessions WHERE session_id = %s;", (db_sid,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[Evidence] Failed to delete session records: {e}")
    finally:
        put_conn(conn)


# ── PhoneRecorder — one clip per phone ───────────────────────────────────────

class PhoneRecorder:
    """
    Records a video clip from top_camera for one phone resolution attempt.

    Architecture change (performance fix)
    ──────────────────────────────────────
    The original implementation decoded up to 600 JPEG pre-buffer frames
    synchronously before live capture could begin, causing lag on admin pick-up.

    New model:
        start()   → snapshot pre-buffer, submit to BackgroundEncoder (non-blocking),
                    then start live capture thread immediately.
        stop()    → stop live capture, flush VideoWriter.
                    keep=True:  DB records inserted; pre-buffer DB record inserted
                                via callback once BackgroundEncoder finishes.
                    keep=False: live clip deleted; pre-buffer clip deleted by
                                BackgroundEncoder delete-on-done flag.
    """

    def __init__(self, session_id: str, pid: str, lid: int, session_dir: Path):
        self.session_id = session_id
        self.pid        = pid
        self.lid        = lid

        ts = int(time.time())
        safe_pid = pid.replace("-", "")[:16]
        self._path     = session_dir / f"{safe_pid}_lid{lid}_{ts}_live.mp4"
        self._pre_path = session_dir / f"{safe_pid}_lid{lid}_{ts}_pre.mp4"
        self._started_at: float = 0.0

        self._running   = False
        self._keep_pre  = False
        self._pre_done  = False
        self._thread:  Optional[threading.Thread] = None
        self._writer:  Optional[cv2.VideoWriter]  = None
        self._frame_count = 0

    def start(self):
        """
        Submit pre-buffer clip to BackgroundEncoder (non-blocking), then
        start live capture immediately.  Returns in microseconds.
        """
        from back_end.slot_monitor.camera.rolling_buffer import (
            top_rolling_buffer, TOP_FPS,
        )
        from back_end.slot_monitor.admin.background_encoder import BackgroundEncoder

        self._running    = True
        self._started_at = time.time()

        pre_frames = top_rolling_buffer.snapshot()
        if pre_frames:
            BackgroundEncoder.instance().submit(
                frames         = pre_frames,
                path           = self._pre_path,
                fps            = float(TOP_FPS),
                callback       = self._on_pre_encoded,
                delete_on_done = False,
            )

        self._thread = threading.Thread(
            target=self._record_live,
            daemon=True,
            name=f"EvidenceRec-{self.pid[:8]}",
        )
        self._thread.start()

        logger.info(
            f"[Evidence] Recording started: PID={self.pid} LID={self.lid} "
            f"live={self._path.name}  "
            f"pre={'queued' if pre_frames else 'empty'} ({len(pre_frames)} frames)"
        )

    def _on_pre_encoded(self, path: Path) -> None:
        """Called by BackgroundEncoder when pre-buffer clip is encoded."""
        self._pre_done = True
        if not self._keep_pre:
            try:
                if path.exists():
                    path.unlink()
                    logger.debug(f"[Evidence] Pre-buffer clip discarded: {path.name}")
            except Exception as e:
                logger.warning(f"[Evidence] Could not delete pre-buffer clip: {e}")
        else:
            try:
                _db_insert_clip(
                    session_id = self.session_id,
                    pid        = self.pid,
                    lid        = self.lid,
                    clip_path  = str(path),
                    outcome    = "pre_buffer",
                    duration_s = 0.0,
                )
                logger.info(f"[Evidence] Pre-buffer DB record inserted: {path.name}")
            except Exception as e:
                logger.warning(f"[Evidence] Pre-buffer DB insert failed: {e}")

    def stop(self, keep: bool) -> float:
        self._running  = False
        self._keep_pre = keep

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        if self._writer:
            self._writer.release()
            self._writer = None

        duration = time.time() - self._started_at

        if keep:
            if self._path.exists() and self._frame_count > 0:
                _db_insert_clip(
                    session_id = self.session_id,
                    pid        = self.pid,
                    lid        = self.lid,
                    clip_path  = str(self._path),
                    outcome    = "kept_live",
                    duration_s = duration,
                )
                logger.warning(
                    f"[Evidence] Live clip KEPT: {self._path.name} "
                    f"({self._frame_count} frames, {duration:.1f}s)"
                )
                # Face cam: async with DB-insert callback.
                try:
                    from back_end.slot_monitor.camera.rolling_buffer import (
                        face_rolling_buffer,
                    )
                    sid, pid_, lid_, dur = (
                        self.session_id, self.pid, self.lid, duration
                    )
                    def _insert_face_db(face_path: Path) -> None:
                        try:
                            _db_insert_clip(
                                session_id = sid, pid = pid_, lid = lid_,
                                clip_path  = str(face_path),
                                outcome    = "kept_face_cam",
                                duration_s = dur,
                            )
                        except Exception as ex:
                            logger.warning(f"[Evidence] Face DB insert failed: {ex}")

                    face_rolling_buffer.save_session_clip(
                        session_id = self.session_id,
                        pid        = self.pid,
                        callback   = _insert_face_db,
                    )
                except Exception as e:
                    logger.warning(f"[Evidence] Face clip queue failed: {e}")

                # Pre-buffer: if already encoded insert DB now; else callback handles it.
                if self._pre_done and self._pre_path.exists():
                    try:
                        _db_insert_clip(
                            session_id = self.session_id, pid = self.pid,
                            lid = self.lid, clip_path = str(self._pre_path),
                            outcome = "pre_buffer", duration_s = 0.0,
                        )
                    except Exception:
                        pass
            else:
                logger.warning(
                    f"[Evidence] Live clip flagged to keep but empty/missing: "
                    f"{self._path.name}"
                )
        else:
            try:
                if self._path.exists():
                    self._path.unlink()
            except Exception as e:
                logger.warning(f"[Evidence] Could not delete live clip: {e}")

            if self._pre_done:
                try:
                    if self._pre_path.exists():
                        self._pre_path.unlink()
                except Exception as e:
                    logger.warning(f"[Evidence] Could not delete pre-buffer clip: {e}")

            logger.info(f"[Evidence] Clips discarded (clean): {self._path.name}")

        return duration

    def _record_live(self) -> None:
        """Phase 2: live capture from top_camera. Starts immediately."""
        from back_end.slot_monitor.camera.top_camera import top_camera

        writer_ready = False
        interval     = 1.0 / RECORD_FPS

        while self._running:
            loop_start = time.time()

            got = top_camera.wait_for_frame(timeout=0.2)
            if not got:
                continue

            frame = top_camera.get_frame()
            top_camera.clear_frame_event()
            if frame is None:
                continue

            if not writer_ready:
                h, w = frame.shape[:2]
                writer_ready = self._init_writer(w, h)
                if not writer_ready:
                    logger.error("[Evidence] VideoWriter init failed on live frame")
                    self._running = False
                    break

            self._writer.write(frame)
            self._frame_count += 1

            elapsed = time.time() - loop_start
            sleep   = interval - elapsed
            if sleep > 0:
                time.sleep(sleep)

    def _init_writer(self, width: int, height: int) -> bool:
        """Try XVID first (fastest encode), fall back to mp4v."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        for fourcc_str in ("XVID", "mp4v"):
            fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
            writer = cv2.VideoWriter(
                str(self._path), fourcc, RECORD_FPS, (width, height)
            )
            if writer.isOpened():
                self._writer = writer
                logger.debug(
                    f"[Evidence] VideoWriter opened ({fourcc_str}) "
                    f"{width}x{height} @ {RECORD_FPS}fps"
                )
                return True
            writer.release()

        logger.error(f"[Evidence] Could not open VideoWriter for {self._path}")
        return False


class EvidenceRecorder:
    """
    Manages per-phone video clips for one admin resolution session.

    Usage
    ─────
        recorder = EvidenceRecorder(session_id)
        recorder.start()                           # create session dir + DB record

        recorder.start_clip(pid, lid)              # phone picked up
        recorder.stop_clip(keep=False)             # phone placed correctly → delete
        recorder.stop_clip(keep=True)              # anomaly → keep

        recorder.close(outcome, warnings)          # finalise session
    """

    def __init__(self, session_id: str):
        self.session_id  = session_id
        self._session_dir = EVIDENCE_BASE_DIR / session_id
        self._opened_at  = datetime.utcnow()

        self._current: Optional[PhoneRecorder] = None
        self._any_kept  = False  # True if at least one clip was kept

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """Create session directory and DB record."""
        self._session_dir.mkdir(parents=True, exist_ok=True)
        _db_create_session(self.session_id, self._opened_at)
        logger.info(
            f"[Evidence] Session started: {self.session_id}  "
            f"dir={self._session_dir}"
        )

    def close(self, outcome: str, warnings: list):
        """
        Finalise the session.

        If a clip is still recording (e.g. session forced closed), it is
        kept — that's an anomaly.  If warnings exist, all remaining clips
        are already kept.  On a clean close, delete the session directory.
        """
        # If recording was active when session closed (shouldn't happen normally)
        if self._current is not None:
            logger.warning(
                f"[Evidence] Session closed while clip was recording — keeping clip"
            )
            self._current.stop(keep=True)
            self._any_kept = True
            self._current = None

        if warnings:
            self._any_kept = True

        kept = self._any_kept

        if outcome == "force_closed":
            kept = True

        if kept or outcome == "force_closed":
            # Rename session directory to NotFullyResolved-{date}-{session_id}
            # so it is immediately recognisable on disk.
            if outcome == "force_closed" and self._session_dir.exists():
                date_str   = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                safe_sid   = self.session_id.replace("/", "_").replace("\\", "_")[:16]
                new_name   = f"NotFullyResolved-{date_str}-{safe_sid}"
                new_dir    = EVIDENCE_BASE_DIR / new_name
                try:
                    self._session_dir.rename(new_dir)
                    self._session_dir = new_dir
                    logger.warning(
                        f"[Evidence] Session dir renamed -> {new_dir.name}"
                    )
                except Exception as e:
                    logger.error(f"[Evidence] Failed to rename session dir: {e}")

            _db_close_session(
                session_id=self.session_id,
                outcome=outcome,
                warnings=warnings,
                kept=True,
            )
            logger.warning(
                f"[Evidence] Session {self.session_id} evidence KEPT "
                f"outcome={outcome!r} warnings={len(warnings)}"
            )
        else:
            # Clean session — delete everything
            try:
                if self._session_dir.exists():
                    shutil.rmtree(self._session_dir)
                    logger.info(
                        f"[Evidence] Session dir deleted (clean): {self._session_dir}"
                    )
            except Exception as e:
                logger.error(
                    f"[Evidence] Failed to delete session dir: {e}"
                )
            _db_delete_session(self.session_id)
            logger.info(
                f"[Evidence] Session {self.session_id} evidence deleted (clean)"
            )

    # ── Per-phone clip control ────────────────────────────────────────────────

    def start_clip(self, pid: str, lid: int):
        """
        Start recording a clip for the phone now in the admin's hand.

        If a previous clip is still open (error recovery path), it is kept
        automatically.
        """
        if self._current is not None:
            logger.warning(
                f"[Evidence] start_clip called while {self._current.pid} clip still "
                f"open — finalising previous clip as KEPT"
            )
            self._current.stop(keep=True)
            self._any_kept = True
            self._current = None

        recorder = PhoneRecorder(
            session_id=self.session_id,
            pid=pid,
            lid=lid,
            session_dir=self._session_dir,
        )
        recorder.start()
        self._current = recorder

    def stop_clip(self, keep: bool, reason: str = ""):
        """
        Stop the current clip.

        Args:
            keep:   True = anomaly, keep the file.
                    False = successful resolution, delete the file.
            reason: Log annotation (e.g. "placed", "no_qr", "missing").
        """
        if self._current is None:
            # No clip running — nothing to do
            return

        recorder = self._current
        self._current = None

        duration = recorder.stop(keep=keep)

        if keep:
            self._any_kept = True
            logger.warning(
                f"[Evidence] Clip kept — reason={reason!r}  "
                f"PID={recorder.pid}  duration={duration:.1f}s"
            )
        else:
            logger.info(
                f"[Evidence] Clip deleted — PID={recorder.pid}  "
                f"duration={duration:.1f}s"
            )

    def stop_current_clip_if_active(self, keep: bool, reason: str = ""):
        """
        Convenience: stop clip only if one is currently recording.
        Safe to call speculatively (e.g. at session close).
        """
        if self._current is not None:
            self.stop_clip(keep=keep, reason=reason)

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def is_flagged(self) -> bool:
        """True if at least one clip has been kept."""
        return self._any_kept

    @property
    def is_recording(self) -> bool:
        """True if a clip is currently being recorded."""
        return self._current is not None