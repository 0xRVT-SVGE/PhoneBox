# ============================================================
# FILE: back_end/slot_monitor/alarm_controller.py
# ============================================================
import logging
import threading
import time

from back_end.secrets import Secrets
from back_end.config import AlarmConfig as _AC

logger = logging.getLogger(__name__)

_CLIP_DEBOUNCE_S = _AC.CLIP_DEBOUNCE_S


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
        filter them itself.

    Performance note:
        All socketio emits are done OUTSIDE the internal lock to avoid
        blocking monitor workers on network I/O.  A simple per-(pid,lid)
        timestamp debounce prevents the background encoder from being
        flooded when a slot re-triggers rapidly.

    Clip saving:
        Rolling-buffer clips are submitted to BackgroundEncoder (non-blocking).
    """

    def __init__(self):
        self._lock             = threading.Lock()
        self.active            = False
        self.mismatches: set[tuple[str, int]] = set()
        self._alarm_start_time = None
        self._silenced         = False
        self.socketio          = None
        self._redis_pub        = None   # Opt #39: set for multi-instance Redis fanout

        # Debounce: track last clip-save time per (pid, lid) to avoid flooding
        # the background encoder when a slot triggers multiple times in quick
        # succession (e.g. during noisy lighting conditions).
        self._last_clip_time: dict[tuple[str, int], float] = {}

    def set_socketio(self, socketio):
        with self._lock:
            self.socketio = socketio

    def set_redis_publisher(self, publisher) -> None:
        """
        Opt #39 — set a Redis publisher for multi-instance alarm fanout.

        When set, alarm events are published to Redis INSTEAD OF direct
        SocketIO emit.  All Flask instances run an AlarmSubscriber that
        re-emits the events to their local WebSocket clients.

        Pass None to revert to direct SocketIO emit.
        """
        with self._lock:
            self._redis_pub = publisher
        if publisher is not None:
            logger.info("[AlarmController] Redis pub/sub fanout enabled (Opt #39)")
        else:
            logger.info("[AlarmController] Redis pub/sub disabled — direct SocketIO emit")


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
        Register a new mismatch and (if needed) activate the alarm.

        All socketio emits happen OUTSIDE the lock so that the alarm lock
        never blocks while waiting on network I/O.  This prevents monitor
        workers from stalling each other during a burst of alarm triggers.
        """
        pid = str(pid)

        first_trigger = False
        snapshot: list | None = None
        sio = None

        with self._lock:
            sio = self.socketio

            if not self.active:
                self._silenced         = False
                self._alarm_start_time = time.time()
                self.active            = True
                self._start_alarm_sound()
                logger.warning("ALARM ACTIVATED")
                first_trigger = True

            self.mismatches.add((pid, lid))
            logger.warning(f"Mismatch added: PID={pid}, LID={lid}")
            snapshot = list(self.mismatches)

        # ── Emit OUTSIDE lock (Redis or SocketIO) ──────────────────
        if snapshot is not None:
            mismatch_list = [[p, l] for p, l in snapshot]
            redis_pub = None
            with self._lock:
                redis_pub = self._redis_pub

            if redis_pub is not None:
                # Opt #39: publish to Redis — AlarmSubscriber re-emits on all instances
                if first_trigger:
                    redis_pub.publish("alarm_triggered", {
                        "mismatch_count": len(snapshot),
                        "mismatches":     mismatch_list,
                    })
                redis_pub.publish("alarm_updated", {
                    "mismatch_count": len(snapshot),
                    "mismatches":     mismatch_list,
                })
            elif sio is not None:
                # Default: direct SocketIO emit (single-process mode)
                if first_trigger:
                    sio.emit("alarm_triggered", {
                        "mismatch_count": len(snapshot),
                        "mismatches":     mismatch_list,
                    })
                sio.emit("alarm_updated", {
                    "mismatch_count": len(snapshot),
                    "mismatches":     mismatch_list,
                })

        self._save_alarm_clips(pid, lid)

    def _save_alarm_clips(self, pid: str, lid: int):
        """
        Submit rolling-buffer snapshots to BackgroundEncoder (non-blocking).

        Debounced per (pid, lid): if a clip was already queued for this
        slot within CLIP_DEBOUNCE_S seconds, skip to avoid flooding the
        encoder queue during rapid repeated triggers.
        """
        key = (pid, lid)
        now = time.time()
        if now - self._last_clip_time.get(key, 0.0) < _CLIP_DEBOUNCE_S:
            logger.debug(f"[Alarm] Clip save debounced for PID={pid} LID={lid}")
            return
        self._last_clip_time[key] = now

        try:
            from back_end.slot_monitor.camera.rolling_buffer import (
                face_rolling_buffer, top_rolling_buffer,
            )
            face_rolling_buffer.save_alarm_clip(pid=pid, lid=lid)
            top_rolling_buffer.save_alarm_clip(pid=pid, lid=lid)
        except Exception as e:
            logger.warning(f"[Alarm] Failed to queue alarm clips: {e}")

    def resolve(self, pid: str, lid: int):
        sio       = None
        redis_pub = None
        cleared   = False

        with self._lock:
            self.mismatches.discard((pid, lid))
            logger.info(f"Mismatch resolved: PID={pid}, LID={lid}")
            if self.active and not self.mismatches:
                self._clear_active_alarm()
                cleared = True
            sio       = self.socketio
            redis_pub = self._redis_pub

        if cleared:
            if redis_pub is not None:
                redis_pub.publish("alarm_cleared", {})
            elif sio is not None:
                sio.emit("alarm_cleared", {})

    def stop_if_clear(self):
        sio       = None
        redis_pub = None
        cleared   = False
        with self._lock:
            if self.active and not self.mismatches:
                self._clear_active_alarm()
                cleared = True
            sio       = self.socketio
            redis_pub = self._redis_pub
        if cleared:
            if redis_pub is not None:
                redis_pub.publish("alarm_cleared", {})
            elif sio is not None:
                sio.emit("alarm_cleared", {})

    def clear(self):
        sio       = None
        redis_pub = None
        cleared   = False
        with self._lock:
            count = len(self.mismatches)
            self.mismatches.clear()
            if self.active:
                self._clear_active_alarm()
                cleared = True
            logger.info(f"Alarm force-cleared ({count} mismatches removed)")
            sio       = self.socketio
            redis_pub = self._redis_pub
        if cleared:
            if redis_pub is not None:
                redis_pub.publish("alarm_cleared", {})
            elif sio is not None:
                sio.emit("alarm_cleared", {})


    def _clear_active_alarm(self):
        """Must be called with self._lock held. Does NOT emit — caller handles that."""
        if not self._silenced:
            self._stop_alarm_sound()
        duration               = time.time() - self._alarm_start_time if self._alarm_start_time else 0
        self.active            = False
        self._silenced         = False
        self._alarm_start_time = None
        logger.info(f"ALARM CLEARED after {duration:.1f}s")
        # NOTE: socketio.emit("alarm_cleared") is done by the CALLER outside the lock.

    # ── Admin auth ────────────────────────────────────────

    def authenticate_admin(self, password: str) -> dict:
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