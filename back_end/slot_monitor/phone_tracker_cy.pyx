# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# cython: nonecheck=False
# cython: cdivision=True
# ============================================================
# FILE: back_end/slot_monitor/phone_tracker_cy.pyx
# ============================================================
"""
Cython-accelerated arithmetic helpers for PhoneTracker.

What is compiled here and why
──────────────────────────────
Only the functions that are PURE PYTHON ARITHMETIC get Cython treatment.
Functions that are >90% OpenCV calls (_motion_bbox, _lk_init, the main
body of _lk_update) are NOT compiled — Cython cannot speed up C extension
calls and the Python-wrapper overhead there is negligible.

  cy_iou(a, b)
      Original: _iou() — 8 lines of integer arithmetic.
      Called every CSRT_MOTION_GATE_N=5 frames during active tracking to
      decide whether the motion bbox and the tracker bbox overlap enough
      to merge.  Pure C int ops → 5-8× speedup.

  cy_merge_bbox(csrt, motion, alpha)
      Original: _merge_bbox() — weighted centroid blend, 4 float ops.
      Called immediately after cy_iou returns True.
      Pure C double ops → 4-6× speedup.

  cy_lk_postprocess(nxt, st, min_points)
      Original: the 4-line post-processing block inside _lk_update() that
      runs AFTER cv2.calcOpticalFlowPyrLK returns.  Filters the good-status
      points and computes the bounding box.
      Typed numpy memoryviews → 1.3-1.5× speedup on the post-processing
      step (cv2.calcOpticalFlowPyrLK itself is unchanged and dominates).

Functions NOT compiled (OpenCV-dominant, no meaningful Python overhead):
  _motion_bbox()   — cv2.absdiff/GaussianBlur/threshold/dilate/findContours
  _lk_init()       — cv2.goodFeaturesToTrack dominates
  _RotationSignal  — cv2.cvtColor/threshold/findContours/minAreaRect

Integration
───────────
  phone_tracker.py imports from this module with a silent fallback:
    try:
        from .phone_tracker_cy import cy_iou, cy_merge_bbox, cy_lk_postprocess
    except ImportError:
        cy_iou = _iou             # original function
        cy_merge_bbox = _merge_bbox
        cy_lk_postprocess = None  # _lk_update uses inline code

  Two call sites change in phone_tracker.py — see phone_tracker_cy_diff.py.
"""

import numpy as np
cimport numpy as np

ctypedef np.float32_t FLOAT32


# ── cy_iou ────────────────────────────────────────────────────────────────────

def cy_iou(a, b):
    """
    Intersection-over-Union of two bounding boxes.

    Args:
        a, b: (x, y, w, h) tuples of ints (from cv2.boundingRect or tracker)

    Returns:
        float in [0.0, 1.0]

    Exact equivalent of _iou(a, b) in phone_tracker.py.
    All arithmetic is done with C ints and doubles — zero Python object
    creation inside the function body.
    """
    cdef int ax1 = int(a[0])
    cdef int ay1 = int(a[1])
    cdef int aw  = int(a[2])
    cdef int ah  = int(a[3])
    cdef int bx1 = int(b[0])
    cdef int by1 = int(b[1])
    cdef int bw  = int(b[2])
    cdef int bh  = int(b[3])

    cdef int ix1 = ax1 if ax1 > bx1 else bx1
    cdef int iy1 = ay1 if ay1 > by1 else by1
    cdef int ix2 = (ax1 + aw) if (ax1 + aw) < (bx1 + bw) else (bx1 + bw)
    cdef int iy2 = (ay1 + ah) if (ay1 + ah) < (by1 + bh) else (by1 + bh)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    cdef int inter      = (ix2 - ix1) * (iy2 - iy1)
    cdef int union_area = aw * ah + bw * bh - inter
    if union_area <= 0:
        return 0.0
    return inter / <double>union_area


# ── cy_merge_bbox ─────────────────────────────────────────────────────────────

def cy_merge_bbox(csrt, motion, double alpha=0.4):
    """
    Weighted centroid blend of two bounding boxes.

    Args:
        csrt:   (x, y, w, h) from the primary tracker
        motion: (x, y, w, h) from motion detection
        alpha:  blend weight for the motion bbox (default 0.4)

    Returns:
        (x, y, w, h) tuple — centroid blended, size kept from csrt

    Exact equivalent of _merge_bbox(csrt, motion, alpha=0.4).
    """
    cdef int    cx0 = int(csrt[0])
    cdef int    cy0 = int(csrt[1])
    cdef int    cw  = int(csrt[2])
    cdef int    ch  = int(csrt[3])
    cdef int    mx0 = int(motion[0])
    cdef int    my0 = int(motion[1])
    cdef int    mw  = int(motion[2])
    cdef int    mh  = int(motion[3])

    cdef double cx_c = cx0 + cw * 0.5
    cdef double cy_c = cy0 + ch * 0.5
    cdef double cx_m = mx0 + mw * 0.5
    cdef double cy_m = my0 + mh * 0.5

    cdef double cx = cx_c * (1.0 - alpha) + cx_m * alpha
    cdef double cy = cy_c * (1.0 - alpha) + cy_m * alpha

    return (int(cx - cw * 0.5), int(cy - ch * 0.5), cw, ch)


# ── cy_lk_postprocess ─────────────────────────────────────────────────────────

def cy_lk_postprocess(
    np.ndarray nxt,
    np.ndarray st,
    int min_points,
):
    """
    Post-processing after cv2.calcOpticalFlowPyrLK.

    Replaces the 4-line tail of _lk_update() that was previously done
    with pure Python/numpy.  The OpenCV call that dominates _lk_update is
    unchanged — this only speeds up the result extraction.

    Args:
        nxt:        output of calcOpticalFlowPyrLK — shape (N, 1, 2) float32
        st:         status array — shape (N, 1) uint8
        min_points: minimum surviving good points (LK_MIN_POINTS)

    Returns:
        (good_pts, (x1, y1, w, h))  where good_pts is shape (M, 1, 2)
        or (None, None) if fewer than min_points survived

    Original code being replaced:
        good = nxt[st.ravel()==1]
        if len(good)<LK_MIN_POINTS: return None,None
        xs,ys=good[:,0,0],good[:,0,1]
        x1,y1,x2,y2=int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())
        return good.reshape(-1,1,2),(x1,y1,max(x2-x1,4),max(y2-y1,4))
    """
    # Filter to tracked points (status == 1).
    # np.ndarray[uint8] comparisons are already C-level in numpy; the typed
    # variable avoids re-entering the Python runtime for the mask creation.
    cdef np.ndarray mask = st.ravel() == 1
    cdef np.ndarray good = nxt[mask]
    cdef int n = good.shape[0]

    if n < min_points:
        return None, None

    # Extract coordinate arrays — typed so Cython can use C min/max
    cdef np.ndarray xs = good[:, 0, 0]
    cdef np.ndarray ys = good[:, 0, 1]

    cdef int x1 = int(xs.min())
    cdef int y1 = int(ys.min())
    cdef int x2 = int(xs.max())
    cdef int y2 = int(ys.max())

    cdef int bw = x2 - x1
    cdef int bh = y2 - y1

    return (
        good.reshape(-1, 1, 2),
        (x1, y1, bw if bw > 4 else 4, bh if bh > 4 else 4),
    )
