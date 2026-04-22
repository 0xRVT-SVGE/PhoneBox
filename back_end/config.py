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
  CameraConfig          Camera indices and resolutions
  DatabaseConfig        DB connection parameters and pool sizes
  ServerConfig          Flask / SocketIO host+port, debug flags
  ScannerConfig         Front-camera face+barcode scan worker
  ScannerStateConfig    Badge / auth timeout for the scanner state machine
  TrackerConfig         PhoneTracker state-machine timeouts and knobs
  MotionConfig          Motion-detection and CSRT tracker parameters
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


# ============================================================
# CAMERAS
# ============================================================

class CameraConfig:
    # ── Device indices ────────────────────────────────────
    # Change these to match your physical camera layout.
    FRONT_CAM_INDEX  = 0   # scanner_loop.py  — face + barcode
    TOP_CAM_INDEX    = 2   # top_camera.py    — QR scan + phone tracking
    BOTTOM_CAM_INDEX = 1   # headless_slot_monitor.py — slot embedding

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


# ============================================================
# DATABASE
# ============================================================

class DatabaseConfig:
    # ── Synchronous connection pool (psycopg2) ────────────
    # Used by: db.py, SlotMonitorDB, SlotOperations, EvidenceRecorder
    SYNC_HOST     = "localhost"
    SYNC_PORT     = 5432
    SYNC_DATABASE = "PhoneBoxDB"
    SYNC_POOL_MIN = 1
    SYNC_POOL_MAX = 10
    # Credentials: imported from back_end.secrets.Secrets.DB_USER / DB_PASSWORD

    # ── Async connection pool (asyncpg) ───────────────────
    # Used by: AsyncSlotMonitorDB / HeadlessSlotMonitor
    ASYNC_HOST        = "localhost"
    ASYNC_PORT        = 5432
    ASYNC_DATABASE    = "PhoneBoxDB"
    ASYNC_POOL_MIN    = 5
    ASYNC_POOL_MAX    = 20
    ASYNC_CMD_TIMEOUT = 10.0   # seconds per command
    # Credentials: imported from back_end.secrets.Secrets.DB_USER / DB_PASSWORD


# ============================================================
# SERVER
# ============================================================

class ServerConfig:
    HOST = "0.0.0.0"
    PORT = 5000

    # Debug flags for scanner_loop
    DEBUG_ROI    = True   # Draw ROI rectangle on front-camera feed
    DEBUG_WINDOW = False  # Show cv2.imshow debug window

    # DEV_MODE: set env var PHONEBOX_DEV=1 or force True here.
    DEV_MODE_ENV_VAR = "PHONEBOX_DEV"

    # Slot monitor setup timeout — server_main waits this long for the
    # monitor's async setup to finish before starting admin handlers.
    MONITOR_SETUP_TIMEOUT = 20.0   # seconds

    # Fallback num_lids used for ROI calibration when DB is unreachable.
    FALLBACK_NUM_LIDS = 4

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

    # ── Debug / display ───────────────────────────────────
    # Set True to draw the phone bounding box on the top-camera feed.
    # Useful during development; can be disabled in production.
    DRAW_TRACKING_BOX = True


# ============================================================
# MOTION DETECTION  (frame-diff used inside PhoneTracker)
# ============================================================

class MotionConfig:
    BLUR_K               = 15
    THRESH               = 20
    DILATE               = 3
    MIN_AREA             = 1500
    IOU_MERGE            = 0.20
    CSRT_REINIT_INTERVAL = 12
    CSRT_MOTION_GATE_N   = 5   # Opt #13: run motion detect every N frames when CSRT OK


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
    #
    # Used by SlotOperations.make_placement_verifier() for BOTH deposit
    # (before=empty → after=occupied) and withdraw (before=occupied → after=empty).
    #
    # Typical cosine distances:
    #   no change (same state)        : 0.00 – 0.03
    #   lighting / angle noise        : 0.02 – 0.05
    #   phone placed or removed       : 0.08 – 0.25
    #
    # Set conservatively above noise but below the smallest real change.
    SLOT_CHANGE_THRESHOLD = 0.07

    # Minimum seconds between saving alarm clips for the same (pid, lid).
    # Prevents the background encoder from being flooded when multiple
    # alarm triggers fire in rapid succession for the same slot.
    CLIP_DEBOUNCE_S = 30.0


# ============================================================
# ADMIN RESOLUTION SESSION
# ============================================================

class AdminConfig:
    # Base session lifetime before expiry (seconds).
    # Extended by SESSION_EXTEND_PER_RESOLVE for each resolved phone.
    SESSION_TIMEOUT = 120   # 2 minutes

    # Extra seconds added per resolved mismatch.
    SESSION_EXTEND_PER_RESOLVE = 30

    # Watchdog poll interval (seconds)
    WATCHDOG_POLL_INTERVAL = 5.0

    # Maximum seconds for a QR scan during an admin resolution operation.
    ADMIN_QR_SCAN_TIMEOUT = 90.0

    # Seconds after admin_remove_ok before the "No QR on this object" button
    # appears in the frontend.  Set well below ADMIN_QR_SCAN_TIMEOUT so the
    # button only appears when the QR genuinely cannot be found, not on every scan.
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
    # Admin top-cam needs 800 kbps for QR code readability.
    # Front-cam (main/preview) needs 300 kbps for face recognition detail.
    # MAX_BITRATE_KBPS is the hard fallback for any unrecognised mode.
    ADMIN_BITRATE_KBPS = 800   # top-down camera — QR codes must be sharp
    MAIN_BITRATE_KBPS  = 300   # front camera — face recognition quality
    MAX_BITRATE_KBPS   = 300   # fallback (same as main)
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