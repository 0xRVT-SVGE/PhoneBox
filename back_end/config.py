# ============================================================
# FILE: back_end/config.py
# ============================================================
"""
PhoneBox — Central Configuration
=================================
All tuneable constants in one place.  Import from here instead of
defining magic numbers inline across modules.

Credentials (DB passwords, admin password) live in back_end/secrets.py
and support environment-variable overrides — do not add them here.

Usage:
    from back_end.config import CameraConfig, TrackerConfig, ...

Sections
--------
  CameraConfig          Camera indices, resolutions, and capture backends
  DatabaseConfig        DB connection parameters and pool sizes
  ServerConfig          Flask / SocketIO host+port, debug flags
  ScannerConfig         Front-camera face+barcode scan worker
  ScannerStateConfig    Badge / auth timeout for the scanner state machine
  TrackerConfig         PhoneTracker state-machine timeouts and knobs
  MotionConfig          Motion-detection and CSRT/Nano tracker parameters
  LKConfig              Lucas-Kanade optical-flow parameters
  OrbConfig             ORB feature-matching parameters
  SlotMonitorConfig     Bottom-camera slot monitoring workers
  AlarmConfig           Mismatch / grace-period thresholds
  AdminConfig           Admin resolution session timeouts and limits
  QRConfig              QR scan timeouts
  EmbeddingConfig       Face-embedding worker pool size
  WebRTCConfig          WebRTC bitrate caps and offer timeout
  CalibrationConfig     Camera warm-up frames used by calibration tools
  RollingBufferConfig   Rolling evidence buffer sizes and fps
  EvidenceConfig        Evidence recording fps, codec, paths
  BgEncoderConfig       Background video encoder queue settings
  OverlayConfig         On-screen draw colours (BGR tuples)
"""

import os
from pathlib import Path

# ── Repository root (back_end/ is one level below this file) ──────────────
_REPO_ROOT  = Path(__file__).parent.parent
_MODELS_DIR = _REPO_ROOT / "back_end" / "models"


# ============================================================
# CAMERAS
# ============================================================

class CameraConfig:
    # ── Device indices ────────────────────────────────────
    FRONT_CAM_INDEX  = 0   # scanner_loop.py  — face + barcode
    TOP_CAM_INDEX    = 1   # top_camera.py    — QR scan + phone tracking
    BOTTOM_CAM_INDEX = 2   # headless_slot_monitor.py — slot embedding

    # ── Bottom camera (slot monitor) resolution ───────────
    BOTTOM_CAM_WIDTH  = 1280
    BOTTOM_CAM_HEIGHT = 720
    BOTTOM_CAM_FPS    = 30

    # ── Front camera (scanner loop) resolution ────────────
    FRONT_CAM_WIDTH  = 1920
    FRONT_CAM_HEIGHT = 1080

    # ── Top-camera reconnect delay (seconds) ─────────────
    TOP_CAM_RECONNECT_DELAY = 1.0

    # ── Async frame buffer max concurrent subscribers ─────
    ASYNC_FRAME_MAX_SUBSCRIBERS = 100

    # ── cv2.VideoCapture backends ─────────────────────────
    # Controls the capture API passed as the second argument to
    # cv2.VideoCapture(index, apiPreference).
    #
    # Values:
    #   "auto"  — let OpenCV choose (cv2.CAP_ANY, default behaviour).
    #             Works on most systems; use this unless you hit issues.
    #   "dshow" — Windows DirectShow (cv2.CAP_DSHOW).
    #             WARNING: dshow enumerates USB cameras in a DIFFERENT ORDER
    #             than msmf/auto. Index N under dshow may be a different
    #             physical camera than index N under msmf. If the front camera
    #             opens but face/barcode scanning doesn't work, switch to msmf.
    #   "msmf"  — Windows Media Foundation (cv2.CAP_MSMF).  Recommended for
    #             Windows. Consistent index ordering, better H.264/MJPEG support.
    #   "auto"  — Let OpenCV choose (cv2.CAP_ANY). Cross-platform default.
    #   "v4l2"  — Video4Linux2 (cv2.CAP_V4L2). Standard on Linux.
    #   "gstreamer" — GStreamer pipeline (cv2.CAP_GSTREAMER).
    #   "ffmpeg"    — FFmpeg backend (cv2.CAP_FFMPEG).
    #
    # Each camera can be set independently so you can mix backends.
    #
    # Environment-variable overrides (string, case-insensitive):
    #   PHONEBOX_CAM_BACKEND_FRONT   e.g. "msmf"
    #   PHONEBOX_CAM_BACKEND_TOP     e.g. "auto"
    #   PHONEBOX_CAM_BACKEND_BOTTOM  e.g. "auto"
    FRONT_CAM_BACKEND  = os.environ.get("PHONEBOX_CAM_BACKEND_FRONT",  "dshow").lower()
    TOP_CAM_BACKEND    = os.environ.get("PHONEBOX_CAM_BACKEND_TOP",    "msmf").lower()
    BOTTOM_CAM_BACKEND = os.environ.get("PHONEBOX_CAM_BACKEND_BOTTOM", "dshow").lower()

    # ── Backend resolver ──────────────────────────────────
    # Use CameraConfig.resolve_backend(name) to get the cv2 integer constant.
    # Import cv2 inside callers (not at config level) to avoid hard dependency.
    @staticmethod
    def resolve_backend(name: str) -> int:
        """
        Convert a backend name string to its cv2 integer constant.

        Usage in callers::

            import cv2
            from back_end.config import CameraConfig

            backend = CameraConfig.resolve_backend(CameraConfig.FRONT_CAM_BACKEND)
            cap = cv2.VideoCapture(CameraConfig.FRONT_CAM_INDEX, backend)

        Returns cv2.CAP_ANY (0) for unknown names so callers always get a
        valid integer even if cv2 is not imported here.
        """
        import cv2  # local import — cv2 may not be installed in all envs
        _MAP = {
            "auto":      cv2.CAP_ANY,
            "dshow":     cv2.CAP_DSHOW,
            "msmf":      cv2.CAP_MSMF,
            "v4l2":      cv2.CAP_V4L2,
            "gstreamer": cv2.CAP_GSTREAMER,
            "ffmpeg":    cv2.CAP_FFMPEG,
        }
        return _MAP.get(name.lower(), cv2.CAP_ANY)


# ============================================================
# DATABASE
# ============================================================

class DatabaseConfig:
    _HOST = os.environ.get("PHONEBOX_DB_HOST", "localhost")
    _PORT = int(os.environ.get("PHONEBOX_DB_PORT", "5432"))
    _NAME = os.environ.get("PHONEBOX_DB_NAME", "PhoneBoxDB")

    # ── Synchronous connection pool (psycopg2) ────────────
    SYNC_HOST     = _HOST
    SYNC_PORT     = _PORT
    SYNC_DATABASE = _NAME
    SYNC_POOL_MIN = 1
    SYNC_POOL_MAX = 10

    # B5: idle threshold — only run health-check ping when a pooled connection
    # has not been used for this many seconds. Active connections skip the
    # round-trip entirely, cutting per-query overhead in half under load.
    HEALTH_CHECK_IDLE_S = 30.0   # seconds

    # ── Async connection pool (asyncpg) ───────────────────
    ASYNC_HOST        = _HOST
    ASYNC_PORT        = _PORT
    ASYNC_DATABASE    = _NAME
    ASYNC_POOL_MIN    = 5
    ASYNC_POOL_MAX    = 20

    # A1: raised from 10.0 → 30.0 so slow HNSW scans and advisory-lock
    # waits under load don't raise asyncio.TimeoutError prematurely.
    ASYNC_CMD_TIMEOUT = 30.0


# ============================================================
# SERVER
# ============================================================

class ServerConfig:
    HOST     = os.environ.get("PHONEBOX_HOST",     "0.0.0.0")
    PORT     = int(os.environ.get("PHONEBOX_PORT", "5000"))
    # Each physical box declares its slug, which must exist in the boxes table.
    # Example: PHONEBOX_BOX_SLUG=year_1  (for the Year 1 cabinet)
    # BOX_ID is resolved at startup by server_main._resolve_box_id().
    BOX_SLUG = os.environ.get("PHONEBOX_BOX_SLUG", "box_y3")

    # Debug flags for scanner_loop
    DEBUG_ROI    = True   # Draw ROI rectangle on front-camera feed
    DEBUG_WINDOW = False  # Show cv2.imshow debug window

    # DEV_MODE: set env var PHONEBOX_DEV=1 or force True here.
    DEV_MODE_ENV_VAR = "PHONEBOX_DEV"

    # Slot monitor setup timeout — server_main waits this long for the
    # monitor's async setup to finish before starting admin handlers.
    MONITOR_SETUP_TIMEOUT = 20.0   # seconds

    # Fallback num_lids used for ROI calibration when DB is unreachable.
    # Set this to the actual number of physical slots in the cabinet so that
    # if the DB is momentarily unreachable at startup the calibration grid
    # is still drawn correctly.
    FALLBACK_NUM_LIDS = int(os.getenv("PHONEBOX_NUM_LIDS", "30"))

    # API base URL for the student lookup endpoint (scanner_worker)
    STUDENT_API_BASE    = "http://127.0.0.1:5000/api/students"
    STUDENT_API_TIMEOUT = 3   # seconds per request


# ============================================================
# SCANNER WORKER  (front camera — face + barcode)
# ============================================================

class ScannerConfig:
    # Face recognition model / backend
    FACE_MODEL            = "SFace"
    FACE_DETECTOR_BACKEND = "opencv"

    # Minimum cosine similarity [0–1] to accept a face match
    SIMILARITY_THRESHOLD = 0.5

    # How long a successful badge scan stays valid (seconds)
    BADGE_VALID_TIME = 7   # seconds

    # Minimum interval between consecutive face / barcode detections
    FACE_INTERVAL    = 0.5   # seconds
    BARCODE_INTERVAL = 0.5   # seconds

    # Frame width used for DeepFace inference (downscaled from full res)
    SCALED_WIDTH = 720   # pixels

    # Rolling evidence buffer — push every Nth front-camera frame
    # (30 fps camera → N=2 → ~15 fps recorded, halves JPEG-encode load)
    FACE_BUF_EVERY_N = 2


# ============================================================
# SCANNER STATE  (badge / auth timeout)
# ============================================================

class ScannerStateConfig:
    # Seconds without a barcode scan before the badge is considered absent.
    # Triggers badge_timeout_exceeded() → scan failure path.
    NO_BADGE_TIMEOUT = 10   # seconds


# ============================================================
# PHONE TRACKER  (top camera — state-machine timeouts)
# ============================================================

class TrackerConfig:
    # Time allowed for motion detection to find the phone after QR scan (s)
    DETECT_TIMEOUT          = 8.0

    # Total time window from TRACKING → SUCCESS before giving up (s)
    PLACEMENT_TIMEOUT       = 35.0

    # Max time in INSERTING state before insertion_timeout failure (s)
    INSERTION_TIMEOUT       = 8.0

    # Max time in ENTERING/STABILIZING before stabilization_timeout (s)
    TRACKER_SUCCESS_TIMEOUT = 12.0

    # Time in STABILIZING state before calling verify_fn (not a timeout,
    # the verifier is called on the very first STABILIZING frame)
    STABILIZING_TIMEOUT     = 3.0

    # ── QR visibility ─────────────────────────────────────
    QR_CHECK_EVERY_N  = 6
    QR_ABSENT_FAIL_S  = 3.0

    # ── ROI approach gate ─────────────────────────────────
    ROI_APPROACH_MARGIN = 0.15
    ROI_APPROACH_FRAMES = 3

    # ── Rotation / insertion detection ────────────────────
    AREA_REDUCTION_TRIGGER = 0.52
    ANGLE_SWING_TRIGGER    = 28

    # Minimum bounding-box area (px²) to accept as a valid track target
    MIN_TRACK_AREA_PX = 800

    # ── Stillness detector ────────────────────────────────
    STILL_VEL_THRESHOLD   = 12
    STILL_REQUIRED_FRAMES = 8
    STILL_PENALTY_ON_MOVE = 2

    STAGING_HOLD_TIME = 1.5   # seconds
    EMIT_INTERVAL     = 0.5   # seconds between tracking_update events

    # ── QR positional anchor ───────────────────────────────
    # Whenever the QR is successfully decoded the tracker bbox centroid is
    # compared against the QR centroid.  Three zones:
    #
    #   drift < QR_ANCHOR_BLEND_DIST   → trust tracker, no correction
    #   drift in [BLEND_DIST, REINIT_DIST) → soft centroid blend (65% QR)
    #   drift ≥ QR_ANCHOR_REINIT_DIST  → full tracker reinit at QR position
    #
    # Units: pixels in the top-camera frame (default 1280×720 or similar).
    QR_ANCHOR_BLEND_DIST  = 20   # px — below this, tracker is trusted
    QR_ANCHOR_REINIT_DIST = 60   # px — above this, reinit tracker

    # ── Opt #9: TrackerNano backend ───────────────────────
    # "nano"  — use TrackerNano (opencv-contrib >= 4.7, ~3-5× lighter than CSRT)
    # "csrt"  — always use CSRT (original behaviour)
    # "auto"  — try Nano first; fall back to CSRT if models missing or OpenCV
    #           build does not include contrib trackers (default, safest)
    TRACKER_BACKEND = "auto"

    # Paths to the two TrackerNano ONNX model files.
    # Run back_end/slot_monitor/tools/download_tracker_models.py once to
    # populate these files.  On intranet: download on an internet machine,
    # then scp the back_end/models/ directory to the server.
    NANO_BACKBONE_PATH: str = str(_MODELS_DIR / "nanotrack_backbone_sim.onnx")
    NANO_NECKHEAD_PATH: str = str(_MODELS_DIR / "nanotrack_head_sim.onnx")

    # Debug / display
    # Set True to draw the phone bounding box on the top-camera feed.
    # Useful during development; can be disabled in production.
    DRAW_TRACKING_BOX = True


# ============================================================
# MOTION DETECTION  (frame-diff used inside PhoneTracker)
# ============================================================

class MotionConfig:
    BLUR_K   = 15
    THRESH   = 20
    DILATE   = 5       # was 3; larger dilation bridges gaps between parts of
                       # the same phone (screen, bezel, QR sticker) so they
                       # merge into one contour before bounding-box extraction.
    MIN_AREA = 1500
    IOU_MERGE = 0.20

    # If True, _motion_bbox merges ALL contours above (MIN_AREA // 4) into a
    # single unified bounding box instead of returning only the largest one.
    # This ensures the box covers the whole phone, not just the largest piece
    # (e.g. not just the QR sticker when the phone edge moves less).
    # Set to False to restore the legacy largest-contour-only behaviour.
    MOTION_MERGE_ALL = True

    # Opt #9: TrackerNano is statistically more stable than CSRT between
    # reinitializations, so we can reinit less aggressively.
    # CSRT was reinitialised every 12 frames; Nano every 20.
    # In "auto" mode _make_tracker() sets this at runtime.
    CSRT_REINIT_INTERVAL = 12   # used when backend == "csrt"
    NANO_REINIT_INTERVAL = 20   # used when backend == "nano"

    # Opt #13: run motion detect every N frames when primary tracker is healthy
    CSRT_MOTION_GATE_N = 5


# ============================================================
# LUCAS-KANADE OPTICAL FLOW  (fallback tracker layer)
# ============================================================

class LKConfig:
    MAX_POINTS   = 20
    MIN_POINTS   = 5
    GOOD_QUALITY = 0.25
    WIN_SIZE     = (17, 17)
    MAX_LEVEL    = 2
    CRITERIA     = (0x02 | 0x01, 20, 0.03)


# ============================================================
# ORB RE-IDENTIFICATION  (last-resort tracker layer)
# ============================================================

class OrbConfig:
    MATCH_THRESHOLD = 0.75
    MIN_MATCHES     = 10
    RE_ID_EVERY_N   = 15


# ============================================================
# SLOT MONITOR  (bottom camera — embedding workers)
# ============================================================

class SlotMonitorConfig:
    # Number of async worker coroutines sharing the slot list
    NUM_WORKERS = 4

    # Embedding distance above which a slot is considered "changed"
    MISMATCH_THRESHOLD = 0.15

    # Embedding distance drift that triggers a soft baseline recalibration
    RECALC_THRESHOLD = 0.05

    # How long (seconds) a slot must stay "mismatch" before the alarm fires
    GRACE_PERIOD = 3.0

    # ── Distance history sliding window ───────────────────
    # Size of the per-slot deque that stores recent embedding distances.
    DISTANCE_HISTORY_MAXLEN = 10

    # Minimum samples in the history before recalibration is considered.
    # Also the window size used to evaluate recent distances.
    RECALC_MIN_SAMPLES = 5

    # Status-reporter interval (seconds between periodic log summaries)
    STATUS_REPORT_INTERVAL = 30   # seconds

    # Stop-event poll interval (seconds)
    STOP_POLL_INTERVAL = 0.25   # seconds

    # Camera boot-up: wait up to this long for the first frame (seconds)
    CAMERA_INIT_TIMEOUT = 5.0

    # Camera warm-up frames to discard before monitoring begins
    CAMERA_WARMUP_FRAMES = 10

    # Grid spacing (px) used in auto-generated fallback ROI grid
    GRID_SPACING = 10


# ============================================================
# ALARM & SLOT-CHANGE VERIFICATION
# ============================================================

class AlarmConfig:
    # Admin password is in back_end.secrets.Secrets.ADMIN_PASSWORD

    # Minimum cosine distance between the "before" embedding (captured at
    # operation start) and the "after" embedding (captured at operation end)
    # that must be exceeded to confirm the slot physically changed.
    SLOT_CHANGE_THRESHOLD = 0.07

    # Minimum seconds between saving alarm clips for the same (pid, lid).
    CLIP_DEBOUNCE_S = 30.0


# ============================================================
# ADMIN RESOLUTION SESSION
# ============================================================

class AdminConfig:
    # Base session lifetime before expiry (seconds).
    SESSION_TIMEOUT = 30   # 2 minutes

    # Extra seconds added per resolved mismatch.
    SESSION_EXTEND_PER_RESOLVE = 30

    # Watchdog poll interval (seconds)
    WATCHDOG_POLL_INTERVAL = 5.0

    # Maximum seconds for a QR scan during an admin resolution operation.
    ADMIN_QR_SCAN_TIMEOUT = 90.0

    # Seconds after admin_remove_ok before the "No QR on this object" button
    # appears in the frontend.
    NO_QR_BUTTON_DELAY_S = 25


# ============================================================
# QR SCANNING
# ============================================================

class QRConfig:
    # Maximum seconds to wait for a valid QR scan during DVW operations
    DVW_SCAN_TIMEOUT = 15.0

    # QR frame-buffer poll timeout (seconds per iteration)
    BUFFER_POLL_TIMEOUT = 0.05


# ============================================================
# FACE EMBEDDING WORKER
# ============================================================

class EmbeddingConfig:
    MAX_WORKERS = 2


# ============================================================
# WEBRTC
# ============================================================

class WebRTCConfig:
    # Opt #23: per-mode bitrate caps.
    ADMIN_BITRATE_KBPS = 800
    MAIN_BITRATE_KBPS  = 300
    MAX_BITRATE_KBPS   = 300
    OFFER_TIMEOUT      = 10


# ============================================================
# CALIBRATION TOOLS
# ============================================================

class CalibrationConfig:
    BOTTOM_CAM_WARMUP = 20
    TOP_CAM_WARMUP    = 25

    EMBED_CAM_WIDTH  = 1280
    EMBED_CAM_HEIGHT = 720


# ============================================================
# ROLLING EVIDENCE BUFFERS
# ============================================================

class RollingBufferConfig:
    BUFFER_DURATION_S = 30.0
    JPEG_QUALITY      = 70
    TOP_FPS_ACTIVE    = 20
    TOP_FPS_IDLE      = 5

    # B3: fps used by RollingBuffer to compute deque(maxlen).
    # Must match the camera fps driving push() calls.
    # TopRollingBuffer uses TOP_FPS_ACTIVE; FaceRollingBuffer uses FACE_RECORD_FPS.
    ROLLING_BUFFER_FPS = 20   # frames/sec — used for maxlen calculation

    # Opt #15: raw numpy ring buffer
    RAW_BUFFER_ENABLED = False


# ============================================================
# EVIDENCE RECORDING
# ============================================================

class EvidenceConfig:
    BASE_DIR           = "evidence"
    RECORD_FPS         = 20.0
    WRITER_INIT_TIMEOUT = 0.2


# ============================================================
# BACKGROUND VIDEO ENCODER
# ============================================================

class BgEncoderConfig:
    YIELD_EVERY  = 10
    YIELD_SLEEP  = 0.002
    MAX_QUEUE    = 8
    FOURCC_ORDER = ["XVID", "mp4v"]


# ============================================================
# ON-SCREEN OVERLAY COLOURS  (BGR format for OpenCV)
# ============================================================

class OverlayConfig:
    COL_TRACKING    = (20, 215, 20)
    COL_QR_WARN     = (20, 20, 215)
    COL_ENTERING    = (0, 200, 255)
    COL_INSERTING   = (0, 140, 255)
    COL_STABILIZING = (255, 100, 0)

    COL_SOURCE    = (30, 130, 255)
    COL_DEST_BASE = (40, 220, 255)

    COL_STAGING_EMPTY = [(200, 100, 30), (30, 100, 200)]
    COL_STAGING_OCC   = [(255, 180, 80), (80, 180, 255)]

    ROI_RECT_COLOR = (255, 255, 0)


# ============================================================
# SLOT EMBED  (bottom-camera embedding algorithm)
# ============================================================

class SlotEmbedConfig:
    IMG_SIZE  = 64
    HIST_BINS = 32
    DCT_SIZE  = 8
    EMBEDDING_DIM = HIST_BINS + (DCT_SIZE * DCT_SIZE)   # 96


# ============================================================
# CAMERA PROCESS  (Opt #1 — multi-process camera)
# ============================================================

class CameraProcessConfig:
    ENABLED         = False
    WATCHER_POLL_S  = 0.001
    STARTUP_TIMEOUT = 5.0
    WARMUP_FRAMES   = 10


# ============================================================
# EVIDENCE STORAGE  (Opt #38 — local structured storage)
# ============================================================

class EvidenceStorageConfig:
    BASE_DIR         = "evidence"
    RETENTION_DAYS   = 30
    DISK_WARN_GB     = 10.0
    DISK_HARD_CAP_GB = 20.0
    PRUNE_INTERVAL_S = 3600


# ============================================================
# MONITOR SERVICE  (Opt #39 — slot monitor microservice / Redis fanout)
# ============================================================

class MonitorServiceConfig:
    ENABLED            = os.environ.get("PHONEBOX_REDIS_ENABLED", "0") == "1"
    REDIS_HOST         = os.environ.get("PHONEBOX_REDIS_HOST", "localhost")
    REDIS_PORT         = int(os.environ.get("PHONEBOX_REDIS_PORT", "6379"))
    REDIS_DB           = 0
    ALARM_CHANNEL      = "phonebox:alarms"
    SLOT_STATE_CHANNEL = "phonebox:slot_state"
    SUBSCRIBE_TIMEOUT_S = 1.0


# ── NtfyConfig ────────────────────────────────────────────────────────────────

class NtfyConfig:
    """
    LAN-native push notifications via self-hosted ntfy server.
    Setup: docker run -d --name ntfy -p 80:80 binwiederhier/ntfy serve
    """
    ENABLED    = False
    SERVER_URL = "http://ntfy.phonebox.local"
    TOPIC      = "phonebox-alarms"
    TIMEOUT_S  = 3.0
