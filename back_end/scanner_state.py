import threading
from queue import Queue
import time
from flask_socketio import emit as _emit
from Backup.back_end.config import ScannerStateConfig as _SSC

class ScannerState:
    def __init__(self):
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._main_frame_event = threading.Event()

        # --------- Managing students ----------
        self._rframe_lock = threading.Lock()
        self._latest_rframe = None
        self._preview_frame_event = threading.Event()

        # --------- PREVIEW EVENTS ----------
        # Signals for event-driven WebRTC preview
        self.preview_requested = threading.Event()
        self.photo_taken_event = threading.Event()

        self.task_queue = Queue(maxsize=1)
        self._scan_request = {"running": False}
        self._auth_status = {"authorized": False, "user": None}
        self._scan_results = {
            "face_verified": False,
            "barcode_verified": False,
            "current_name": "Idle",
            "badge_timeout_exceeded": False,
        }

        self._current_student = None
        self._current_embed = None
        self.face_lock_until = 0
        self.barcode_lock_until = 0

        self.no_badge_timeout = _SSC.NO_BADGE_TIMEOUT  # seconds
        self._last_barcode_time = time.time()

        self._scan_callbacks = []

        self._socketio = None  # <-- socketio placeholder

    def set_socketio(self, sio):
        self._socketio = sio

    # ---------------- RAW FRAME ----------------
    def set_rframe(self, frame):
        # Lock needed: scanner_loop writes, WebRTC async thread reads.
        with self._rframe_lock:
            self._latest_rframe = frame

    def get_rframe(self):
        with self._rframe_lock:
            return self._latest_rframe

    # ---------------- FRAME WITH ROI ------------
    def set_frame(self, frame):
        # Lock needed: scanner_loop writes, WebRTC async thread reads.
        with self._frame_lock:
            self._latest_frame = frame

    def get_frame(self):
        with self._frame_lock:
            return self._latest_frame

    # ---------------- PROPERTIES ----------------
    @property
    def scan_request(self):
        return self._scan_request

    @property
    def auth_status(self):
        return self._auth_status

    @property
    def scan_results(self):
        return self._scan_results

    @property
    def current_student(self):
        return self._current_student

    @current_student.setter
    def current_student(self, student):
        self._current_student = student

    @property
    def current_embed(self):
        return self._current_embed

    @current_embed.setter
    def current_embed(self, embed):
        self._current_embed = embed

    # ---------------- BARCODE TIMEOUT ----------------
    def update_last_barcode(self):
        self._last_barcode_time = time.time()

    def badge_timeout_exceeded(self):
        return (time.time() - self._last_barcode_time) > self.no_badge_timeout

    # ---------------- EMIT ----------------
    def register_callback(self, callback):
        self._scan_callbacks.append(callback)

    # In scanner_state.py

    def emit_to_client(self, client_id):
        """
        Emit to a specific client by WebSocket connection ID.

        Note: Does NOT trigger callbacks - those are for local display only.
        """
        if self._socketio is None or client_id is None:
            return
        self._socketio.emit("scan_status", self._get_status_data(), to=client_id, namespace="/")

    def emit_to_requester(self):
        """
        Emit to the requesting client (uses Flask-SocketIO context).

        Note: Does NOT trigger callbacks - those are for local display only.
        """
        _emit("scan_status", self._get_status_data())

    def emit_scan_status(self):
        """
        Emit to all connected clients (broadcast) AND trigger local callbacks.

        This is used when state changes that should update both:
        1. All WebSocket clients (network)
        2. Local display overlay (callbacks)
        """
        # Network: Emit to all WebSocket clients
        if self._socketio is not None:
            self._socketio.start_background_task(self._emit_socket)

        # Local: Trigger display callbacks
        for cb in self._scan_callbacks:
            try:
                cb(self._scan_results.copy())
            except Exception:
                pass

    def emit_with_callbacks(self, client_id=None):
        """
        Emit to client(s) AND trigger local callbacks.

        Use this when you want both network updates and local display updates.

        Args:
            client_id: Specific client to emit to (None = broadcast)
        """
        # Network: Emit to specific client or broadcast
        if client_id:
            self.emit_to_client(client_id)
        else:
            if self._socketio is not None:
                self._socketio.start_background_task(self._emit_socket)

        # Local: Trigger display callbacks
        for cb in self._scan_callbacks:
            try:
                cb(self._scan_results.copy())
            except Exception:
                pass

    def _get_status_data(self):
        # Read the two sub-dicts once each to avoid repeated attribute lookups.
        auth    = self._auth_status
        results = self._scan_results
        return {
            "running":               self._scan_request["running"],
            "authorized":            auth["authorized"],
            "user":                  auth["user"],
            "face_verified":         results["face_verified"],
            "barcode_verified":      results["barcode_verified"],
            "current_name":          results["current_name"],
            "badge_timeout_exceeded": results["badge_timeout_exceeded"],
        }

    def _emit_socket(self):
        if self._socketio is None:
            return
        self._socketio.emit("scan_status", self._get_status_data(), namespace="/")

    # ---- Preview async methods ----
    def request_preview(self):
        self.photo_taken_event.clear()
        self.preview_requested.set()

    def stop_preview(self):
        self.preview_requested.clear()

    def mark_photo_taken(self):
        self.photo_taken_event.set()


scanner_state = ScannerState()