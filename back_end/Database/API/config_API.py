# ============================================================
# FILE: back_end/Database/API/config_API.py
# ============================================================
"""
Config API — exposes back_end/config/student_groups.json to Flutter.

GET /api/config/groups
  Returns a Flutter-friendly nested structure:
  {
    "status": "success",
    "data": {
      "groups": [
        {
          "code":     "year1",
          "label":    "Year 1",
          "type":     "year",
          "children": [
            {"code": "year1.1",    "label": "Year 1 — Class 1"},
            {"code": "year1.boys", "label": "Year 1 Boys"},
            ...
          ]
        },
        ...
      ],
      "phone_sizes": [
        {"code": "standard", "label": "Standard"},
        ...
      ]
    }
  }

  Flutter calls this once at startup and caches the result.
  No DB dependency — pure in-memory transformation from the flat JSON.
"""

import logging
from flask import Blueprint, jsonify
import back_end.access_control as ac

logger    = logging.getLogger(__name__)
config_bp = Blueprint("config", __name__)


def _build_nested_groups(raw_cfg: dict) -> dict:
    """
    Transform the flat groups list (with parent pointers) into the nested
    format the Flutter picker expects (children arrays).

    raw format : [{"code": "year1.1", "name": "Year 1 — Class 1",
                   "type": "subgroup", "parent": "year1", "sort": 2}, ...]
    returned   : [{"code": "year1", "label": "Year 1", "type": "year",
                   "children": [{"code": "year1.1", "label": "Year 1 — Class 1"}]}]
    """
    flat        = raw_cfg.get("groups", [])
    phone_sizes = raw_cfg.get("phone_sizes", [])

    # Build parent → children map
    children_map: dict[str, list] = {}
    entries: dict[str, dict] = {}

    for g in sorted(flat, key=lambda x: x.get("sort", 999)):
        code   = g["code"]
        parent = g.get("parent")
        entries[code] = {
            "code":  code,
            "label": g.get("name", code),
            "type":  g.get("type", "other"),
        }
        if parent:
            children_map.setdefault(parent, []).append(code)

    # Build nested root list (items with parent=null only)
    nested_groups = []
    for g in sorted(flat, key=lambda x: x.get("sort", 999)):
        if g.get("parent") is not None:
            continue   # skip children — they appear inside their parent
        code = g["code"]
        item = dict(entries[code])
        kids = children_map.get(code, [])
        item["children"] = [
            {"code": c, "label": entries[c]["label"]}
            for c in kids
            if c in entries
        ]
        nested_groups.append(item)

    return {
        "groups":      nested_groups,
        "phone_sizes": [
            {"code": s.get("code", s), "label": s.get("name", s.get("code", s))}
            for s in phone_sizes
        ],
    }


@config_bp.route("/groups", methods=["GET"])
def get_groups():
    """
    Return student groups + phone sizes in Flutter-friendly nested format.
    See module docstring for the exact shape.
    """
    try:
        cfg = ac.get_groups_config()
        if not cfg:
            # Config not loaded yet (e.g. direct HTTP hit before first scan)
            cfg = ac.load_groups_config()
        nested = _build_nested_groups(cfg)
        return jsonify({"status": "success", "data": nested}), 200
    except FileNotFoundError as exc:
        logger.error("[ConfigAPI] student_groups.json not found: %s", exc)
        return jsonify({"status": "error",
                        "message": "Group config file missing on server"}), 500
    except Exception as exc:
        logger.error("[ConfigAPI] /groups error: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@config_bp.route("/current_box", methods=["GET"])
def get_current_box():
    """Return the current box's metadata (slug, name, accepted groups/sizes)."""
    try:
        box = ac.get_current_box()
        return jsonify({"status": "success", "data": box}), 200
    except Exception as exc:
        logger.error("[ConfigAPI] /current_box error: %s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500
