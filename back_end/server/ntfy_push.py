# ============================================================
# FILE: back_end/server/ntfy_push.py
# ============================================================
"""
LAN-native push notifications via self-hosted ntfy server.

ntfy (https://ntfy.sh) is a simple HTTP-based pub/sub service you can
self-host with one Docker command — no Firebase, no internet, no API keys.

Architecture
────────────
  AlarmController.trigger()  →  NtfyPush.send_alarm()  →  HTTP POST ntfy server
  Flutter NtfyService         →  HTTP GET/SSE ntfy topic  →  show notification

Intranet setup (one command)
────────────────────────────
  docker run -d --name ntfy -p 80:80 \\
    -v /var/cache/ntfy:/var/cache/ntfy \\
    -v /etc/ntfy:/etc/ntfy \\
    binwiederhier/ntfy serve

  # Access at http://ntfy.phonebox.local/ (add DNS/hosts entry for that name)

Config in back_end/config.py → NtfyConfig:
  ENABLED     bool   False = disabled (default); True = send pushes
  SERVER_URL  str    "http://ntfy.phonebox.local"
  TOPIC       str    "phonebox-alarms"
  TIMEOUT_S   float  3.0

Wire-up (server_main.py, after alarm is created):
  from back_end.server.ntfy_push import NtfyPush
  ntfy = NtfyPush()
  if ntfy.enabled:
      slot_monitor.alarm.set_ntfy_push(ntfy)
"""

import json
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config import with defaults ───────────────────────────────────────────────
try:
    from back_end.config import NtfyConfig as _NC
    _ENABLED    = _NC.ENABLED
    _SERVER_URL = _NC.SERVER_URL.rstrip("/")
    _TOPIC      = _NC.TOPIC
    _TIMEOUT_S  = _NC.TIMEOUT_S
except AttributeError:
    _ENABLED    = False
    _SERVER_URL = "http://ntfy.phonebox.local"
    _TOPIC      = "phonebox-alarms"
    _TIMEOUT_S  = 3.0


class NtfyPush:
    """
    Sends alarm push notifications to a self-hosted ntfy server.

    Thread-safe. All sends are fire-and-forget in a daemon thread so
    alarm_controller.trigger() is never blocked by a slow ntfy server.

    Usage:
        ntfy = NtfyPush()
        if ntfy.enabled:
            ntfy.send_alarm(mismatch_count=2, mismatches=[["pid1", 3]])
    """

    def __init__(self):
        self._url     = f"{_SERVER_URL}/{_TOPIC}"
        self.enabled  = _ENABLED

        if self.enabled:
            logger.info(
                f"[NtfyPush] Enabled — topic: {self._url}"
            )
        else:
            logger.info(
                "[NtfyPush] Disabled (set NtfyConfig.ENABLED=True in config.py "
                "to enable LAN push notifications)"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def send_alarm(self, mismatch_count: int, mismatches: list) -> None:
        """
        Send alarm-triggered notification.
        Fire-and-forget: returns immediately; network call is async.
        """
        if not self.enabled:
            return

        slot_list = ", ".join(
            f"slot {int(m[1]) + 1}" for m in mismatches[:4]
        )
        if len(mismatches) > 4:
            slot_list += f" +{len(mismatches) - 4} more"

        title = f"⚠️ PhoneBox Alarm — {mismatch_count} mismatch{'es' if mismatch_count != 1 else ''}"
        body  = f"Mismatched: {slot_list}" if slot_list else "Check PhoneBox immediately."

        self._send_async(
            title    = title,
            message  = body,
            priority = "urgent",
            tags     = ["rotating_light"],
        )

    def send_alarm_cleared(self) -> None:
        """Send alarm-cleared notification."""
        if not self.enabled:
            return
        self._send_async(
            title    = "✅ PhoneBox Alarm Cleared",
            message  = "All slots are back to their expected state.",
            priority = "default",
            tags     = ["white_check_mark"],
        )

    def send_test(self) -> bool:
        """
        Synchronous test — returns True if ntfy server responded 2xx.
        Called from /api/debug/test_ntfy endpoint.
        """
        return self._send_sync(
            title   = "PhoneBox ntfy test",
            message = "Push notifications are working.",
            priority= "default",
            tags    = ["loudspeaker"],
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send_async(self, title: str, message: str,
                    priority: str = "default", tags: Optional[list] = None) -> None:
        """Submit the HTTP POST to a daemon thread — non-blocking."""
        t = threading.Thread(
            target=self._send_sync,
            args=(title, message, priority, tags),
            daemon=True,
            name="NtfyPush",
        )
        t.start()

    def _send_sync(self, title: str, message: str,
                   priority: str = "default", tags: Optional[list] = None) -> bool:
        """
        Execute the HTTP POST to ntfy. Returns True on success.
        Uses urllib (stdlib) — no extra dependencies.
        """
        import urllib.request
        import urllib.error

        payload = message.encode()
        headers = {
            "Title":         title,
            "Priority":      priority,
            "Content-Type":  "text/plain",
        }
        if tags:
            headers["Tags"] = ",".join(tags)

        try:
            req = urllib.request.Request(
                self._url,
                data    = payload,
                headers = headers,
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                ok = 200 <= resp.status < 300
                if ok:
                    logger.debug(f"[NtfyPush] Sent: {title}")
                else:
                    logger.warning(
                        f"[NtfyPush] Server returned {resp.status} for: {title}"
                    )
                return ok
        except urllib.error.URLError as e:
            logger.warning(
                f"[NtfyPush] Cannot reach ntfy server at {self._url}: {e}. "
                "Is ntfy running? docker run -d -p 80:80 binwiederhier/ntfy serve"
            )
            return False
        except Exception as e:
            logger.warning(f"[NtfyPush] Unexpected error: {e}")
            return False
