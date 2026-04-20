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
  WebRTCConfig          WebRTC bitrate cap and offer timeout
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
    ASYNC_HOST      = "localhost"
    ASYNC_PORT      = 5432
    ASYNC_DATABASE  = "PhoneBoxDB"
    ASYNC_POOL_MIN  = 5
    ASYNC_POOL_MAX  = 20
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
    # When True the camera_manager allows a single physical camera to
    # serve multiple roles simultaneously (useful on a dev laptop).
    DEV_MODE_ENV_VAR = "PHONEBOX_DEV"

    # Slot monitor setup timeout — server_main waits this long for the
    # monitor's async setup to finish before starting admin handlers.
    MONITOR_SETUP_TIMEOUT = 20.0   # seconds

    # Fallback num_lids used for ROI calibration when DB is unreachable.
    FALLBACK_NUM_LIDS = 4

    # API base URL for the student lookup endpoint (scanner_worker)
    STUDENT_API_BASE = "http://127.0.0.1:5000/api/students"
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
    # Check QR every Nth frame (keeps CPU load low)
    QR_CHECK_EVERY_N  = 6

    # QR must be invisible for this long before qr_lost failure (s)
    QR_ABSENT_FAIL_S  = 3.0

    # ── ROI approach gate ─────────────────────────────────
    # Extra margin around the slot ROI for the centroid-in-ROI check
    ROI_APPROACH_MARGIN = 0.15   # fraction of max(roi_w, roi_h)
    # Centroid must be in ROI for this many consecutive frames → ENTERING
    ROI_APPROACH_FRAMES = 3

    # ── Rotation / insertion detection ────────────────────
    # Fraction of initial bounding-box area — triggers INSERTING transition
    AREA_REDUCTION_TRIGGER = 0.52
    # Degrees of rotation from initial angle — triggers INSERTING transition
    ANGLE_SWING_TRIGGER    = 28

    # Minimum bounding-box area (px²) to accept as a valid track target
    MIN_TRACK_AREA_PX = 800

    # ── Stillness detector ────────────────────────────────
    # Centroid velocity (px/frame) below which the phone is "still"
    STILL_VEL_THRESHOLD   = 12
    # Consecutive "still" frames needed to advance to STABILIZING
    STILL_REQUIRED_FRAMES = 8
    # Frames subtracted from the still counter when motion is detected
    STILL_PENALTY_ON_MOVE = 2

    # ── QR in-hand re-check interval (seconds) ────────────
    # How long the phone must be in a staging zone with QR visible
    STAGING_HOLD_TIME = 1.5   # seconds

    # ── SocketIO emit throttle ────────────────────────────
    EMIT_INTERVAL = 0.5   # seconds between tracking_update events


# ============================================================
# MOTION DETECTION  (frame-diff used inside PhoneTracker)
# ============================================================

class MotionConfig:
    # Gaussian blur kernel size for background subtraction (must be odd)
    BLUR_K      = 15
    # Binary threshold value after blur
    THRESH      = 20
    # Dilation iterations on the binary mask
    DILATE      = 3
    # Minimum contour area (px²) to count as phone motion
    MIN_AREA    = 1500
    # Minimum IoU between CSRT and motion bbox to merit merging
    IOU_MERGE   = 0.20
    # Re-initialise CSRT every N frames (prevents drift accumulation)
    CSRT_REINIT_INTERVAL = 12


# ============================================================
# LUCAS-KANADE OPTICAL FLOW  (fallback tracker layer)
# ============================================================

class LKConfig:
    MAX_POINTS   = 20
    MIN_POINTS   = 5
    GOOD_QUALITY = 0.25   # goodFeaturesToTrack quality level
    WIN_SIZE     = (17, 17)
    MAX_LEVEL    = 2
    # (type_flags, max_iterations, epsilon)
    CRITERIA     = (0x02 | 0x01, 20, 0.03)   # EPS | COUNT, 20 iters, 0.03 ε


# ============================================================
# ORB RE-IDENTIFICATION  (last-resort tracker layer)
# ============================================================

class OrbConfig:
    MATCH_THRESHOLD = 0.75   # ratio test threshold (Lowe's ratio test)
    MIN_MATCHES     = 10     # minimum good matches to accept re-ID
    RE_ID_EVERY_N   = 15     # re-ID attempt every Nth frame when CSRT is lost


# ============================================================
# SLOT MONITOR  (bottom camera — embedding workers)
# ============================================================

class SlotMonitorConfig:
    # Number of async worker coroutines sharing the slot list
    NUM_WORKERS = 4

    # Embedding distance above which a slot is considered "changed"
    MISMATCH_THRESHOLD = 0.15

    # Embedding distance drift that triggers a soft baseline recalibration
    RECALC_THRESHOLD   = 0.05

    # How long (seconds) a slot must stay "mismatch" before the alarm fires
    GRACE_PERIOD       = 3.0

    # Status-reporter interval (seconds between periodic log summaries)
    STATUS_REPORT_INTERVAL = 30   # seconds

    # Stop-event poll interval (seconds between checks inside _watch_stop_event)
    STOP_POLL_INTERVAL = 0.25   # seconds

    # Camera boot-up: wait up to this long for the first frame (seconds)
    CAMERA_INIT_TIMEOUT = 5.0

    # Camera warm-up frames to discard before monitoring begins
    CAMERA_WARMUP_FRAMES = 10

    # Grid spacing (px) used in auto-generated fallback ROI grid
    GRID_SPACING = 10


# ============================================================
# ALARM & PLACEMENT VERIFICATION
# ============================================================

class AlarmConfig:
    # Admin password is in back_end.secrets.Secrets.ADMIN_PASSWORD

    # Minimum embedding distance change seen by the BOTTOM camera that
    # indicates a phone is physically present in a slot.
    PLACEMENT_DETECTION_THRESHOLD = 0.10


# ============================================================
# ADMIN RESOLUTION SESSION
# ============================================================

class AdminConfig:
    # Base session lifetime before expiry (seconds).
    # Extended dynamically by SESSION_EXTEND_PER_RESOLVE for each resolved phone.
    SESSION_TIMEOUT = 120   # 2 minutes

    # Extra seconds added to the session timeout per resolved mismatch.
    SESSION_EXTEND_PER_RESOLVE = 30

    # Watchdog poll interval — how often the expiry thread checks is_expired() (s)
    WATCHDOG_POLL_INTERVAL = 5.0

    # Maximum seconds to wait for a QR code during an admin resolution scan.
    # Intentionally much longer than DVW (admin operations are manual and slow).
    ADMIN_QR_SCAN_TIMEOUT = 90.0


# ============================================================
# QR SCANNING
# ============================================================

class QRConfig:
    # Maximum seconds to wait for a valid QR scan during DVW operations
    DVW_SCAN_TIMEOUT = 15.0   # seconds (ops_handler.py QR_SCAN_TIMEOUT)

    # QR frame-buffer poll timeout (seconds per iteration)
    BUFFER_POLL_TIMEOUT = 0.05   # seconds


# ============================================================
# FACE EMBEDDING WORKER
# ============================================================

class EmbeddingConfig:
    # Thread-pool size for DeepFace embedding computation in embedding_gen.py.
    # Kept at 2: one active + one warm so latency is low without thrashing.
    MAX_WORKERS = 2


# ============================================================
# WEBRTC
# ============================================================

class WebRTCConfig:
    # Maximum video bitrate cap injected into the SDP answer (kbps).
    MAX_BITRATE_KBPS = 100

    # Timeout for asyncio future.result() when waiting for the offer handler (s).
    OFFER_TIMEOUT = 10


# ============================================================
# CALIBRATION TOOLS
# ============================================================

class CalibrationConfig:
    # Warm-up frames discarded before capturing the calibration snapshot.
    BOTTOM_CAM_WARMUP = 20   # roi_calibration.py + embed_calibration.py
    TOP_CAM_WARMUP    = 25   # staging_calibration.py

    # Resolution used by embed_calibration.py for the bottom camera.
    # Should match CameraConfig.BOTTOM_CAM_* unless the tool needs a
    # different resolution during calibration.
    EMBED_CAM_WIDTH  = 1280
    EMBED_CAM_HEIGHT = 720


# ============================================================
# ROLLING EVIDENCE BUFFERS
# ============================================================

class RollingBufferConfig:
    # How many seconds of footage to keep in memory at all times
    BUFFER_DURATION_S = 30.0

    # JPEG quality for in-memory frame compression [0–100]
    # Lower = smaller RAM footprint, slightly lower quality
    JPEG_QUALITY = 70

    # Top-camera buffer sample rate during DVW / admin operations
    TOP_FPS_ACTIVE = 20   # fps
    # Top-camera buffer sample rate when no operation is in progress
    TOP_FPS_IDLE   = 5    # fps


# ============================================================
# EVIDENCE RECORDING  (admin resolution session clips)
# ============================================================

class EvidenceConfig:
    # Base directory for all evidence files (relative to CWD)
    BASE_DIR = "evidence"

    # Target output fps for live-capture clips
    # (actual fps may be lower if the camera is slow)
    RECORD_FPS = 20.0

    # Max wait for video writer to initialise on the first live frame
    WRITER_INIT_TIMEOUT = 0.2   # seconds


# ============================================================
# BACKGROUND VIDEO ENCODER
# ============================================================

class BgEncoderConfig:
    # Yield CPU to real-time threads every N frames during encoding
    YIELD_EVERY = 10
    # Duration of each yield sleep (seconds)
    YIELD_SLEEP = 0.002

    # Drop oldest job when queue exceeds this depth (burst protection)
    MAX_QUEUE = 8

    # Codec preference list — tried in order, first winner used
    # "XVID" is fastest on most Windows/Linux OpenCV builds.
    FOURCC_ORDER = ["XVID", "mp4v"]


# ============================================================
# ON-SCREEN OVERLAY COLOURS  (BGR format for OpenCV)
# ============================================================

class OverlayConfig:
    # Phone-tracker bounding box
    COL_TRACKING    = (20, 215, 20)    # green  — QR visible, tracking OK
    COL_QR_WARN     = (20, 20, 215)    # red    — QR not visible
    COL_ENTERING    = (0, 200, 255)    # yellow — approaching slot
    COL_INSERTING   = (0, 140, 255)    # orange — mid-insertion
    COL_STABILIZING = (255, 100, 0)    # blue   — verifying

    # Context overlay
    COL_SOURCE       = (30, 130, 255)   # source slot (orange-ish)
    COL_DEST_BASE    = (40, 220, 255)   # destination slot (pulsing yellow)

    # Staging zones (two colours cycling)
    COL_STAGING_EMPTY = [(200, 100, 30), (30, 100, 200)]
    COL_STAGING_OCC   = [(255, 180, 80), (80, 180, 255)]

    # Misc
    ROI_RECT_COLOR = (255, 255, 0)   # scanner_loop ROI rectangle (cyan)