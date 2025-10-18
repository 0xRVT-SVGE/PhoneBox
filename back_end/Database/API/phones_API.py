# back_end/Database/API/phones_API.py
from flask import Blueprint, jsonify, request
from back_end.Database.phones import (
    create_phone, get_phone, list_phones,
    update_phone, delete_phone, phones_not_stored,
    phones_by_condition, phone_stats,
    reassign_phone, regenerate_pid
)

phones_bp = Blueprint("phones", __name__)

# --- Basic CRUD --- #
@phones_bp.route("/", methods=["GET"])
def route_list_phones():
    data, code = list_phones()
    return jsonify(data), code

@phones_bp.route("/<sid>", methods=["GET"])
def route_get_phone(sid):
    data, code = get_phone(sid)
    return jsonify(data), code

@phones_bp.route("/", methods=["POST"])
def route_create_phone():
    data_in = request.get_json(force=True)
    data, code = create_phone(data_in)
    return jsonify(data), code

@phones_bp.route("/<sid>", methods=["PUT"])
def route_update_phone(sid):
    data_in = request.get_json(force=True)
    data, code = update_phone(sid, data_in)
    return jsonify(data), code

@phones_bp.route("/<sid>", methods=["DELETE"])
def route_delete_phone(sid):
    data, code = delete_phone(sid)
    return jsonify(data), code

# --- Advanced --- #
@phones_bp.route("/not_stored", methods=["GET"])
def route_phones_not_stored():
    data, code = phones_not_stored()
    return jsonify(data), code

@phones_bp.route("/condition/<cond>", methods=["GET"])
def route_phones_by_condition(cond):
    data, code = phones_by_condition(cond)
    return jsonify(data), code

@phones_bp.route("/stats", methods=["GET"])
def route_phone_stats():
    data, code = phone_stats()
    return jsonify(data), code

@phones_bp.route("/reassign", methods=["PATCH"])
def route_reassign_phone():
    data_in = request.get_json(force=True)
    data, code = reassign_phone(data_in)
    return jsonify(data), code

@phones_bp.route("/regenerate_pid/<sid>", methods=["PATCH"])
def route_regenerate_pid(sid):
    data, code = regenerate_pid(sid)
    return jsonify(data), code
