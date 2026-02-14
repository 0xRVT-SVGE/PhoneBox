# ============================================================
# FILE: back_end/slot_monitor/test_virtual_dvw_enhanced.py
# ============================================================
"""
Enhanced Virtual DVW Testing System

INCLUDES:
- Alarm simulation
- Edge case testing
- Concurrent operation testing
- Monitor pause/resume simulation
- Timeout scenarios
- Error injection
"""

import logging
import sys
import time
import numpy as np
from typing import Dict, Optional
import threading
from enum import Enum

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# ALARM SYSTEM (Mock)
# ============================================================

class AlarmState(Enum):
    IDLE = "idle"
    TRIGGERED = "triggered"
    CLEARED = "cleared"


class MockAlarmController:
    """
    Mock alarm controller with full functionality.

    Simulates:
    - Alarm triggers
    - Grace periods
    - Admin authentication
    - Alarm clearing
    """

    def __init__(self, socketio=None):
        self.socketio = socketio
        self.state = AlarmState.IDLE
        self.mismatches = []  # [(pid, lid), ...]
        self.trigger_time = None
        self.grace_period = 5.0
        logger.info("MockAlarmController initialized")

    def trigger(self, pid: int, lid: int):
        """Trigger alarm for mismatch"""
        if self.state == AlarmState.IDLE:
            self.state = AlarmState.TRIGGERED
            self.trigger_time = time.time()
            logger.critical(f"🚨 ALARM TRIGGERED! PID={pid}, LID={lid}")

        # Add to mismatches if not already there
        if (pid, lid) not in self.mismatches:
            self.mismatches.append((pid, lid))
            logger.warning(f"   Added to mismatches: PID={pid}, LID={lid}")

        # Emit via SocketIO if available
        if self.socketio:
            self.socketio.emit('alarm_triggered', {
                'pid': pid,
                'lid': lid,
                'timestamp': time.time()
            })

    def stop_if_clear(self, any_mismatch: bool):
        """Stop alarm if no more mismatches"""
        if not any_mismatch and self.state == AlarmState.TRIGGERED:
            self.clear()

    def clear(self):
        """Clear alarm"""
        if self.state == AlarmState.TRIGGERED:
            self.state = AlarmState.CLEARED
            duration = time.time() - self.trigger_time if self.trigger_time else 0
            logger.info(f"✅ Alarm cleared (duration: {duration:.1f}s)")
            self.mismatches.clear()
            self.trigger_time = None

            if self.socketio:
                self.socketio.emit('alarm_cleared', {
                    'timestamp': time.time(),
                    'duration': duration
                })

    def get_status(self) -> dict:
        """Get alarm status"""
        duration = 0
        if self.trigger_time:
            duration = time.time() - self.trigger_time

        return {
            'active': self.state == AlarmState.TRIGGERED,
            'state': self.state.value,
            'mismatch_count': len(self.mismatches),
            'mismatches': self.mismatches.copy(),
            'duration': duration
        }

    def authenticate_admin(self, password: str) -> dict:
        """Mock admin authentication"""
        if password == "admin":  # Mock password
            return {
                "authenticated": True,
                "mismatches": self.mismatches.copy()
            }
        return {
            "authenticated": False,
            "mismatches": []
        }

    def simulate_mismatch(self, pid: int, lid: int):
        """Manually simulate a mismatch (for testing)"""
        logger.warning(f"⚠️  Simulating mismatch: PID={pid}, LID={lid}")
        self.trigger(pid, lid)


# ============================================================
# MOCK MONITOR (Simulates WorkerPool)
# ============================================================

class MockMonitor:
    """
    Mock monitor that simulates slot monitoring.

    Tracks:
    - Paused slots
    - Monitored slots
    - Slot states
    """

    def __init__(self):
        self.paused_slots = set()
        self.monitored_slots = {}  # lid -> {"baseline": np.ndarray, "occupied": bool}
        self.alarm_controller = MockAlarmController()
        logger.info("MockMonitor initialized")

    def pause_slot(self, lid: int):
        """Pause monitoring for a slot"""
        self.paused_slots.add(lid)
        logger.info(f"⏸️  Slot {lid} paused (monitoring disabled)")

    def resume_slot(self, lid: int, baseline: np.ndarray, is_occupied: bool):
        """Resume monitoring for a slot"""
        self.paused_slots.discard(lid)
        self.monitored_slots[lid] = {
            "baseline": baseline,
            "occupied": is_occupied,
            "last_check": time.time()
        }
        logger.info(f"▶️  Slot {lid} resumed (occupied={is_occupied})")

    def remove_slot(self, lid: int):
        """Remove slot from monitoring"""
        self.paused_slots.discard(lid)
        self.monitored_slots.pop(lid, None)
        logger.info(f"❌ Slot {lid} removed from monitoring")

    def is_slot_paused(self, lid: int) -> bool:
        """Check if slot is paused"""
        return lid in self.paused_slots

    def simulate_check(self, lid: int, distance: float):
        """Simulate monitoring check (for testing)"""
        if lid not in self.monitored_slots:
            logger.warning(f"Slot {lid} not being monitored")
            return

        if self.is_slot_paused(lid):
            logger.info(f"Slot {lid} is paused, skipping check")
            return

        threshold = 0.15
        if distance > threshold:
            logger.critical(f"🚨 Mismatch detected! LID={lid}, distance={distance:.4f}")
            # Get PID from MockDatabase
            from_storage = MockDatabase.get_pid_at_lid(lid)
            pid = from_storage if from_storage else f"unknown-{lid}"
            self.alarm_controller.trigger(pid, lid)
        else:
            logger.info(f"✓ Slot {lid} OK (distance={distance:.4f})")

    def get_status(self) -> dict:
        """Get monitor status"""
        return {
            "paused_slots": list(self.paused_slots),
            "monitored_slots": len(self.monitored_slots),
            "alarm": self.alarm_controller.get_status()
        }


# ============================================================
# MOCK QR SCANNER (Enhanced with timeouts)
# ============================================================

class MockQRScanner:
    """Mock QR scanner with timeout support"""

    @staticmethod
    def scan_and_validate_pid(camera_index: int = 0, timeout: float = 10.0) -> dict:
        """
        Mock QR scan with timeout.

        Returns:
            {"status": "success", "pid": int} or error
        """
        logger.info("=" * 60)
        logger.info("MOCK QR SCANNER")
        logger.info(f"Timeout: {timeout}s")
        logger.info("=" * 60)

        try:
            # Simulate timeout by prompting with time limit message
            print(f"You have {timeout}s to enter PID...")
            pid_input = input("Enter PID to 'scan' (or 'timeout' to simulate timeout): ").strip()

            if pid_input.lower() == 'timeout':
                logger.error("❌ QR scan timeout!")
                return {
                    "status": "error",
                    "message": "qr_not_detected"
                }

            if pid_input.lower() == 'cancel':
                return {
                    "status": "error",
                    "message": "qr_not_detected"
                }

            if not pid_input.isdigit():
                logger.error("Invalid PID format")
                return {
                    "status": "error",
                    "message": "qr_not_detected"
                }

            pid = int(pid_input)

            if not MockDatabase.pid_exists(pid):
                logger.warning(f"PID {pid} not in mock database")
                return {
                    "status": "error",
                    "message": "pid_not_found",
                    "pid": pid
                }

            logger.info(f"✅ Mock scan successful: PID={pid}")
            return {
                "status": "success",
                "pid": pid
            }

        except Exception as e:
            logger.error(f"Mock scan error: {e}")
            return {
                "status": "error",
                "message": "scan_error"
            }


# ============================================================
# MOCK DATABASE (Enhanced)
# ============================================================

class MockDatabase:
    """Enhanced mock database"""

    _phones = {
        123: {"pid": 123, "imei": "123456789", "model": "iPhone 12"},
        456: {"pid": 456, "imei": "987654321", "model": "Samsung S21"},
        789: {"pid": 789, "imei": "555555555", "model": "Pixel 6"},
    }

    _storage = {}
    _baselines = {}
    _operation_lock = threading.Lock()  # For testing concurrent operations

    @staticmethod
    def pid_exists(pid: int) -> bool:
        return pid in MockDatabase._phones

    @staticmethod
    def get_next_free_lid() -> Optional[int]:
        with MockDatabase._operation_lock:
            for lid in range(10):
                if lid not in MockDatabase._storage:
                    return lid
        return None

    @staticmethod
    def get_lid_for_pid(pid: int) -> Optional[int]:
        for lid, data in MockDatabase._storage.items():
            if data["pid"] == pid:
                return lid
        return None

    @staticmethod
    def get_pid_at_lid(lid: int) -> Optional[int]:
        """Get PID stored at a specific LID"""
        if lid in MockDatabase._storage:
            return MockDatabase._storage[lid]["pid"]
        return None

    @staticmethod
    def deposit(pid: int, lid: int) -> bool:
        """Thread-safe deposit"""
        with MockDatabase._operation_lock:
            if lid in MockDatabase._storage:
                logger.error(f"❌ Slot {lid} already occupied!")
                return False

            MockDatabase._storage[lid] = {
                "pid": pid,
                "stored_at": time.time()
            }
            logger.info(f"MockDB: Deposited PID={pid} at LID={lid}")
            return True

    @staticmethod
    def withdraw(pid: int) -> Optional[int]:
        """Thread-safe withdrawal"""
        with MockDatabase._operation_lock:
            lid = MockDatabase.get_lid_for_pid(pid)
            if lid is not None:
                del MockDatabase._storage[lid]
                logger.info(f"MockDB: Withdrawn PID={pid} from LID={lid}")
                return lid
        return None

    @staticmethod
    def get_storage_status():
        return {
            "occupied": len(MockDatabase._storage),
            "free": 10 - len(MockDatabase._storage),
            "storage": MockDatabase._storage.copy()
        }

    @staticmethod
    def reset():
        with MockDatabase._operation_lock:
            MockDatabase._storage.clear()
            MockDatabase._baselines.clear()
        logger.info("MockDB: Reset complete")


# ============================================================
# MOCK SLOT OPERATIONS (Enhanced)
# ============================================================

class MockSlotOperations:
    """Enhanced slot operations with monitor integration"""

    def __init__(self):
        self.monitor = MockMonitor()
        self.embedder = None
        logger.info("MockSlotOperations initialized")

    def set_monitor(self, monitor):
        self.monitor = monitor
        logger.info("Monitor attached")

    def set_embedder(self, embedder):
        self.embedder = embedder
        logger.info("Embedder attached")

    def deposit_phone_db(self, pid: int, lid: int) -> dict:
        """Deposit with concurrent safety"""
        if not MockDatabase.pid_exists(pid):
            return {"status": "error", "message": "Phone not found"}

        success = MockDatabase.deposit(pid, lid)
        if not success:
            return {"status": "error", "message": f"Location {lid} already occupied"}

        return {
            "status": "success",
            "message": "Phone deposited successfully",
            "pid": pid,
            "lid": lid,
            "storage_id": lid
        }

    def withdraw_phone_db(self, pid: int) -> dict:
        lid = MockDatabase.get_lid_for_pid(pid)

        if lid is None:
            return {"status": "error", "message": "Phone not in storage"}

        MockDatabase.withdraw(pid)

        return {
            "status": "success",
            "message": "Phone withdrawn successfully",
            "pid": pid,
            "lid": lid,
            "storage_id": lid
        }

    def capture_and_save_baseline(self, lid: int, is_occupied: bool, wait_for_stable: float = 2.0) -> dict:
        logger.info(f"Capturing baseline for LID={lid}, occupied={is_occupied}")

        # Simulate wait
        time.sleep(wait_for_stable)

        # Generate mock baseline
        baseline = np.random.rand(512).astype(np.float32)
        MockDatabase._baselines[lid] = baseline

        # Resume monitoring
        if self.monitor:
            self.monitor.resume_slot(lid, baseline, is_occupied)

        return {
            "status": "success",
            "message": "Baseline captured successfully",
            "baseline": baseline
        }


# ============================================================
# ENHANCED DVW HANDLER
# ============================================================

class EnhancedDVWHandler:
    """Enhanced DVW handler with full edge case testing"""

    def __init__(self, slot_ops: MockSlotOperations):
        self.slot_ops = slot_ops
        logger.info("EnhancedDVWHandler initialized")

    def test_deposit(self):
        """Test deposit with monitor pause/resume"""
        logger.info("\n" + "=" * 60)
        logger.info("TESTING: DEPOSIT OPERATION")
        logger.info("=" * 60)

        pid_input = input("Enter PID to deposit: ").strip()
        if not pid_input.isdigit():
            logger.error("Invalid PID")
            return

        pid = int(pid_input)

        if not MockDatabase.pid_exists(pid):
            logger.error(f"PID {pid} not found")
            logger.info(f"Available: {list(MockDatabase._phones.keys())}")
            return

        lid = MockDatabase.get_next_free_lid()
        if lid is None:
            logger.error("No free slots")
            return

        logger.info(f"✅ Target slot: LID={lid}")

        # PAUSE MONITORING
        self.slot_ops.monitor.pause_slot(lid)
        logger.info(f"⏸️  Monitoring paused for LID={lid}")

        logger.info(f"Scan QR for PID {pid}, then place in slot {lid}")

        scan_result = MockQRScanner.scan_and_validate_pid(timeout=10.0)

        if scan_result["status"] != "success":
            logger.error(f"QR scan failed: {scan_result.get('message')}")
            # RESUME ON FAILURE
            self.slot_ops.monitor.resume_slot(lid, np.zeros(512), False)
            return

        scanned_pid = scan_result["pid"]

        if scanned_pid != pid:
            logger.error(f"❌ PID MISMATCH! Expected {pid}, got {scanned_pid}")
            # RESUME ON FAILURE
            self.slot_ops.monitor.resume_slot(lid, np.zeros(512), False)
            return

        logger.info(f"✅ PID verified: {scanned_pid}")

        db_result = self.slot_ops.deposit_phone_db(pid, lid)
        if db_result["status"] != "success":
            logger.error(f"DB error: {db_result['message']}")
            self.slot_ops.monitor.resume_slot(lid, np.zeros(512), False)
            return

        baseline_result = self.slot_ops.capture_and_save_baseline(lid, is_occupied=True)

        if baseline_result["status"] != "success":
            logger.error("Baseline capture failed")
            return

        # Monitor is resumed by capture_and_save_baseline
        logger.info("=" * 60)
        logger.info(f"✅ DEPOSIT COMPLETE")
        logger.info(f"   PID: {pid}, LID: {lid}")
        logger.info(f"   Monitoring resumed with new baseline")
        logger.info("=" * 60)

    def test_withdraw(self):
        """Test withdrawal with monitor handling"""
        logger.info("\n" + "=" * 60)
        logger.info("TESTING: WITHDRAW OPERATION")
        logger.info("=" * 60)

        status = MockDatabase.get_storage_status()
        if status["occupied"] == 0:
            logger.error("No phones in storage!")
            return

        logger.info("Stored phones:")
        for lid, data in status["storage"].items():
            logger.info(f"  LID {lid}: PID {data['pid']}")

        pid_input = input("\nEnter PID to withdraw: ").strip()
        if not pid_input.isdigit():
            logger.error("Invalid PID")
            return

        pid = int(pid_input)
        lid = MockDatabase.get_lid_for_pid(pid)

        if lid is None:
            logger.error(f"PID {pid} not in storage")
            return

        logger.info(f"✅ Phone at: LID={lid}")

        # PAUSE MONITORING
        self.slot_ops.monitor.pause_slot(lid)
        logger.info(f"⏸️  Monitoring paused for LID={lid}")

        logger.info(f"Remove phone from slot {lid}, then scan QR")
        input("Press Enter when removed...")

        scan_result = MockQRScanner.scan_and_validate_pid(timeout=10.0)

        if scan_result["status"] != "success":
            logger.error(f"QR scan failed")
            # RESUME AS OCCUPIED ON FAILURE
            self.slot_ops.monitor.resume_slot(lid, np.zeros(512), True)
            return

        scanned_pid = scan_result["pid"]

        if scanned_pid != pid:
            logger.error(f"❌ PID MISMATCH! Expected {pid}, got {scanned_pid}")
            self.slot_ops.monitor.resume_slot(lid, np.zeros(512), True)
            return

        logger.info(f"✅ PID verified: {scanned_pid}")

        db_result = self.slot_ops.withdraw_phone_db(pid)
        if db_result["status"] != "success":
            logger.error(f"DB error: {db_result['message']}")
            self.slot_ops.monitor.resume_slot(lid, np.zeros(512), True)
            return

        baseline_result = self.slot_ops.capture_and_save_baseline(lid, is_occupied=False)

        # REMOVE FROM MONITORING (slot now empty)
        self.slot_ops.monitor.remove_slot(lid)

        logger.info("=" * 60)
        logger.info(f"✅ WITHDRAWAL COMPLETE")
        logger.info(f"   PID: {pid}, LID: {lid}")
        logger.info(f"   Slot removed from monitoring")
        logger.info("=" * 60)

    def test_concurrent_deposit(self):
        """Test concurrent deposit to same slot (should fail)"""
        logger.info("\n" + "=" * 60)
        logger.info("TESTING: CONCURRENT DEPOSIT (Edge Case)")
        logger.info("=" * 60)

        lid = MockDatabase.get_next_free_lid()
        if lid is None:
            logger.error("No free slots")
            return

        def attempt_deposit(pid: int, lid: int, name: str):
            logger.info(f"{name}: Attempting deposit PID={pid} to LID={lid}")
            result = MockDatabase.deposit(pid, lid)
            if result:
                logger.info(f"{name}: ✅ SUCCESS")
            else:
                logger.error(f"{name}: ❌ FAILED (slot occupied)")

        # Simulate two concurrent deposits
        t1 = threading.Thread(target=attempt_deposit, args=(123, lid, "Thread-1"))
        t2 = threading.Thread(target=attempt_deposit, args=(456, lid, "Thread-2"))

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        logger.info("Expected: One success, one failure")
        logger.info("=" * 60)

    def test_alarm_trigger(self):
        """Test alarm triggering"""
        logger.info("\n" + "=" * 60)
        logger.info("TESTING: ALARM TRIGGER")
        logger.info("=" * 60)

        # Deposit a phone first
        lid = MockDatabase.get_next_free_lid()
        if lid is None:
            logger.error("No free slots")
            return

        MockDatabase.deposit(123, lid)
        baseline = np.random.rand(512).astype(np.float32)
        self.slot_ops.monitor.resume_slot(lid, baseline, is_occupied=True)

        logger.info(f"Phone PID=123 deposited at LID={lid}")
        logger.info("Simulating mismatch...")

        # Simulate high distance (mismatch)
        self.slot_ops.monitor.simulate_check(lid, distance=0.50)  # > 0.15 threshold

        # Check alarm status
        alarm_status = self.slot_ops.monitor.alarm_controller.get_status()
        logger.info(f"\nAlarm Status:")
        logger.info(f"  Active: {alarm_status['active']}")
        logger.info(f"  Mismatches: {alarm_status['mismatch_count']}")

        logger.info("=" * 60)


# ============================================================
# ENHANCED MENU
# ============================================================

def show_menu():
    print("\n" + "=" * 60)
    print("ENHANCED DVW TEST MENU")
    print("=" * 60)
    print("BASIC OPERATIONS:")
    print("  1. Test Deposit")
    print("  2. Test Withdraw")
    print("  3. Show Storage Status")
    print("\nEDGE CASES:")
    print("  4. Test Concurrent Deposit")
    print("  5. Test Alarm Trigger")
    print("  6. Test Monitor Status")
    print("\nUTILITIES:")
    print("  7. Reset Database")
    print("  8. Add Test Phone")
    print("  9. Clear Alarms")
    print("  Q. Quit")
    print("=" * 60)


def main():
    logger.info("=" * 60)
    logger.info("ENHANCED DVW TESTING SYSTEM")
    logger.info("=" * 60)
    logger.info("✅ Alarm simulation")
    logger.info("✅ Monitor pause/resume")
    logger.info("✅ Edge case testing")
    logger.info("✅ Concurrent operation testing")
    logger.info("=" * 60)

    slot_ops = MockSlotOperations()
    handler = EnhancedDVWHandler(slot_ops)

    logger.info(f"\n✅ System initialized")
    logger.info(f"Available phones: {list(MockDatabase._phones.keys())}")

    while True:
        show_menu()
        choice = input("\nChoice: ").strip().upper()

        if choice == '1':
            handler.test_deposit()
        elif choice == '2':
            handler.test_withdraw()
        elif choice == '3':
            status = MockDatabase.get_storage_status()
            logger.info(f"\nStorage: {status['occupied']}/10 occupied")
            if status["storage"]:
                for lid, data in status["storage"].items():
                    logger.info(f"  LID {lid}: PID {data['pid']}")
        elif choice == '4':
            handler.test_concurrent_deposit()
        elif choice == '5':
            handler.test_alarm_trigger()
        elif choice == '6':
            monitor_status = slot_ops.monitor.get_status()
            logger.info("\nMonitor Status:")
            logger.info(f"  Paused slots: {monitor_status['paused_slots']}")
            logger.info(f"  Monitored slots: {monitor_status['monitored_slots']}")
            logger.info(f"  Alarm: {monitor_status['alarm']}")
        elif choice == '7':
            MockDatabase.reset()
            logger.info("✅ Database reset")
        elif choice == '8':
            pid_input = input("Enter PID: ").strip()
            if pid_input.isdigit():
                pid = int(pid_input)
                model = input("Model: ").strip()
                MockDatabase._phones[pid] = {
                    "pid": pid,
                    "imei": f"IMEI-{pid}",
                    "model": model or "Unknown"
                }
                logger.info(f"✅ Added PID {pid}")
        elif choice == '9':
            slot_ops.monitor.alarm_controller.clear()
            logger.info("✅ Alarms cleared")
        elif choice == 'Q':
            logger.info("\n✅ Exiting...")
            break
        else:
            logger.warning("Invalid choice")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n\n✅ Interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)