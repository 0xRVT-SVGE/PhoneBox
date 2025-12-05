import threading
from queue import Queue
import time

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
        self.stop_requested = False

        self.no_badge_timeout = 10  # seconds
        self._last_barcode_time = time.time()

        self._scan_callbacks = []

        self._socketio = None  # <-- socketio placeholder

    def set_socketio(self, sio):
        self._socketio = sio

    # ---------------- RAW FRAME ----------------
    def set_rframe(self, frame):
        with self._rframe_lock:
            self._latest_rframe = frame if frame is not None else None

    def get_rframe(self):
        with self._rframe_lock:
            return self._latest_rframe if self._latest_rframe is not None else None

    # ---------------- FRAME WITH ROI ------------
    def set_frame(self, frame):
        with self._frame_lock:
            self._latest_frame = frame if frame is not None else None

    def get_frame(self):
        with self._frame_lock:
            return self._latest_frame if self._latest_frame is not None else None

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

    def emit_scan_status(self):
        if self._socketio is not None:
            try:
                # Use start_background_task for thread safety
                self._socketio.start_background_task(self._emit_socket)
            except Exception:
                pass

        # Call all local callbacks
        for cb in self._scan_callbacks:
            try:
                cb(self._scan_results.copy())
            except Exception:
                pass

    def _emit_socket(self):
        self._socketio.emit("scan_status", {
            "running": self._scan_request["running"],
            "authorized": self._auth_status["authorized"],
            "user": self._auth_status["user"],
            "face_verified": self._scan_results["face_verified"],
            "barcode_verified": self._scan_results["barcode_verified"],
            "current_name": self._scan_results["current_name"],
            "badge_timeout_exceeded": self._scan_results["badge_timeout_exceeded"],
        }, namespace="/")

    # ---- Preview async methods ----
    def request_preview(self):
        self.photo_taken_event.clear()
        self.preview_requested.set()

    def stop_preview(self):
        self.preview_requested.clear()

    def mark_photo_taken(self):
        self.photo_taken_event.set()


scanner_state = ScannerState()
