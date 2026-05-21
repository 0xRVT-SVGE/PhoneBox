# ============================================================
# FILE: back_end/slot_monitor/redis_bridge.py
# ============================================================
"""
Opt #39 — Redis pub/sub bridge for multi-process / multi-instance alarm fanout.

Architecture
────────────
  AlarmPublisher   — runs in the MONITOR process (or the Flask server when
                     ENABLED=False).  Publishes alarm events to a Redis
                     pub/sub channel so any number of Flask server instances
                     can forward them to connected WebSocket clients.

  AlarmSubscriber  — runs as a background daemon thread inside each Flask
                     server instance.  Listens to the same channel and
                     calls socketio.emit() for each event received.

  RedisSocketIOBridge — thin shim that implements the same .emit() interface
                     as Flask-SocketIO.  Use this to pass to
                     AlarmController.set_socketio() in the standalone
                     monitor_service.py entry point so alarm_controller.py
                     is never modified.

Single-process mode (MonitorServiceConfig.ENABLED = False, default)
────────────────────────────────────────────────────────────────────
  Nothing in this module is instantiated.  AlarmController.set_socketio()
  receives the real SocketIO object and emits directly.

Multi-process mode (MonitorServiceConfig.ENABLED = True)
────────────────────────────────────────────────────────
  1. monitor_service.py starts HeadlessSlotMonitor with
     AlarmController.set_socketio(RedisSocketIOBridge(publisher)).
  2. Every socketio.emit() call in alarm_controller.py is intercepted by
     RedisSocketIOBridge.emit() and published to Redis.
  3. Each Flask server process runs AlarmSubscriber.start() on boot.
  4. AlarmSubscriber forwards every received event to its local socketio,
     which broadcasts it to all connected WebSocket clients.

Message format (JSON published on ALARM_CHANNEL):
    {"event": "alarm_triggered", "data": {"mismatch_count": 1, ...}}
"""

import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import redis as _redis_module
    _REDIS_AVAILABLE = True
except ImportError:
    _REDIS_AVAILABLE = False
    logger.warning(
        "[RedisBridge] redis-py not installed — "
        "MonitorServiceConfig.ENABLED will not work. "
        "Install with: pip install redis"
    )

from back_end.config import MonitorServiceConfig as _MSC


# ── Publisher ─────────────────────────────────────────────────────────────────

class AlarmPublisher:
    """
    Publishes alarm events from the monitor process to Redis pub/sub.

    Thread-safe: a single connection per instance; Redis publish is atomic.
    """

    def __init__(self):
        if not _REDIS_AVAILABLE:
            raise RuntimeError(
                "redis-py is not installed. "
                "Run: pip install redis"
            )
        self._r = _redis_module.Redis(
            host            = _MSC.REDIS_HOST,
            port            = _MSC.REDIS_PORT,
            db              = _MSC.REDIS_DB,
            decode_responses = True,
            socket_timeout   = 5,
            socket_connect_timeout = 5,
        )
        self._channel = _MSC.ALARM_CHANNEL
        self._ping()

    def _ping(self) -> None:
        try:
            self._r.ping()
            logger.info(
                f"[AlarmPublisher] Connected to Redis "
                f"{_MSC.REDIS_HOST}:{_MSC.REDIS_PORT}"
            )
        except Exception as e:
            logger.error(f"[AlarmPublisher] Cannot reach Redis: {e}")
            raise

    def publish(self, event: str, data: dict) -> None:
        """Publish one alarm event. Silently swallows errors (non-critical)."""
        try:
            self._r.publish(
                self._channel,
                json.dumps({"event": event, "data": data}),
            )
        except Exception as e:
            logger.warning(f"[AlarmPublisher] publish failed ({event}): {e}")


# ── SocketIO shim for standalone monitor process ──────────────────────────────

class RedisSocketIOBridge:
    """
    Drop-in replacement for Flask-SocketIO's emit interface.

    Pass this to AlarmController.set_socketio() in the standalone monitor
    process.  Every alarm_controller.sio.emit(event, data) call is forwarded
    to Redis instead of a WebSocket connection.

    Example:
        publisher = AlarmPublisher()
        bridge    = RedisSocketIOBridge(publisher)
        alarm_ctrl.set_socketio(bridge)
    """

    def __init__(self, publisher: AlarmPublisher):
        self._publisher = publisher

    def emit(self, event: str, data: Optional[dict] = None, **kwargs) -> None:
        """Intercept socketio.emit and publish to Redis instead."""
        self._publisher.publish(event, data or {})


# ── Subscriber ────────────────────────────────────────────────────────────────

class AlarmSubscriber:
    """
    Background daemon thread that subscribes to the Redis alarm channel
    and re-emits events via the local Flask-SocketIO instance.

    One instance per Flask server process.  Call .start() once at boot
    (inside create_app() when MonitorServiceConfig.ENABLED is True).
    """

    def __init__(self, socketio):
        if not _REDIS_AVAILABLE:
            raise RuntimeError(
                "redis-py is not installed. "
                "Run: pip install redis"
            )
        self._socketio = socketio
        self._stop     = threading.Event()
        self._thread:  Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target  = self._loop,
            daemon  = True,
            name    = "AlarmSubscriber",
        )
        self._thread.start()
        logger.info(
            f"[AlarmSubscriber] Listening on "
            f"{_MSC.REDIS_HOST}:{_MSC.REDIS_PORT} "
            f"channel={_MSC.ALARM_CHANNEL}"
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                r = _redis_module.Redis(
                    host             = _MSC.REDIS_HOST,
                    port             = _MSC.REDIS_PORT,
                    db               = _MSC.REDIS_DB,
                    decode_responses = True,
                    socket_timeout   = _MSC.SUBSCRIBE_TIMEOUT_S + 1,
                )
                pubsub = r.pubsub()
                pubsub.subscribe(_MSC.ALARM_CHANNEL)

                for msg in pubsub.listen():
                    if self._stop.is_set():
                        break
                    if msg["type"] != "message":
                        continue
                    try:
                        payload = json.loads(msg["data"])
                        event   = payload.get("event", "unknown")
                        data    = payload.get("data", {})
                        self._socketio.emit(event, data)
                        logger.debug(f"[AlarmSubscriber] re-emitted: {event}")
                    except Exception as e:
                        logger.warning(
                            f"[AlarmSubscriber] Failed to parse/emit: {e}"
                        )

            except Exception as e:
                if not self._stop.is_set():
                    logger.error(
                        f"[AlarmSubscriber] Redis error — reconnecting in 5s: {e}"
                    )
                    self._stop.wait(timeout=5.0)
