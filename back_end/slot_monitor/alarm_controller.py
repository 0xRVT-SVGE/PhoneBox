# ============================================================
# FILE: back_end/slot_monitor/alarm_controller.py
# ============================================================
import threading
import time
import logging

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

    Clip saving:
        Rolling-buffer clips are saved in a background daemon thread so that
        the async worker loop is NEVER blocked by disk I/O.  Before the fix,
        save_alarm_clip() was called synchronously inside trigger(), stalling
        the event loop for ~30 s per alarm and causing subsequent alarms to
        fire one-by-one with 30 s gaps instead of simultaneously.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.active = False
        self.mismatches: set[tuple[str, int]] = set()
        self._alarm_start_time = None
        self._silenced = False
        self.socketio = None

    def set_socketio(self, socketio):
        with self._lock:
            self.socketio = socketio

    # ──────────────────────────────────────────────────────
    # SOUND CONTROL
    # ──────────────────────────────────────────────────────

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

    # ──────────────────────────────────────────────────────
    # MISMATCH TRACKING
    # ──────────────────────────────────────────────────────

    def trigger(self, pid: str, lid: int):
        pid = str(pid)
        snapshot = None
        with self._lock:
            if not self.active:
                self._silenced = False
                self._start_alarm_sound()
                self.active = True
                self._alarm_start_time = time.time()
                logger.warning("ALARM ACTIVATED")
                if self.socketio:
                    self.socketio.emit("alarm_triggered", {
                        "mismatch_count": 1,
                        "mismatches": [[pid, lid]],
                    })

            self.mismatches.add((pid, lid))
            logger.warning(f"Mismatch added: PID={pid}, LID={lid}")
            # Take a snapshot while we hold the lock; build the list outside.
            if self.socketio:
                snapshot = list(self.mismatches)

        if snapshot is not None:
            self.socketio.emit("alarm_updated", {
                "mismatch_count": len(snapshot),
                "mismatches": [[p, l] for p, l in snapshot],
            })

        # Save rolling-buffer clips in a background thread.
        # This MUST be outside the lock and non-blocking — each clip can take
        # 1–30 s to encode, and calling it synchronously stalls the async
        # worker event loop, causing subsequent alarm triggers to queue up
        # 30 s apart instead of firing immediately.
        threading.Thread(
            target=self._save_alarm_clips,
            args=(pid, lid),
            daemon=True,
            name=f"AlarmClip-{pid[:8]}-lid{lid}",
        ).start()

    def _save_alarm_clips(self, pid: str, lid: int):
        """
        Save face + top rolling-buffer clips for one alarm event.
        Runs in a daemon thread — never blocks trigger() or the event loop.
        """
        try:
            # Import lazily on first alarm (avoids circular import at module load),
            # but the module is cached by Python after the first call so subsequent
            # alarms do not pay an import cost.
            from back_end.slot_monitor.camera.rolling_buffer import (
                face_rolling_buffer, top_rolling_buffer
            )
            face_rolling_buffer.save_alarm_clip(pid=pid, lid=lid)
            top_rolling_buffer.save_alarm_clip(pid=pid, lid=lid)
        except Exception as e:
            logger.warning(f"[Alarm] Failed to save alarm clips: {e}")

    def resolve(self, pid: str, lid: int):
        with self._lock:
            self.mismatches.discard((pid, lid))
            logger.info(f"Mismatch resolved: PID={pid}, LID={lid}")
            if self.active and not self.mismatches:
                if not self._silenced:
                    self._stop_alarm_sound()
                duration = time.time() - self._alarm_start_time if self._alarm_start_time else 0
                self.active = False
                self._silenced = False
                self._alarm_start_time = None
                logger.info(f"ALARM CLEARED after {duration:.1f}s")
                if self.socketio:
                    self.socketio.emit("alarm_cleared", {})

    def stop_if_clear(self):
        with self._lock:
            if self.active and not self.mismatches:
                if not self._silenced:
                    self._stop_alarm_sound()
                duration = time.time() - self._alarm_start_time if self._alarm_start_time else 0
                self.active = False
                self._silenced = False
                self._alarm_start_time = None
                logger.info(f"ALARM CLEARED after {duration:.1f}s")
                if self.socketio:
                    self.socketio.emit("alarm_cleared", {})

    def clear(self):
        with self._lock:
            count = len(self.mismatches)
            self.mismatches.clear()
            if self.active:
                if not self._silenced:
                    self._stop_alarm_sound()
                self.active = False
                self._silenced = False
                self._alarm_start_time = None
            logger.info(f"Alarm force-cleared ({count} mismatches removed)")
            if self.socketio:
                self.socketio.emit("alarm_cleared", {})

    def authenticate_admin(self, password: str) -> dict:
        # TODO: Replace with proper authentication
        if password == "admin":
            with self._lock:
                # Copy the set inside the lock; sort outside to minimise lock hold time.
                snapshot = list(self.mismatches)
            return {
                "authenticated": True,
                "mismatches": sorted([(str(p), l) for p, l in snapshot]),
            }
        return {"authenticated": False, "mismatches": []}

    def get_status(self) -> dict:
        with self._lock:
            return {
                "active": self.active,
                "silenced": self._silenced,
                "mismatch_count": len(self.mismatches),
                "duration": time.time() - self._alarm_start_time if self._alarm_start_time else 0,
            }

    # ──────────────────────────────────────────────────────
    # HARDWARE INTERFACE
    # ──────────────────────────────────────────────────────

    def _start_alarm_sound(self):
        print("ALARM ON")
        logger.warning("Physical alarm started")

    def _stop_alarm_sound(self):
        print("ALARM OFF")
        logger.info("Physical alarm stopped")