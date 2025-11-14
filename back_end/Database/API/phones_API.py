# back_end/Database/API/phones_API.py
from flask import Blueprint, jsonify, request
from back_end.Database.phones import (
    create_phone, get_phone, list_phones, update_phone, delete_phone,
    phones_not_stored, phones_by_condition, phone_stats, reassign_phone, regenerate_pid
)

phones_bp = Blueprint("phones", __name__)

def handle_response(res):
    data, code = res if isinstance(res, tuple) else (res, 200)
    return jsonify(data), code

# --- CRUD ---
@phones_bp.route("/", methods=["GET"])
def route_list_phones():
    return handle_response(list_phones())

@phones_bp.route("/<sid>", methods=["GET"])
def route_get_phone(sid):
    return handle_response(get_phone(sid))

@phones_bp.route("/", methods=["POST"])
def route_create_phone():
    data = request.get_json(force=True)
    return handle_response(create_phone(data))

@phones_bp.route("/<sid>", methods=["PUT"])
def route_update_phone(sid):
    data = request.get_json(force=True)
    return handle_response(update_phone(sid, data))

@phones_bp.route("/<sid>", methods=["DELETE"])
def route_delete_phone(sid):
    return handle_response(delete_phone(sid))

# --- Advanced ---
@phones_bp.route("/not_stored", methods=["GET"])
def route_phones_not_stored():
    return handle_response(phones_not_stored())

@phones_bp.route("/condition/<cond>", methods=["GET"])
def route_phones_by_condition(cond):
    return handle_response(phones_by_condition(cond))

@phones_bp.route("/stats", methods=["GET"])
def route_phone_stats():
    return handle_response(phone_stats())

@phones_bp.route("/reassign", methods=["PATCH"])
def route_reassign_phone():
    data = request.get_json(force=True)
    return handle_response(reassign_phone(data.get("old_sid"), data.get("new_sid")))

@phones_bp.route("/regenerate_pid/<sid>", methods=["PATCH"])
def route_regenerate_pid(sid):
    return handle_response(regenerate_pid(sid))
