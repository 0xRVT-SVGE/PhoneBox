# back_end/Database/API/students_API.py
from flask import Blueprint, jsonify

from back_end.Database.students import (
    list_students, get_student, create_student,
    update_student, delete_student,
    search_students, students_near_location,
    recently_modified_students
)

students_bp = Blueprint("students", __name__)

# --- Basic CRUD --- #
@students_bp.route("/", methods=["GET"])
def api_list_students():
    result = list_students()
    return jsonify(result)

@students_bp.route("/<sid>", methods=["GET"])
def api_get_student(sid):
    result = get_student(sid)
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)

@students_bp.route("/", methods=["POST"])
def api_add_student():
    result = create_student()
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)

@students_bp.route("/<sid>", methods=["PUT"])
def api_edit_student(sid):
    result = update_student(sid)
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)

@students_bp.route("/<sid>", methods=["DELETE"])
def api_delete_student(sid):
    result = delete_student(sid)
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)

# --- Advanced --- #
@students_bp.route("/search", methods=["GET"])
def api_search_students():
    result = search_students()
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)

@students_bp.route("/nearby", methods=["GET"])
def api_students_near_location():
    result = students_near_location()
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)

@students_bp.route("/recent", methods=["GET"])
def api_recently_modified():
    result = recently_modified_students()
    if isinstance(result, tuple):
        return jsonify(result[0]), result[1]
    return jsonify(result)
