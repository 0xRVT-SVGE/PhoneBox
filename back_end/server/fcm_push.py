# ============================================================
# FILE: back_end/server/fcm_push.py
# ============================================================
"""
Opt #34 — FCM Push Alarms.

Sends push notifications to admin devices when:
  • An alarm fires          → high-priority "ALARM" notification
  • An alarm is cleared     → normal-priority "All clear" notification

Architecture
────────────
  • Firebase Admin SDK sends directly to FCM HTTP v1 API.
  • Device tokens are stored in   back_end/server/fcm_tokens.json
    (a JSON list of strings). The Flutter app registers its token
    via  POST /api/fcm/register  on every app launch.
  • If firebase-admin is not installed OR no credentials file is
    found, all calls are silent no-ops so the server starts fine
    without Firebase configured.

Credential lookup order
───────────────────────
  1. Env var   PHONEBOX_FCM_CREDENTIALS  (path to .json key file)
  2. File      back_end/server/firebase_service_account.json
  3. Application Default Credentials (gcloud auth, GCP VM, etc.)

Install
───────
  pip install firebase-admin

Firebase setup
──────────────
  1. https://console.firebase.google.com → your project
  2. Project settings → Service accounts → Generate new private key
     Save as back_end/server/firebase_service_account.json
  3. Add Android/iOS apps and download their google-services.json /
     GoogleService-Info.plist into the Flutter project.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────
_HERE        = Path(__file__).parent
_CREDS_FILE  = _HERE / "firebase_service_account.json"
_TOKENS_FILE = _HERE / "fcm_tokens.json"

# ── firebase_admin availability ───────────────────────────
try:
    import firebase_admin
    from firebase_admin import credentials, messaging
    _FA_AVAILABLE = True
except ImportError:
    firebase_admin = None  # type: ignore
    credentials    = None  # type: ignore
    messaging      = None  # type: ignore
    _FA_AVAILABLE  = False
    logger.info(
        "[FCM] firebase-admin not installed — push notifications disabled. "
        "Install with: pip install firebase-admin"
    )

# ── Module state ──────────────────────────────────────────
_app_initialized = False
_init_lock       = threading.Lock()
_tokens_lock     = threading.Lock()


# ══════════════════════════════════════════════════════════
# INITIALIZATION
# ══════════════════════════════════════════════════════════

def _find_creds_path() -> Optional[Path]:
    """Return the first valid credentials file path, or None."""
    # 1. Env var override
    env = os.environ.get("PHONEBOX_FCM_CREDENTIALS")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning(f"[FCM] PHONEBOX_FCM_CREDENTIALS set but file not found: {p}")

    # 2. Default location
    if _CREDS_FILE.exists():
        return _CREDS_FILE

    return None


def _init_app() -> bool:
    """
    Lazy one-time initialization of firebase_admin.
    Returns True if the app is ready to send notifications.
    """
    global _app_initialized
    if _app_initialized:
        return True
    if not _FA_AVAILABLE:
        return False

    with _init_lock:
        if _app_initialized:
            return True

        try:
            creds_path = _find_creds_path()

            if creds_path:
                cred = credentials.Certificate(str(creds_path))
                logger.info(f"[FCM] Using service account: {creds_path.name}")
            else:
                # Application Default Credentials (GCP, gcloud auth)
                cred = credentials.ApplicationDefault()
                logger.info("[FCM] Using Application Default Credentials")

            # Only initialize if not already done (e.g. by another module)
            if not firebase_admin._apps:
                firebase_admin.initialize_app(cred)

            _app_initialized = True
            logger.info("[FCM] Opt #34: Firebase Admin SDK initialized — push alerts active")
            return True

        except Exception as exc:
            logger.warning(
                f"[FCM] Firebase init failed ({exc}) — "
                "push notifications disabled. "
                "Check your service account credentials."
            )
            return False


# ══════════════════════════════════════════════════════════
# TOKEN REGISTRY
# ══════════════════════════════════════════════════════════

def _load_tokens() -> List[str]:
    """Load FCM device tokens from disk. Returns [] on any error."""
    with _tokens_lock:
        if not _TOKENS_FILE.exists():
            return []
        try:
            data = json.loads(_TOKENS_FILE.read_text())
            if isinstance(data, list):
                return [t for t in data if isinstance(t, str) and t.strip()]
            return []
        except Exception as exc:
            logger.warning(f"[FCM] Failed to load tokens: {exc}")
            return []


def _save_tokens(tokens: List[str]) -> None:
    """Persist token list to disk."""
    with _tokens_lock:
        try:
            _TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _TOKENS_FILE.write_text(json.dumps(list(set(tokens)), indent=2))
        except Exception as exc:
            logger.warning(f"[FCM] Failed to save tokens: {exc}")


def register_token(token: str) -> bool:
    """
    Register an FCM device token.

    Called by POST /api/fcm/register on every Flutter app launch.
    Duplicate tokens are deduplicated automatically.

    Returns True if newly added, False if already registered.
    """
    if not token or not isinstance(token, str):
        return False
    tokens = _load_tokens()
    if token in tokens:
        return False
    tokens.append(token)
    _save_tokens(tokens)
    logger.info(f"[FCM] Token registered ({len(tokens)} total): {token[:20]}…")
    return True


def unregister_token(token: str) -> bool:
    """Remove a stale token (called when FCM reports it as invalid)."""
    tokens = _load_tokens()
    if token not in tokens:
        return False
    tokens.remove(token)
    _save_tokens(tokens)
    logger.info(f"[FCM] Token removed ({len(tokens)} remaining): {token[:20]}…")
    return True


def get_token_count() -> int:
    """Return the number of registered device tokens."""
    return len(_load_tokens())


# ══════════════════════════════════════════════════════════
# NOTIFICATION SENDERS
# ══════════════════════════════════════════════════════════

def _send_multicast(
    title:    str,
    body:     str,
    data:     dict,
    priority: str = "high",
) -> None:
    """
    Send a notification to all registered tokens.

    Runs in a background daemon thread so it never blocks the
    alarm_controller's hot path.

    Stale tokens returned by FCM are automatically unregistered.
    """
    if not _init_app():
        return

    tokens = _load_tokens()
    if not tokens:
        logger.debug("[FCM] No tokens registered — skipping push")
        return

    try:
        android_config = messaging.AndroidConfig(
            priority = priority,
            notification = messaging.AndroidNotification(
                title        = title,
                body         = body,
                sound        = "alarm" if priority == "high" else "default",
                channel_id   = "phonebox_alarms",
                icon         = "@mipmap/ic_launcher",
                color        = "#E5484D" if priority == "high" else "#30A46C",
                priority     = "max"    if priority == "high" else "default",
            ),
        )

        apns_config = messaging.APNSConfig(
            headers = {"apns-priority": "10" if priority == "high" else "5"},
            payload = messaging.APNSPayload(
                aps = messaging.Aps(
                    alert = messaging.ApsAlert(title=title, body=body),
                    sound = "alarm.caf" if priority == "high" else "default",
                    badge = 1,
                    content_available = True,
                ),
            ),
        )

        # Batch into groups of 500 (FCM multicast limit)
        stale: List[str] = []
        for i in range(0, len(tokens), 500):
            batch = tokens[i : i + 500]
            msg   = messaging.MulticastMessage(
                tokens           = batch,
                notification     = messaging.Notification(title=title, body=body),
                data             = {k: str(v) for k, v in data.items()},
                android          = android_config,
                apns             = apns_config,
            )
            response = messaging.send_each_for_multicast(msg)
            logger.info(
                f"[FCM] Sent to {len(batch)} device(s): "
                f"{response.success_count} ok, {response.failure_count} failed"
            )

            # Collect stale tokens
            for idx, res in enumerate(response.responses):
                if not res.success:
                    err = res.exception
                    if err and hasattr(err, 'code') and err.code in (
                        'registration-token-not-registered',
                        'invalid-registration-token',
                    ):
                        stale.append(batch[idx])
                        logger.debug(f"[FCM] Stale token: {batch[idx][:20]}…")

        for t in stale:
            unregister_token(t)

    except Exception as exc:
        logger.error(f"[FCM] send_multicast failed: {exc}")


def _send_async(title: str, body: str, data: dict, priority: str = "high") -> None:
    """Fire-and-forget: runs _send_multicast in a daemon thread."""
    t = threading.Thread(
        target  = _send_multicast,
        args    = (title, body, data, priority),
        daemon  = True,
        name    = "FCMSend",
    )
    t.start()


# ── Public notification helpers ───────────────────────────

def send_alarm_push(mismatch_count: int, mismatches: list) -> None:
    """
    Push a high-priority alarm notification to all admin devices.

    Called by AlarmController.trigger() on the first alarm activation.
    Fire-and-forget — never blocks the caller.

    Args:
        mismatch_count: Total number of active mismatches.
        mismatches:     List of [pid, lid] pairs.
    """
    if mismatch_count == 0:
        return

    slot_list = ", ".join(
        f"slot {int(lid) + 1}" for _, lid in mismatches[:3]
    )
    if mismatch_count > 3:
        slot_list += f" +{mismatch_count - 3} more"

    title = f"⚠️ PhoneBox Alarm — {mismatch_count} mismatch{'es' if mismatch_count > 1 else ''}"
    body  = f"Unexpected activity in: {slot_list}"

    _send_async(
        title    = title,
        body     = body,
        priority = "high",
        data     = {
            "type":            "alarm_triggered",
            "mismatch_count":  mismatch_count,
            "mismatches":      json.dumps([[str(p), int(l)] for p, l in mismatches]),
        },
    )
    logger.info(f"[FCM] Alarm push queued ({mismatch_count} mismatches)")


def send_alarm_cleared_push() -> None:
    """
    Push a normal-priority 'all clear' notification.

    Called by AlarmController._clear_active_alarm() after the alarm resolves.
    """
    _send_async(
        title    = "✅ PhoneBox — Alarm cleared",
        body     = "All slots are back to their expected state.",
        priority = "normal",
        data     = {"type": "alarm_cleared"},
    )
    logger.info("[FCM] Alarm-cleared push queued")


def send_test_push() -> bool:
    """
    Send a test notification to all registered devices.
    Returns True if at least one token was targeted.

    Called by POST /api/debug/test_fcm.
    """
    if not _init_app():
        return False
    tokens = _load_tokens()
    if not tokens:
        logger.warning("[FCM] test_push: no tokens registered")
        return False
    _send_async(
        title    = "🔔 PhoneBox — Test notification",
        body     = "Push notifications are working correctly.",
        priority = "normal",
        data     = {"type": "test"},
    )
    return True