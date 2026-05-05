# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False
# cython: cdivision=True
# ============================================================
# FILE: back_end/slot_monitor/slots_cy.pyx
# ============================================================
"""
Cython-accelerated slot distance math.

Session 14 — B2/CY1 fix
────────────────────────
`_cy_should_recalculate` previously did:
    recent = list(distances_history)[-min_samples:]
This materialises a Python list on EVERY normal frame (the common case —
most frames are not mismatches). At 50 slots × 30fps = 1500 calls/second
that is 1500 list allocations/second inside what was supposed to be
compiled C code.

Fix: replace with C-level reverse iteration over the deque using
`reversed()` and a C counter. `reversed(deque)` returns a C-level
`_collections._deque_iterator` — no list allocation, no copying.
The loop exits as soon as `min_samples` elements are checked.
"""

import numpy as np
cimport numpy as np
import time as _time_module

ctypedef np.float32_t FLOAT32


# ── cy_embedding_distance ─────────────────────────────────────────────────────

def cy_embedding_distance(
    np.ndarray[FLOAT32, ndim=1] e1,
    np.ndarray[FLOAT32, ndim=1] e2,
):
    """
    Cosine distance between two L2-normalised float32 vectors.
    Returns float in [0, 2].
    """
    return float(1.0 - np.dot(e1, e2))


# ── cy_update_distance ────────────────────────────────────────────────────────

def cy_update_distance(
    double dist,
    double mismatch_threshold,
    double recalc_threshold,
    double grace_period,
    double last_dist,            # slot.last_dist (in)
    bint   current_mismatch,     # slot.mismatch (in)
    object grace_start_ts,       # slot._grace_start_ts (in) — float or None
    object distances_history,    # slot.distances_history (deque, mutated in-place)
    int    recalc_min_samples,   # SlotMonitorConfig.RECALC_MIN_SAMPLES
):
    """
    Run the distance → alarm-state machine for one slot, one frame.

    Returns dict with keys:
        trigger_alarm  bool
        stop_alarm     bool
        needs_recalc   bool
        new_last_dist  float
        new_mismatch   bool
        new_grace_ts   float or None
    """
    # Append to history (Python deque — one Python call, unavoidable)
    distances_history.append(dist)

    cdef double now
    cdef bint   trigger_alarm = False
    cdef bint   stop_alarm    = False
    cdef bint   needs_recalc  = False
    cdef bint   new_mismatch  = current_mismatch
    cdef object new_grace_ts  = grace_start_ts

    # ── CASE 1: NORMAL ────────────────────────────────────────────────────────
    if dist < mismatch_threshold:
        if current_mismatch:
            stop_alarm   = True
        new_mismatch  = False
        new_grace_ts  = None

        # B2/CY1 fix: no list allocation — C-level reverse iteration
        needs_recalc = _cy_should_recalculate_no_alloc(
            distances_history, recalc_min_samples, recalc_threshold, dist
        )

        return {
            "trigger_alarm": False,
            "stop_alarm":    stop_alarm,
            "needs_recalc":  needs_recalc,
            "new_last_dist": dist,
            "new_mismatch":  new_mismatch,
            "new_grace_ts":  new_grace_ts,
        }

    # ── CASE 2: SUSPICIOUS ────────────────────────────────────────────────────
    if grace_start_ts is None:
        new_grace_ts = _time_module.time()
    else:
        now = _time_module.time()
        if (now - <double>grace_start_ts) >= grace_period:
            if not current_mismatch:
                new_mismatch  = True
                trigger_alarm = True

    return {
        "trigger_alarm": trigger_alarm,
        "stop_alarm":    False,
        "needs_recalc":  False,
        "new_last_dist": dist,
        "new_mismatch":  new_mismatch,
        "new_grace_ts":  new_grace_ts,
    }


# ── _cy_should_recalculate_no_alloc (B2/CY1 fix) ─────────────────────────────

cdef bint _cy_should_recalculate_no_alloc(
    object distances_history,   # deque
    int    min_samples,
    double recalc_threshold,
    double last_dist,
):
    """
    B2/CY1: Check recalibration eligibility WITHOUT allocating a Python list.

    Original code:
        recent = list(distances_history)[-min_samples:]   # ← 1500 allocs/sec!
        for d in recent: ...

    New code: iterate the deque in REVERSE using reversed(), count up to
    min_samples, exit early. reversed(deque) is a C-level iterator
    (_collections._deque_iterator) — zero heap allocation.

    Returns True if the last min_samples distances all fall in the
    soft-recalibration band: (recalc_threshold, last_dist * 1.5).
    """
    cdef int    n       = len(distances_history)
    cdef int    checked = 0
    cdef double d
    cdef double upper   = last_dist * 1.5

    if n < min_samples:
        return False

    # reversed() on a deque is O(1) construction, O(1) per step — no copy
    for d in reversed(distances_history):
        if not (recalc_threshold < d < upper):
            return False
        checked += 1
        if checked >= min_samples:
            return True

    # Fewer than min_samples elements iterated (shouldn't happen after guard above)
    return False


# ── Legacy name kept for any direct callers ───────────────────────────────────
# (slots.py calls cy_update_distance which now uses the no-alloc version
#  internally; this alias exists only for tooling / tests)
cdef bint _cy_should_recalculate(
    object distances_history,
    int    min_samples,
    double recalc_threshold,
    double last_dist,
):
    return _cy_should_recalculate_no_alloc(
        distances_history, min_samples, recalc_threshold, last_dist
    )
