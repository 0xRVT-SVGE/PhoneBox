# ============================================================
# FILE: back_end/access_control.py
# ============================================================
"""
Stateless access-control helper for multi-box PhoneBox.

Loaded once at server startup by server_main.init_access_control().
All public functions read from module-level state — no DB calls at
query time, so they are safe to call from the scanner thread.

Group inheritance rule
----------------------
A student's *effective group set* is:
  - Always contains year_code (e.g. 'year1')
  - If sub_group_codes is non-empty → those specific codes are added
  - If sub_group_codes is NULL/empty → ALL children of year_code
    (from the config) are implicitly included

Box access check
----------------
student_effective_groups ∩ box.accept_group_codes ≠ ∅
  → access granted
If box.accept_group_codes is NULL → box accepts any student.

Phone fit check
---------------
phone.phone_size ∈ box.accepted_phone_sizes
  → phone fits this cabinet
If box.accepted_phone_sizes is NULL → any size accepted.
"""

import json
import os
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ── Config paths ──────────────────────────────────────────────────────────────

_CONFIG_DIR  = os.path.join(os.path.dirname(__file__), "config")
_GROUPS_FILE = os.path.join(_CONFIG_DIR, "student_groups.json")

# ── Module-level state (set by init_access_control at startup) ────────────────

_groups_config: Dict = {}          # raw JSON dict from student_groups.json
_children_map:  Dict[str, List[str]] = {}   # parent_code → [child_codes]
_group_names:   Dict[str, str] = {}         # code → display name

_current_box: Dict = {             # current box metadata from DB
    "box_id":               None,
    "box_slug":             None,
    "box_name":             "Unknown Box",
    "accept_group_codes":   None,  # None = any student
    "accepted_phone_sizes": None,  # None = any size
}

_all_boxes: List[Dict] = []        # all known boxes (for denial messages)


# ── Initialisation ────────────────────────────────────────────────────────────

def load_groups_config(path: str = _GROUPS_FILE) -> Dict:
    """Load student_groups.json and build internal lookup structures."""
    global _groups_config, _children_map, _group_names

    with open(path, "r", encoding="utf-8") as f:
        _groups_config = json.load(f)

    _children_map.clear()
    _group_names.clear()

    for g in _groups_config.get("groups", []):
        code   = g["code"]
        parent = g.get("parent")
        _group_names[code] = g.get("name", code)
        if parent:
            _children_map.setdefault(parent, []).append(code)

    logger.info(
        "[AccessControl] Loaded %d groups, %d parents from %s",
        len(_group_names), len(_children_map), path,
    )
    return _groups_config


def set_current_box(box_id: int, box_slug: str, box_name: str,
                    accept_group_codes: Optional[List[str]],
                    accepted_phone_sizes: Optional[List[str]]) -> None:
    """Called at startup after resolving PHONEBOX_BOX_SLUG from DB."""
    global _current_box
    _current_box = {
        "box_id":               box_id,
        "box_slug":             box_slug,
        "box_name":             box_name,
        "accept_group_codes":   accept_group_codes,
        "accepted_phone_sizes": accepted_phone_sizes,
    }
    logger.info(
        "[AccessControl] Current box: slug=%r id=%d accept_groups=%s sizes=%s",
        box_slug, box_id, accept_group_codes, accepted_phone_sizes,
    )


def set_all_boxes(boxes: List[Dict]) -> None:
    """
    Cache the full box list (for denial messages).
    Each dict must have: box_id, box_slug, box_name, accept_group_codes.
    """
    global _all_boxes
    _all_boxes = boxes
    logger.info("[AccessControl] Cached %d boxes for denial messages.", len(boxes))


def get_groups_config() -> Dict:
    """Return the raw groups config (for API responses)."""
    return _groups_config


def get_current_box() -> Dict:
    """Return the cached current box metadata."""
    return _current_box


# ── Core helpers ──────────────────────────────────────────────────────────────

def get_effective_groups(year_code: Optional[str],
                         sub_group_codes: Optional[List[str]]) -> Set[str]:
    """
    Return the full set of group codes this student effectively belongs to.

    Examples
    --------
    year1, subs=[]          → {'year1', 'year1.1', 'year1.2', 'year1.boys', ...}
    year1, subs=['year1.1'] → {'year1', 'year1.1'}
    year1, subs=['boys']    → {'year1', 'boys'}
    None,  subs=[]          → set()   (unrestricted / staff with no year)
    """
    if not year_code:
        # No year set (e.g. staff) — return just their sub_group_codes if any
        return set(sub_group_codes) if sub_group_codes else set()

    effective: Set[str] = {year_code}
    subs = sub_group_codes or []

    if subs:
        effective.update(subs)
    else:
        # No specific subcategory → inherit ALL direct children of year_code
        effective.update(_children_map.get(year_code, []))

    return effective


def get_group_display_name(code: str) -> str:
    """Return the human-readable name for a group code."""
    return _group_names.get(code, code)


# ── Access checks (use current box by default) ────────────────────────────────

def check_box_access(year_code: Optional[str],
                     sub_group_codes: Optional[List[str]],
                     box_accept_codes: Optional[List[str]] = None) -> bool:
    """
    Return True if the student can access the box.

    box_accept_codes defaults to the current box's accept_group_codes.
    Pass an explicit value to check access against a different box.
    """
    if box_accept_codes is None:
        box_accept_codes = _current_box["accept_group_codes"]

    if box_accept_codes is None:
        return True   # box accepts anyone

    effective = get_effective_groups(year_code, sub_group_codes)
    if not effective:
        # Student has no group → deny (unless box is open to all)
        return False

    return bool(effective & set(box_accept_codes))


def check_phone_fits(phone_size: str,
                     accepted_sizes: Optional[List[str]] = None) -> bool:
    """
    Return True if the phone size is accepted in the current box.

    accepted_sizes defaults to the current box's accepted_phone_sizes.
    """
    if accepted_sizes is None:
        accepted_sizes = _current_box["accepted_phone_sizes"]

    if accepted_sizes is None:
        return True   # box accepts any size

    return phone_size in accepted_sizes


# ── Message builders ──────────────────────────────────────────────────────────

def build_denial_message(year_code: Optional[str],
                         sub_group_codes: Optional[List[str]]) -> str:
    """
    Build a human-readable denial message that tells the student
    which boxes they should use instead.
    """
    effective = get_effective_groups(year_code, sub_group_codes)
    current_name = _current_box.get("box_name", "this box")

    # Find boxes this student CAN use (excluding current)
    recommended = []
    for box in _all_boxes:
        slug   = box.get("box_slug")
        if slug == _current_box.get("box_slug"):
            continue
        accept = box.get("accept_group_codes")
        if accept is None or bool(effective & set(accept)):
            recommended.append(box.get("box_name", slug))

    if recommended:
        box_list = ", ".join(recommended)
        return (
            f"Access denied at {current_name}. "
            f"Please go to: {box_list}."
        )

    return (
        f"Access denied at {current_name}. "
        "You do not have access to any registered box. "
        "Please contact an administrator."
    )


def build_size_mismatch_message(phone_size: str) -> str:
    """
    Build a message for a phone that doesn't fit the current box's
    size requirement.
    """
    current_name   = _current_box.get("box_name", "this box")
    accepted_sizes = _current_box.get("accepted_phone_sizes")

    # Find a box that accepts this phone size
    suggested = []
    for box in _all_boxes:
        slug  = box.get("box_slug")
        if slug == _current_box.get("box_slug"):
            continue
        sizes = box.get("accepted_phone_sizes")
        if sizes is None or phone_size in sizes:
            suggested.append(box.get("box_name", slug))

    size_label = phone_size.replace("_", " ").title()
    if suggested:
        return (
            f"{size_label} phone — not accepted at {current_name}. "
            f"Use: {', '.join(suggested)}."
        )
    return f"{size_label} phone — not accepted at {current_name}."


# ── Phone action resolver ─────────────────────────────────────────────────────

def resolve_phone_action(phone: Dict) -> Dict:
    """
    Determine the UI action state for a single phone at the current box.

    Input dict keys (from get_student_phone_statuses query):
      pid, model, phone_size, stored_box_id, stored_box_name, stored_box_slug

    Returns the phone dict enriched with:
      action          — 'deposit' | 'retrieve' | 'wrong_box' | 'size_mismatch'
      fits_here       — bool
      stored_here     — bool
      action_message  — human-readable note (empty when action is active)
    """
    current_box_id = _current_box.get("box_id")
    stored_box_id  = phone.get("stored_box_id")
    phone_size     = phone.get("phone_size", "standard")
    fits           = check_phone_fits(phone_size)
    stored_here    = (stored_box_id is not None and stored_box_id == current_box_id)

    if stored_here:
        action  = "retrieve"
        message = ""
    elif stored_box_id is not None:
        # Stored in a different box
        action  = "wrong_box"
        stored_name = phone.get("stored_box_name", "another box")
        message = f"This phone is in {stored_name}. Go there to retrieve it."
    elif fits:
        action  = "deposit"
        message = ""
    else:
        action  = "size_mismatch"
        message = build_size_mismatch_message(phone_size)

    return {
        **phone,
        "action":        action,
        "fits_here":     fits,
        "stored_here":   stored_here,
        "action_message": message,
    }
