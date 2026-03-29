# ============================================================
# FILE: back_end/slot_monitor/ops_handler.py
# ============================================================

import logging
from flask_socketio import emit, SocketIO
from flask import request

from back_end.slot_monitor.services.operation_context import op_ctx
from back_end.slot_monitor.camera.qr_pid_reader import scan_and_validate_pid_from_buffer
from back_end.slot_monitor.camera.top_camera import top_camera
from back_end.slot_monitor.slot_operations import SlotOperations
from back_end.slot_monitor.db_interface import SlotMonitorDB
from back_end.slot_monitor.phone_tracker import create_tracker_for_operation

logger = logging.getLogger(__name__)

QR_SCAN_TIMEOUT = 15.0   # seconds for the QR scan blocking call


class DVWSocketHandler:

    def __init__(self, slot_operations: SlotOperations, socketio: SocketIO):
        self.slot_ops = slot_operations
        self.socketio = socketio

    # ══════════════════════════════════════════════════════
    # CANCEL
    # ══════════════════════════════════════════════════════

    def handle_cancel_operation(self, data: dict):
        """
        Cancel any active DVW operation for the requesting client.
        Works in all stages: waiting_qr, waiting_action, tracking.

        op_ctx.clear() sets the operation's cancel_event, which:
          - unblocks the QR scan loop within one frame (~30 ms)
          - signals the PhoneTracker thread to stop immediately
          - restores paused slots to their pre-operation state
        """
        client_id = request.sid
        op = op_ctx.get(client_id)
        if op is None:
            emit("operation_error", {"status": "error", "message": "no_active_operation"})
            return

        logger.info(
            f"[DVW] Cancel requested by client={client_id} "
            f"op_type={op.op_type} PID={op.pid} stage={op.stage}"
        )
        op_ctx.clear(client_id)   # sets cancel_event + restores slots
        emit("operation_cancelled", {"status": "success", "pid": op.pid})

    # ══════════════════════════════════════════════════════
    # DEPOSIT
    # ══════════════════════════════════════════════════════

    def handle_deposit(self, data: dict):
        client_id = request.sid
        pid = data.get("pid")
        if not pid:
            emit("operation_error", {"status": "error", "message": "missing_pid"})
            return

        pid = str(pid)

        if not SlotMonitorDB.pid_exists(pid):
            emit("operation_error", {"status": "error", "message": "pid_not_found", "pid": pid})
            return

        if SlotMonitorDB.is_phone_stored(pid):
            emit("operation_error", {"status": "error", "message": "phone_already_stored", "pid": pid})
            return

        lid = SlotMonitorDB.get_next_free_lid()
        if lid is None:
            emit("operation_error", {"status": "error", "message": "no_free_slots"})
            return

        self._pause_slot(lid)

        try:
            op_ctx.start(client_id, "deposit", pid=pid, lid=lid)
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(lid, is_occupied=False)
            return

        # Capture the top-camera background frame NOW — before the phone
        # arrives.  Stored on the operation for the tracker's frame-diff
        # detection.  May be None if top_camera has no frame yet (handled
        # gracefully in _complete_deposit).
        op = op_ctx.get(client_id)
        if op is not None:
            op.background_frame = top_camera.get_frame()

        emit("deposit_waiting_for_qr", {
            "status": "waiting",
            "pid":    pid,
            "lid":    lid,
            "slot":   lid + 1,   # 1-based for UI
            "message": f"Scan QR for phone {pid}, then place it in slot {lid + 1}",
        })

    # ══════════════════════════════════════════════════════
    # WITHDRAW
    # ══════════════════════════════════════════════════════

    def handle_withdraw(self, data: dict):
        client_id = request.sid
        pid = data.get("pid")
        if not pid:
            emit("operation_error", {"status": "error", "message": "missing_pid"})
            return

        pid = str(pid)

        lid = SlotMonitorDB.get_lid_for_pid(pid)
        if lid is None:
            emit("operation_error", {"status": "error", "message": "phone_not_in_storage", "pid": pid})
            return

        self._pause_slot(lid)

        try:
            op_ctx.start(client_id, "withdraw", pid=pid, lid=lid)
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(lid, is_occupied=True)
            return

        emit("withdraw_waiting_for_action", {
            "status":  "waiting",
            "pid":     pid,
            "lid":     lid,
            "slot":    lid + 1,
            "message": f"Remove phone {pid} from slot {lid + 1}, then scan its QR code",
        })

    # ══════════════════════════════════════════════════════
    # VERIFY
    # ══════════════════════════════════════════════════════

    def handle_verify(self, data: dict):
        client_id    = request.sid
        pid          = data.get("pid")
        original_lid = data.get("original_lid")

        if not pid or original_lid is None:
            emit("operation_error", {"status": "error", "message": "missing_parameters"})
            return

        pid          = str(pid)
        original_lid = int(original_lid)

        target_lid = SlotMonitorDB.get_lid_for_pid(pid)
        if target_lid is None:
            emit("operation_error", {
                "status":  "error",
                "message": "phone_not_registered_to_any_slot",
                "pid":     pid,
                "detail":  (
                    "This phone has no active storage record. "
                    "Use admin resolution (admin_session_start) to handle it."
                ),
            })
            return

        if target_lid != original_lid:
            blocking_pid = SlotMonitorDB.get_pid_for_lid(target_lid)
            if blocking_pid is not None and blocking_pid != pid:
                emit("operation_error", {
                    "status":       "error",
                    "message":      "swap_requires_admin_resolution",
                    "pid":          pid,
                    "original_lid": original_lid,
                    "target_lid":   target_lid,
                    "blocking_pid": blocking_pid,
                    "detail": (
                        f"Slot {target_lid + 1} is occupied by phone {blocking_pid}. "
                        "Use admin resolution."
                    ),
                })
                return

        self._pause_slot(original_lid)
        if target_lid != original_lid:
            self._pause_slot(target_lid)

        try:
            op_ctx.start(
                client_id, "verify",
                pid=pid, lid=target_lid, original_lid=original_lid,
            )
        except RuntimeError:
            emit("operation_error", {"status": "error", "message": "operation_already_active"})
            self._restore_slot(original_lid, is_occupied=True)
            if target_lid != original_lid:
                self._restore_slot(target_lid, is_occupied=False)
            return

        same_slot = (target_lid == original_lid)
        emit("verify_waiting_for_action", {
            "status":       "waiting",
            "pid":          pid,
            "original_lid": original_lid,
            "original_slot": original_lid + 1,
            "target_lid":   target_lid,
            "target_slot":  target_lid + 1,
            "same_slot":    same_slot,
            "message": (
                f"Take phone {pid} from slot {original_lid + 1}, "
                f"scan QR, place back in same slot."
                if same_slot else
                f"Take phone {pid} from slot {original_lid + 1}, "
                f"scan QR, place in slot {target_lid + 1}."
            ),
        })

    # ══════════════════════════════════════════════════════
    # QR SCANNED
    # ══════════════════════════════════════════════════════

    def handle_qr_scanned(self, data: dict):
        client_id = request.sid

        if not op_ctx.is_active(client_id):
            emit("operation_error", {"status": "error", "message": "no_active_operation"})
            return

        op = op_ctx.get(client_id)

        # Lazy-start top camera (idempotent — already started eagerly at boot)
        top_camera.start()
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(True)
        except Exception:
            pass

        # Blocking QR scan — exits immediately if cancel_event is set
        scan_result = scan_and_validate_pid_from_buffer(
            top_camera,
            timeout_sec=QR_SCAN_TIMEOUT,
            cancel_event=op.cancel_event,
        )

        self._top_buffer_idle()

        # Check if cancelled while scanning
        if op.cancel_event.is_set():
            # op_ctx.clear() was already called by handle_cancel_operation;
            # the scan just returned because the event was set.
            # Nothing more to do — operation_cancelled was already emitted.
            return

        if scan_result["status"] != "success":
            emit("operation_error", scan_result)
            op_ctx.clear(client_id)
            return

        scanned_pid = scan_result["pid"]
        if scanned_pid != op.pid:
            emit("operation_error", {
                "status":      "error",
                "message":     "pid_mismatch",
                "expected_pid": op.pid,
                "scanned_pid": scanned_pid,
            })
            op_ctx.clear(client_id)
            return

        op_ctx.qr_scanned(client_id)

        dispatch = {
            "deposit":  self._complete_deposit,
            "withdraw": self._complete_withdraw,
            "verify":   self._complete_verify,
        }
        dispatch[op.op_type](op)

    # ══════════════════════════════════════════════════════
    # COMPLETION HANDLERS
    # ══════════════════════════════════════════════════════

    def _complete_deposit(self, op):
        """
        QR confirmed for deposit.  Start the phone tracker instead of
        completing immediately.  The tracker calls _finalize_deposit()
        on success or _on_tracking_failed() on any failure.

        Falls back to immediate completion if tracker data is unavailable
        (no rois_top.json or no background frame captured).
        """
        client_id = op.client_id
        pid       = op.pid
        lid       = op.lid

        tracker = create_tracker_for_operation(op, self.socketio)

        if tracker is None:
            # No tracker available — complete in the old way
            logger.warning(
                f"[DVW] Tracker unavailable for PID={pid} lid={lid} — "
                "completing deposit without motion verification"
            )
            self._finalize_deposit(op)
            return

        # Advance op stage so cleanup thread doesn't expire it
        op_ctx.set_tracking(client_id)

        self.socketio.emit(
            "tracking_started",
            {
                "pid":  pid,
                "lid":  lid,
                "slot": lid + 1,
                "message": f"Place phone {pid} in slot {lid + 1}. Keep the QR visible.",
            },
            to=client_id,
            namespace="/",
        )

        tracker.start(
            on_success=lambda: self._finalize_deposit(op),
            on_failure=lambda reason: self._on_tracking_failed(op, reason),
        )

    def _finalize_deposit(self, op):
        """
        Called by PhoneTracker on success (or directly as fallback).
        Captures baseline, creates DB record, emits deposit_result.
        Runs from the tracker background thread — uses socketio.emit().
        """
        pid, lid, client_id = op.pid, op.lid, op.client_id

        # Baseline capture confirms phone is physically in the slot
        baseline_result = self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=True, wait_for_stable=2.0
        )
        if baseline_result["status"] != "success":
            self.socketio.emit(
                "deposit_result",
                {
                    "status":  "error",
                    "message": "baseline_capture_failed",
                    "pid": pid, "lid": lid,
                },
                to=client_id, namespace="/",
            )
            op_ctx.clear(client_id)
            return

        db_result = self.slot_ops.deposit_phone_db(pid, lid)
        if db_result["status"] != "success":
            self.socketio.emit("deposit_result", db_result, to=client_id, namespace="/")
            op_ctx.clear(client_id)
            return

        self._resume_slot(lid)
        op_ctx.complete(client_id)
        self.socketio.emit(
            "deposit_result",
            {
                "status":     "success",
                "pid":        pid,
                "lid":        lid,
                "slot":       lid + 1,
                "storage_id": db_result.get("storage_id"),
            },
            to=client_id, namespace="/",
        )

    def _on_tracking_failed(self, op, reason: str):
        """
        Called by PhoneTracker on any failure.
        Clears the operation (restores slot) and notifies the client.
        Runs from the tracker background thread.
        """
        client_id = op.client_id
        op_ctx.clear(client_id)   # restores slot, sets cancel_event

        messages = {
            "qr_lost":       "QR code disappeared before the phone reached the slot. "
                             "This may indicate a substitution attempt. Please retry.",
            "out_of_frame":  "Phone left the camera view before reaching the slot. Please retry.",
            "timeout":       "Placement timed out. Please retry.",
            "detect_timeout":"Phone not detected entering the camera view. Please retry.",
            "cancelled":     "Operation was cancelled.",
        }
        self.socketio.emit(
            "tracking_failed",
            {
                "status":  "error",
                "reason":  reason,
                "message": messages.get(reason, f"Tracking failed: {reason}"),
            },
            to=client_id, namespace="/",
        )

    def _complete_withdraw(self, op):
        pid, lid, client_id = op.pid, op.lid, op.client_id

        db_result = self.slot_ops.withdraw_phone_db(pid)
        if db_result["status"] != "success":
            emit("withdraw_result", db_result)
            op_ctx.clear(client_id)
            return

        self.slot_ops.capture_and_save_baseline(
            lid=lid, is_occupied=False, wait_for_stable=2.0
        )
        self._resume_slot(lid)
        op_ctx.complete(client_id)
        emit("withdraw_result", {
            "status":     "success",
            "pid":        pid,
            "lid":        lid,
            "slot":       lid + 1,
            "storage_id": db_result.get("storage_id"),
        })

    def _complete_verify(self, op):
        pid, original_lid, target_lid, client_id = (
            op.pid, op.original_lid, op.lid, op.client_id
        )
        same_slot = (original_lid == target_lid)

        if not same_slot:
            if not SlotMonitorDB.update_storage_lid(pid, target_lid):
                emit("verify_result", {"status": "error", "message": "database_update_failed"})
                op_ctx.clear(client_id)
                return

        self.slot_ops.capture_and_save_baseline(
            lid=target_lid, is_occupied=True, wait_for_stable=1.5
        )
        self._resume_slot(target_lid)

        if not same_slot:
            self.slot_ops.capture_and_save_baseline(
                lid=original_lid, is_occupied=False, wait_for_stable=1.5
            )
            self._resume_slot(original_lid)

        op_ctx.complete(client_id)
        emit("verify_result", {
            "status":       "success",
            "pid":          pid,
            "original_lid": original_lid,
            "target_lid":   target_lid,
            "target_slot":  target_lid + 1,
        })

    # ══════════════════════════════════════════════════════
    # SLOT HELPERS
    # ══════════════════════════════════════════════════════

    def _top_buffer_idle(self):
        try:
            from back_end.slot_monitor.camera.rolling_buffer import top_rolling_buffer
            top_rolling_buffer.set_active(False)
        except Exception:
            pass

    def _pause_slot(self, lid: int):
        if self.slot_ops.monitor and self.slot_ops.monitor.worker_pool:
            self.slot_ops.monitor.worker_pool.pause_slot(lid)

    def _resume_slot(self, lid: int):
        if self.slot_ops.monitor and self.slot_ops.monitor.worker_pool:
            self.slot_ops.monitor.worker_pool.resume_slot(lid)

    def _restore_slot(self, lid: int, is_occupied: bool):
        if self.slot_ops.monitor and self.slot_ops.monitor.worker_pool:
            self.slot_ops.monitor.worker_pool.restore_slot(lid, is_occupied)


# ══════════════════════════════════════════════════════════
# REGISTRATION
# ══════════════════════════════════════════════════════════

def register_dvw_handlers(socketio: SocketIO, slot_operations: SlotOperations):
    handler = DVWSocketHandler(slot_operations, socketio)

    @socketio.on("cancel_operation")
    def on_cancel_operation(data):
        handler.handle_cancel_operation(data)

    @socketio.on("deposit")
    def on_deposit(data):
        handler.handle_deposit(data)

    @socketio.on("withdraw")
    def on_withdraw(data):
        handler.handle_withdraw(data)

    @socketio.on("verify")
    def on_verify(data):
        handler.handle_verify(data)

    @socketio.on("qr_scanned")
    def on_qr_scanned(data):
        handler.handle_qr_scanned(data)

    @socketio.on("get_alarm_status")
    def on_get_alarm_status(data):
        client_id = request.sid
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm is None:
            emit("alarm_status", {"active": False}, to=client_id)
            return
        status = alarm.get_status()
        if status["active"]:
            with alarm._lock:
                mismatches = [[p, l] for p, l in alarm.mismatches]
            emit("alarm_status", {
                "active":         True,
                "mismatch_count": len(mismatches),
                "mismatches":     mismatches,
            }, to=client_id)
        else:
            emit("alarm_status", {"active": False}, to=client_id)

    @socketio.on("alarm_acknowledge")
    def on_alarm_acknowledge(data):
        client_id = request.sid
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm is None:
            emit("operation_error",
                 {"status": "error", "message": "alarm_not_available"}, to=client_id)
            return
        result = alarm.authenticate_admin(data.get("password", ""))
        if result["authenticated"]:
            alarm.silence()
            emit("alarm_acknowledge_result", {"status": "success"}, to=client_id)
        else:
            emit("alarm_acknowledge_result",
                 {"status": "error", "message": "wrong_password"}, to=client_id)

    @socketio.on("alarm_unsilence")
    def on_alarm_unsilence(data):
        alarm = slot_operations.monitor.alarm if slot_operations.monitor else None
        if alarm:
            alarm.unsilence()

    logger.info("DVW WebSocket handlers registered")