# back_end/Database/API/students_API.py
from flask import Blueprint, jsonify, request
from back_end.Database.students import (
    create_student, get_student, list_students, update_student, delete_student,
    search_students, recently_modified_students
)

students_bp = Blueprint("students", __name__)

def handle_response(res):
    data, code = res if isinstance(res, tuple) else (res, 200)
    return jsonify(data), code

# --- CRUD ---
@students_bp.route("/", methods=["GET"])
def api_list_students():
    return handle_response(list_students())

@students_bp.route("/<sid>", methods=["GET"])
def api_get_student(sid):
    return handle_response(get_student(sid))

@students_bp.route("/", methods=["POST"])
def api_create_student():
    data = request.get_json(force=True)
    return handle_response(create_student(data))

@students_bp.route("/<sid>", methods=["PUT"])
def api_update_student(sid):
    data = request.get_json(force=True)
    return handle_response(update_student(sid, data))

@students_bp.route("/<sid>", methods=["DELETE"])
def api_delete_student(sid):
    return handle_response(delete_student(sid))

# --- Advanced ---
@students_bp.route("/search", methods=["GET"])
def api_search_students():
    query = request.args.get("q", "").strip()
    return handle_response(search_students(query))

@students_bp.route("/recent", methods=["GET"])
def api_recent_students():
    since = request.args.get("since")
    return handle_response(recently_modified_students(since))
