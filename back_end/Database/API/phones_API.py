# back_end/Database/API/phones_API.py
from flask import Blueprint, jsonify, request
from back_end.Database.phones import (
    create_phone, get_phones, list_phones, update_phone, delete_phone,
    phones_not_stored, phones_by_condition, phone_stats, reassign_phone,
    get_phone_storage_history, get_phone_operation_history
)

phones_bp = Blueprint("phones", __name__)


def handle_response(res):
    data, code = res if isinstance(res, tuple) else (res, 200)
    return jsonify(data), code


# --- CRUD ---
@phones_bp.route("/", methods=["GET"])
def route_list_phones():
    """List all phones with storage status"""
    return handle_response(list_phones())


@phones_bp.route("/<sid>", methods=["GET"])
def route_get_phones(sid):
    """Get phones for a specific student"""
    return handle_response(get_phones(sid))


@phones_bp.route("/", methods=["POST"])
def route_create_phone():
    """Create a new phone (not stored)"""
    data = request.get_json(force=True)
    return handle_response(create_phone(data))


@phones_bp.route("/<pid>", methods=["PUT"])
def route_update_phone(pid):
    """Update phone details (NOT storage status)"""
    data = request.get_json(force=True)
    return handle_response(update_phone(pid, data))


@phones_bp.route("/<pid>", methods=["DELETE"])
def route_delete_phone(pid):
    """Delete a phone (must not be in storage)"""
    return handle_response(delete_phone(pid))


# --- Advanced ---
@phones_bp.route("/not_stored", methods=["GET"])
def route_phones_not_stored():
    """Get phones not currently in storage"""
    return handle_response(phones_not_stored())


@phones_bp.route("/condition/<cond>", methods=["GET"])
def route_phones_by_condition(cond):
    """Get phones by condition"""
    return handle_response(phones_by_condition(cond))


@phones_bp.route("/stats", methods=["GET"])
def route_phone_stats():
    """Get phone statistics"""
    return handle_response(phone_stats())


@phones_bp.route("/<pid>/reassign", methods=["PATCH"])
def route_reassign_phone(pid):
    """Reassign phone to different student"""
    data = request.get_json(force=True)
    return handle_response(reassign_phone(pid, data.get("new_sid")))


@phones_bp.route("/<pid>/history", methods=["GET"])
def route_phone_storage_history(pid):
    """Get storage history for a phone"""
    return handle_response(get_phone_storage_history(pid))


@phones_bp.route("/<pid>/operations", methods=["GET"])
def route_phone_operation_history(pid):
    """Get operation audit log for a phone"""
    limit = request.args.get('limit', 50, type=int)
    return handle_response(get_phone_operation_history(pid, limit))