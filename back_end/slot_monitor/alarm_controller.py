import threading
import time
import logging

logger = logging.getLogger(__name__)


class AlarmController:
    """
    Centralized alarm state management.
    Thread-safe. No DB logic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.active = False
        self.mismatches: set[tuple[str, int]] = set()  # (pid, lid)
        self._alarm_start_time = None

    def trigger(self, pid: str, lid: int):
        """Trigger alarm for a specific phone/location mismatch."""
        with self._lock:
            if not self.active:
                self._start_alarm_sound()
                self.active = True
                self._alarm_start_time = time.time()
                logger.warning("ALARM ACTIVATED")

            self.mismatches.add((pid, lid))
            logger.warning(f"Mismatch added: PID={pid}, LID={lid}")

    def stop_if_clear(self, any_mismatch_left: bool):
        """Stop alarm if no mismatches remain."""
        with self._lock:
            if self.active and not any_mismatch_left:
                self._stop_alarm_sound()
                duration = time.time() - self._alarm_start_time if self._alarm_start_time else 0
                self.active = False
                self._alarm_start_time = None
                logger.info(f"ALARM CLEARED after {duration:.1f}s")

    def _get_mismatches_list(self):
        mismatches_as_strings = [(str(pid), lid) for pid, lid in self.mismatches]
        return sorted(mismatches_as_strings)  # Now safely sorted

    def authenticate_admin(self, password: str) -> dict:
        """
        Admin authentication to view mismatches.

        Returns:
            {
                "authenticated": bool,
                "mismatches": list[(pid, lid)]
            }
        """
        # TODO: Replace with proper authentication
        if password == "admin":
            with self._lock:
                logger.info("Admin authenticated, viewing mismatches")
                return {
                    "authenticated": True,
                    "mismatches": self._get_mismatches_list()
                }
        return {
            "authenticated": False,
            "mismatches": []
        }

    def clear(self):
        """Clear all mismatches (admin override)."""
        with self._lock:
            count = len(self.mismatches)
            self.mismatches.clear()
            if self.active:
                self._stop_alarm_sound()
                self.active = False
                self._alarm_start_time = None
            logger.info(f"Cleared {count} mismatches")

    def get_status(self) -> dict:
        """Get current alarm status."""
        with self._lock:
            return {
                "active": self.active,
                "mismatch_count": len(self.mismatches),
                "duration": time.time() - self._alarm_start_time if self._alarm_start_time else 0
            }

    # ------------------------------------------------------------
    # ALARM HARDWARE INTERFACE
    # ------------------------------------------------------------

    def _start_alarm_sound(self):
        """Start physical alarm (buzzer, LED, etc.)."""
        # TODO: Implement actual hardware control
        print("🚨 ALARM ON 🚨")
        logger.warning("Physical alarm started")

    def _stop_alarm_sound(self):
        """Stop physical alarm."""
        # TODO: Implement actual hardware control
        print("✅ ALARM OFF")
        logger.info("Physical alarm stopped")