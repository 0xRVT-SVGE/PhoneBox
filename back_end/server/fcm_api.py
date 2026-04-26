# ============================================================
# FILE: back_end/server/fcm_api.py
# ============================================================
"""
FCM token registration and debug endpoints.

Routes registered on the Flask app by server_main.py:
  POST /api/fcm/register     — Flutter registers its FCM token
  DELETE /api/fcm/unregister — Flutter removes its token on logout
  POST /api/debug/test_fcm   — Trigger a test push (dev/ops only)
  GET  /api/fcm/status       — How many tokens are registered
"""

from flask import Blueprint, jsonify, request
from back_end.server.fcm_push import (
    register_token,
    unregister_token,
    send_test_push,
    get_token_count,
    _app_initialized,
)
import logging

logger  = logging.getLogger(__name__)
fcm_bp  = Blueprint("fcm", __name__)


@fcm_bp.route("/register", methods=["POST"])
def route_register_token():
    """
    Register an FCM device token.

    Body (JSON): { "token": "<fcm_device_token>" }

    Called by the Flutter app on every launch via FcmService.registerToken().
    Idempotent — duplicate tokens are silently ignored.
    """
    data  = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()

    if not token:
        return jsonify({"status": "error", "message": "Missing token"}), 400

    added = register_token(token)
    return jsonify({
        "status": "success",
        "added":  added,
        "total":  get_token_count(),
    }), 200


@fcm_bp.route("/unregister", methods=["DELETE"])
def route_unregister_token():
    """
    Remove a device token (e.g. on admin logout or app uninstall).

    Body (JSON): { "token": "<fcm_device_token>" }
    """
    data  = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()

    if not token:
        return jsonify({"status": "error", "message": "Missing token"}), 400

    removed = unregister_token(token)
    return jsonify({
        "status": "success",
        "removed": removed,
        "total":   get_token_count(),
    }), 200


@fcm_bp.route("/status", methods=["GET"])
def route_fcm_status():
    """Return FCM readiness info (for ops/health checks)."""
    return jsonify({
        "status":      "success",
        "initialized": _app_initialized,
        "token_count": get_token_count(),
    }), 200


@fcm_bp.route("/test", methods=["POST"])
def route_test_push():
    """
    Send a test push to all registered devices.
    Useful during setup to verify end-to-end delivery.
    """
    ok = send_test_push()
    if ok:
        return jsonify({
            "status":  "success",
            "message": f"Test push queued to {get_token_count()} device(s)",
        }), 200
    return jsonify({
        "status":  "error",
        "message": "No tokens registered or Firebase not configured",
    }), 400