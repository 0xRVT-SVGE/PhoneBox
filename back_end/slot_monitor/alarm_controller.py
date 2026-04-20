# ============================================================
# FILE: back_end/slot_monitor/alarm_controller.py
# ============================================================
import logging
import threading
import time

from back_end.secrets import Secrets

logger = logging.getLogger(__name__)


class AlarmController:
    """
    Centralized alarm state management.
    Thread-safe. No DB logic.

    Sound lifecycle:
        trigger()    → starts sound + emits alarm_triggered
        silence()    → stops sound only, mismatches remain
        unsilence()  → restarts sound if mismatches still exist
        resolve()    → removes one mismatch; stops sound if set empties
        clear()      → admin override — clears all mismatches + stops sound

    Note on false positives during DVW operations:
        The worker layer (worker_async.py) suppresses trigger() calls while
        any DVW or admin session is active, so this class never needs to
        filter them itself.  AlarmController only sees legitimate triggers.

    Clip saving:
        Rolling-buffer clips are submitted to BackgroundEncoder (non-blocking).
        The call returns in microseconds — no stalling of the async worker loop.
    """

    def __init__(self):
        self._lock             = threading.Lock()
        self.active            = False
        self.mismatches: set[tuple[str, int]] = set()
        self._alarm_start_time = None
        self._silenced         = False
        self.socketio          = None

    def set_socketio(self, socketio):
        with self._lock:
            self.socketio = socketio

    # ── Sound control ─────────────────────────────────────

    def silence(self):
        with self._lock:
            if self.active and not self._silenced:
                self._stop_alarm_sound()
                self._silenced = True
                logger.info("Alarm silenced — mismatches remain, resolution in progress")

    def unsilence(self):
        with self._lock:
            if self.active and self.mismatches and self._silenced:
                self._silenced = False
                self._start_alarm_sound()
                logger.warning("Alarm unsilenced — admin quit resolution without finishing")

    # ── Mismatch tracking ─────────────────────────────────

    def trigger(self, pid: str, lid: int):
        """
        Record a new mismatch and fire the alarm if not already active.
        Callers (worker_async._process_slot) are responsible for suppressing
        this call during DVW / admin operations.
        """
        pid      = str(pid)
        snapshot = None

        with self._lock:
            if not self.active:
                self._silenced         = False
                self._alarm_start_time = time.time()
                self.active            = True
                self._start_alarm_sound()
                logger.warning("ALARM ACTIVATED")
                if self.socketio:
                    self.socketio.emit("alarm_triggered", {
                        "mismatch_count": 1,
                        "mismatches":     [[pid, lid]],
                    })

            self.mismatches.add((pid, lid))
            logger.warning(f"Mismatch added: PID={pid}, LID={lid}")
            if self.socketio:
                snapshot = list(self.mismatches)

        if snapshot is not None:
            self.socketio.emit("alarm_updated", {
                "mismatch_count": len(snapshot),
                "mismatches":     [[p, l] for p, l in snapshot],
            })

        # Non-blocking — delegates to BackgroundEncoder
        self._save_alarm_clips(pid, lid)

    def _save_alarm_clips(self, pid: str, lid: int):
        try:
            from back_end.slot_monitor.camera.rolling_buffer import (
                face_rolling_buffer, top_rolling_buffer,
            )
            face_rolling_buffer.save_alarm_clip(pid=pid, lid=lid)
            top_rolling_buffer.save_alarm_clip(pid=pid, lid=lid)
        except Exception as e:
            logger.warning(f"[Alarm] Failed to queue alarm clips: {e}")

    def resolve(self, pid: str, lid: int):
        """Remove one mismatch; clear the alarm if the set becomes empty."""
        with self._lock:
            self.mismatches.discard((pid, lid))
            logger.info(f"Mismatch resolved: PID={pid}, LID={lid}")
            if self.active and not self.mismatches:
                self._clear_active_alarm()

    def stop_if_clear(self):
        """Clear alarm when all mismatches have been resolved externally."""
        with self._lock:
            if self.active and not self.mismatches:
                self._clear_active_alarm()

    def clear(self):
        """Admin override — discard all mismatches and clear alarm."""
        with self._lock:
            count = len(self.mismatches)
            self.mismatches.clear()
            if self.active:
                self._clear_active_alarm()
            logger.info(f"Alarm force-cleared ({count} mismatches removed)")

    def _clear_active_alarm(self):
        """Must be called with self._lock held."""
        if not self._silenced:
            self._stop_alarm_sound()
        duration               = time.time() - self._alarm_start_time if self._alarm_start_time else 0
        self.active            = False
        self._silenced         = False
        self._alarm_start_time = None
        logger.info(f"ALARM CLEARED after {duration:.1f}s")
        if self.socketio:
            self.socketio.emit("alarm_cleared", {})

    # ── Admin auth ────────────────────────────────────────

    def authenticate_admin(self, password: str) -> dict:
        # TODO: Replace with proper authentication
        if password == Secrets.ADMIN_PASSWORD:
            with self._lock:
                snapshot = list(self.mismatches)
            return {
                "authenticated": True,
                "mismatches":    sorted([(str(p), l) for p, l in snapshot]),
            }
        return {"authenticated": False, "mismatches": []}

    def get_status(self) -> dict:
        with self._lock:
            return {
                "active":         self.active,
                "silenced":       self._silenced,
                "mismatch_count": len(self.mismatches),
                "duration":       time.time() - self._alarm_start_time if self._alarm_start_time else 0,
            }

    # ── Hardware interface ────────────────────────────────

    def _start_alarm_sound(self):
        print("ALARM ON")
        logger.warning("Physical alarm started")

    def _stop_alarm_sound(self):
        print("ALARM OFF")
        logger.info("Physical alarm stopped")