# ============================================================
# FILE: back_end/slot_monitor/phone_tracker.py
# ============================================================
"""
Phone Tracker  —  4-layer tracking + rotation-aware state machine.

Architecture overview
─────────────────────
Layer 1 — CSRT tracker (primary)
    Best spatial accuracy. Re-initialised every CSRT_REINIT_INTERVAL frames
    from the motion-contour bbox to prevent slow drift accumulation.

Layer 2 — Motion-contour tracker (fallback A)
    Frame-diff largest-contour.  Independent of appearance; survives 3D
    rotation when the phone tips from face-up into the vertical slot.

Layer 3 — Lucas-Kanade optical flow (fallback B)
    Tracks sparse Shi-Tomasi corners inside the last good bbox.  Smooth
    centroid/velocity estimates when contour detection is noisy.

Layer 4 — ORB re-identification (recovery)
    When all motion-based methods fail, tries to re-locate the phone by
    matching ORB descriptors against the initial appearance.  Runs in the
    same thread every RE_ID_EVERY_N frames — no extra threads.

State machine  (core security improvement)
───────────────────────────────────────────
  DETECTING   →  (motion bbox found)                              → TRACKING
  TRACKING    →  (centroid enters ROI approach zone N frames)     → ENTERING
  TRACKING    →  (QR absent > threshold, centroid NOT in ROI)     → FAILED qr_lost
  ENTERING    →  (centroid leaves ROI zone)                       → TRACKING
  ENTERING    →  (ROTATION signal detected)                       → INSERTING
  ENTERING    →  (FLAT signal: QR gone + still)                   → STABILIZING
  INSERTING   →  (phone still for STILL_REQUIRED_FRAMES)          → STABILIZING
  INSERTING   →  (insertion timeout)                              → FAILED insertion_timeout
  STABILIZING →  (verify_fn() confirms bottom cam embedding)      → SUCCESS
  STABILIZING →  (verify_fn() denies)                             → FAILED phone_not_in_slot
  All layers lost while in ENTERING/INSERTING → verify_fn() then SUCCESS or FAILED

Why this beats the old "QR gone in ROI = success"
───────────────────────────────────────────────────
Old design weakness: hide/flip the QR while holding the phone stationary
over (but not in) the slot.

New design requires ALL of the following before confirming success:
  1. Top camera — geometry:   centroid physically inside the ROI zone.
  2. Top camera — motion:     phone has stopped (velocity gate N frames).
  3. Top camera — shape:      phone underwent visible shape change
                               (area drops ≥ 48 % OR angle swings ≥ 28 °)
                               OR took the flat-placement path where QR
                               disappeared while the object was already still.
  4. Bottom camera — embed:   slot visual signature changed by ≥ threshold
                               compared to empty-slot baseline.

Hiding the QR satisfies none of 1–3; covering the bottom camera is
physically obvious and independent of the QR.

Rotation detection — why area reduction is the primary signal
──────────────────────────────────────────────────────────────
When a phone tips from face-up (horizontal, QR visible) into a vertical
charging slot, the top camera sees the phone "collapse" in one dimension:
  • Visible bbox area drops to 20–60 % of the face-up value.
  • minAreaRect angle swings ≥ 28 ° (rotated bounding rect tilts).
Both signals are computed from the largest visible contour inside the bbox,
independent of CSRT accuracy and lighting.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from enum import Enum, auto
from typing import Callable, Dict, List, Optional, Tuple
import re

import cv2
import numpy as np
from pyzbar.pyzbar import decode

logger = logging.getLogger(__name__)

_UUID_RE_TRACKER = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

# ══════════════════════════════════════════════════════════════════════════════
# TUNEABLE CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

DETECT_TIMEOUT           = 8.0
PLACEMENT_TIMEOUT        = 35.0
INSERTION_TIMEOUT        = 8.0
STABILIZING_TIMEOUT      = 3.0

QR_CHECK_EVERY_N         = 3
QR_ABSENT_FAIL_S         = 0.8

ROI_APPROACH_MARGIN      = 0.15   # fraction of max(rw,rh) padding around ROI
ROI_APPROACH_FRAMES      = 3      # consecutive frames before ENTERING

AREA_REDUCTION_TRIGGER   = 0.52   # 48 % area loss → INSERTING
ANGLE_SWING_TRIGGER      = 28     # degrees
MIN_TRACK_AREA_PX        = 800

STILL_VEL_THRESHOLD      = 12     # px/frame
STILL_REQUIRED_FRAMES    = 8
STILL_PENALTY_ON_MOVE    = 2

MOTION_BLUR_K            = 15
MOTION_THRESH            = 20
MOTION_DILATE            = 3
MOTION_MIN_AREA          = 1500
MOTION_IOU_MERGE         = 0.20
CSRT_REINIT_INTERVAL     = 12

LK_MAX_POINTS            = 20
LK_MIN_POINTS            = 5
LK_GOOD_QUALITY          = 0.25
LK_WIN_SIZE              = (17, 17)
LK_MAX_LEVEL             = 2
LK_CRITERIA              = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03)

RE_ID_EVERY_N            = 15
ORB_MATCH_THRESHOLD      = 0.75
ORB_MIN_MATCHES          = 10

STAGING_HOLD_TIME        = 1.5

# BGR colour palette
_COL_SOURCE        = (30, 130, 255)
_COL_DEST_BASE     = (40, 220, 255)
_COL_STAGING_EMPTY = [(200, 100, 30), (30, 100, 200)]
_COL_STAGING_OCC   = [(255, 180, 80), (80, 180, 255)]
_COL_TRACKING      = (20, 215,  20)   # green  — QR visible
_COL_QR_WARN       = (20,  20, 215)   # red    — QR gone
_COL_ENTERING      = (0,  200, 255)   # yellow — approaching ROI
_COL_INSERTING     = (0,  140, 255)   # orange — insertion detected
_COL_STABILIZING   = (255, 100,   0)  # cyan   — verifying
_FONT              = cv2.FONT_HERSHEY_SIMPLEX


# ══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

class _TS(Enum):
    DETECTING   = auto()
    TRACKING    = auto()
    ENTERING    = auto()
    INSERTING   = auto()
    STABILIZING = auto()
    SUCCESS     = auto()
    FAILED      = auto()


# ══════════════════════════════════════════════════════════════════════════════
# DRAWING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _draw_dashed_line(frame, x1, y1, x2, y2, color, thickness=2, dash=12, gap=7):
    length = math.hypot(x2-x1, y2-y1)
    if length < 1:
        return
    ux, uy = (x2-x1)/length, (y2-y1)/length
    pos, on = 0.0, True
    while pos < length:
        seg = min(dash if on else gap, length-pos)
        if on:
            cv2.line(frame,
                     (int(x1+ux*pos), int(y1+uy*pos)),
                     (int(x1+ux*(pos+seg)), int(y1+uy*(pos+seg))),
                     color, thickness, cv2.LINE_AA)
        pos += seg; on = not on


def _draw_dashed_rect(frame, x, y, w, h, color, thickness=2, dash=12, gap=7):
    x2, y2 = x+w, y+h
    _draw_dashed_line(frame, x, y, x2, y, color, thickness, dash, gap)
    _draw_dashed_line(frame, x2, y, x2, y2, color, thickness, dash, gap)
    _draw_dashed_line(frame, x2, y2, x, y2, color, thickness, dash, gap)
    _draw_dashed_line(frame, x, y2, x, y, color, thickness, dash, gap)


def _fill_alpha(frame, x, y, w, h, color, alpha=0.15):
    ov = frame.copy()
    cv2.rectangle(ov, (x,y), (x+w, y+h), color, cv2.FILLED)
    cv2.addWeighted(ov, alpha, frame, 1-alpha, 0, frame)


def _corner_brackets(frame, x, y, w, h, color, arm=20, thickness=2):
    x2, y2 = x+w, y+h
    for (ax,ay),(bx,by),(cx,cy) in [
        ((x+arm,y),(x,y),(x,y+arm)),
        ((x2-arm,y),(x2,y),(x2,y+arm)),
        ((x,y2-arm),(x,y2),(x+arm,y2)),
        ((x2,y2-arm),(x2,y2),(x2-arm,y2)),
    ]:
        cv2.line(frame,(ax,ay),(bx,by),color,thickness,cv2.LINE_AA)
        cv2.line(frame,(bx,by),(cx,cy),color,thickness,cv2.LINE_AA)


def _label(frame, text, x, y, color, scale=0.44, thick=1, pad=3):
    (tw,th),_ = cv2.getTextSize(text, _FONT, scale, thick)
    fh,fw = frame.shape[:2]
    x = max(pad, min(x, fw-tw-pad-2))
    y = max(th+pad+2, min(y, fh-pad-2))
    cv2.rectangle(frame,(x-pad,y-th-pad),(x+tw+pad,y+pad),(0,0,0),cv2.FILLED)
    cv2.putText(frame, text, (x,y), _FONT, scale, color, thick, cv2.LINE_AA)


def _pulse(base, period=1.2, lo=0.55, hi=1.0):
    f = lo + (hi-lo)*(0.5+0.5*math.sin(2*math.pi*time.time()/period))
    return tuple(min(255,int(c*f)) for c in base)


# ══════════════════════════════════════════════════════════════════════════════
# ROI FILE LOADER
# ══════════════════════════════════════════════════════════════════════════════

_roi_cache: Optional[Dict[int,Tuple]] = None
_roi_lock  = threading.Lock()

def _ensure_cache() -> None:
    global _roi_cache
    if _roi_cache is not None: return
    roi_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "rois_top.json")
    if not os.path.exists(roi_file):
        logger.warning(f"[Tracker] rois_top.json not found"); _roi_cache = {}; return
    try:
        with open(roi_file) as f: data = json.load(f)
        _roi_cache = {i: tuple(int(v) for v in r) for i,r in enumerate(data)}
        logger.info(f"[Tracker] Loaded {len(_roi_cache)} top-cam ROIs")
    except Exception as e:
        logger.error(f"[Tracker] Failed to load rois_top.json: {e}"); _roi_cache = {}

def _load_top_roi(lid: int) -> Optional[Tuple]:
    with _roi_lock: _ensure_cache(); return _roi_cache.get(lid)

def load_all_top_rois() -> Dict[int,Tuple]:
    with _roi_lock: _ensure_cache(); return dict(_roi_cache) if _roi_cache else {}


# ══════════════════════════════════════════════════════════════════════════════
# OVERLAY FACTORIES
# ══════════════════════════════════════════════════════════════════════════════

def make_dvw_context_overlay(all_rois, source_lid, dest_lid):
    def draw(frame):
        if source_lid is not None and source_lid in all_rois:
            rx,ry,rw,rh = all_rois[source_lid]
            _fill_alpha(frame,rx,ry,rw,rh,_COL_SOURCE,0.12)
            _draw_dashed_rect(frame,rx,ry,rw,rh,_COL_SOURCE,2)
            _label(frame,f"FROM  slot {source_lid+1}",rx+4,ry+rh-6,_COL_SOURCE)
        if dest_lid in all_rois:
            rx,ry,rw,rh = all_rois[dest_lid]
            col = _pulse(_COL_DEST_BASE)
            _fill_alpha(frame,rx,ry,rw,rh,col,0.13)
            _draw_dashed_rect(frame,rx,ry,rw,rh,col,2)
            _corner_brackets(frame,rx,ry,rw,rh,col,arm=min(20,rw//4,rh//4))
            cx,cy = rx+rw//2, ry+rh//2
            cv2.arrowedLine(frame,(cx,cy-16),(cx,cy+16),col,2,cv2.LINE_AA,tipLength=0.35)
            _label(frame,f"\u25BC  SLOT {dest_lid+1}  \u2014  PLACE HERE",rx+4,ry+rh+17,col)
    return draw


def make_admin_session_overlay(all_slot_rois, staging_rois, staged_pids,
                                source_lid=None, dest_lid=None):
    _staging = list(staging_rois)
    _pids    = list(staged_pids)
    def draw(frame):
        for i,roi in enumerate(_staging):
            if not roi or len(roi)<4: continue
            rx,ry,rw,rh = roi
            pid = _pids[i] if i<len(_pids) else None
            occ = pid is not None
            col_e = _COL_STAGING_EMPTY[i%len(_COL_STAGING_EMPTY)]
            col_o = _COL_STAGING_OCC[i%len(_COL_STAGING_OCC)]
            col   = col_o if occ else col_e
            name  = f"STAGING {i+1}"
            if occ:
                _fill_alpha(frame,rx,ry,rw,rh,col,0.22)
                cv2.rectangle(frame,(rx,ry),(rx+rw,ry+rh),col,3)
                tail = pid[-8:] if pid and len(pid)>8 else (pid or "")
                _label(frame,f"\u25CF {name}  [{tail}]",rx+4,ry+rh//2+7,col)
            else:
                _draw_dashed_rect(frame,rx,ry,rw,rh,col,2,dash=14,gap=6)
                _label(frame,f"\u25CB {name}  \u2014  empty",rx+4,ry+rh//2+7,col)
        if source_lid is not None and source_lid in all_slot_rois:
            rx,ry,rw,rh = all_slot_rois[source_lid]
            same = (source_lid==dest_lid)
            _fill_alpha(frame,rx,ry,rw,rh,_COL_SOURCE,0.11)
            _draw_dashed_rect(frame,rx,ry,rw,rh,_COL_SOURCE,2)
            lbl = f"FROM  slot {source_lid+1}" + (" \u2194 RETURN HERE" if same else "")
            _label(frame,lbl,rx+4,ry+rh-6,_COL_SOURCE)
            if same: _corner_brackets(frame,rx,ry,rw,rh,_COL_SOURCE,arm=min(20,rw//4,rh//4))
        if dest_lid is not None and dest_lid in all_slot_rois and dest_lid!=source_lid:
            rx,ry,rw,rh = all_slot_rois[dest_lid]
            col = _pulse(_COL_DEST_BASE)
            _fill_alpha(frame,rx,ry,rw,rh,col,0.13)
            _draw_dashed_rect(frame,rx,ry,rw,rh,col,2)
            _corner_brackets(frame,rx,ry,rw,rh,col,arm=min(20,rw//4,rh//4))
            cx,cy = rx+rw//2, ry+rh//2
            cv2.arrowedLine(frame,(cx,cy-16),(cx,cy+16),col,2,cv2.LINE_AA,tipLength=0.35)
            _label(frame,f"\u25BC  SLOT {dest_lid+1}  \u2014  PLACE HERE",rx+4,ry+rh+17,col)
    return draw


# ══════════════════════════════════════════════════════════════════════════════
# LOW-LEVEL TRACKING HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _motion_bbox(prev_gray, curr_gray):
    diff = cv2.absdiff(prev_gray, curr_gray)
    blur = cv2.GaussianBlur(diff,(MOTION_BLUR_K,MOTION_BLUR_K),0)
    _,thr = cv2.threshold(blur,MOTION_THRESH,255,cv2.THRESH_BINARY)
    thr   = cv2.dilate(thr,None,iterations=MOTION_DILATE)
    cnts,_ = cv2.findContours(thr,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    if not cnts: return None
    lg = max(cnts,key=cv2.contourArea)
    return cv2.boundingRect(lg) if cv2.contourArea(lg)>=MOTION_MIN_AREA else None


def _iou(a,b):
    ax1,ay1,aw,ah=a; bx1,by1,bw,bh=b
    ix1,iy1=max(ax1,bx1),max(ay1,by1)
    ix2,iy2=min(ax1+aw,bx1+bw),min(ay1+ah,by1+bh)
    inter=max(0,ix2-ix1)*max(0,iy2-iy1)
    if not inter: return 0.0
    return inter/(aw*ah+bw*bh-inter) if aw*ah+bw*bh-inter else 0.0


def _merge_bbox(csrt,motion,alpha=0.4):
    cx_c=csrt[0]+csrt[2]/2; cy_c=csrt[1]+csrt[3]/2
    cx_m=motion[0]+motion[2]/2; cy_m=motion[1]+motion[3]/2
    cx=cx_c*(1-alpha)+cx_m*alpha; cy=cy_c*(1-alpha)+cy_m*alpha
    return (int(cx-csrt[2]/2),int(cy-csrt[3]/2),csrt[2],csrt[3])


def _lk_init(gray, bbox):
    """Extract Shi-Tomasi corners inside bbox for LK tracking."""
    x,y,w,h=(int(v) for v in bbox)
    if w<10 or h<10: return None
    roi=gray[y:y+h, x:x+w]
    pts=cv2.goodFeaturesToTrack(roi,maxCorners=LK_MAX_POINTS,
                                qualityLevel=LK_GOOD_QUALITY,minDistance=6,blockSize=7)
    if pts is None or len(pts)<LK_MIN_POINTS: return None
    pts[:,0,0]+=x; pts[:,0,1]+=y
    return pts


def _lk_update(prev_gray, curr_gray, prev_pts):
    """One LK step. Returns (good_pts, bbox) or (None, None)."""
    if prev_pts is None or len(prev_pts)<LK_MIN_POINTS: return None,None
    nxt,st,_ = cv2.calcOpticalFlowPyrLK(prev_gray,curr_gray,prev_pts,None,
                                         winSize=LK_WIN_SIZE,maxLevel=LK_MAX_LEVEL,
                                         criteria=LK_CRITERIA)
    if nxt is None or st is None: return None,None
    good = nxt[st.ravel()==1]
    if len(good)<LK_MIN_POINTS: return None,None
    xs,ys=good[:,0,0],good[:,0,1]
    x1,y1,x2,y2=int(xs.min()),int(ys.min()),int(xs.max()),int(ys.max())
    return good.reshape(-1,1,2),(x1,y1,max(x2-x1,4),max(y2-y1,4))


def _orb_descriptors(gray, bbox):
    x,y,w,h=(int(v) for v in bbox)
    if w<10 or h<10: return None,None
    roi=gray[y:y+h, x:x+w]
    orb=cv2.ORB_create(nfeatures=100)
    kps,descs=orb.detectAndCompute(roi,None)
    if descs is None or len(descs)<ORB_MIN_MATCHES: return None,None
    for kp in kps: kp.pt=(kp.pt[0]+x, kp.pt[1]+y)
    return kps,descs


def _orb_reidentify(gray, ref_descs, search_region=None):
    if ref_descs is None or len(ref_descs)<ORB_MIN_MATCHES: return None
    fh,fw=gray.shape[:2]
    if search_region is not None:
        sx,sy,sw,sh=search_region
        mx,my=sw//2,sh//2
        x1,y1=max(0,sx-mx),max(0,sy-my)
        x2,y2=min(fw,sx+sw+mx),min(fh,sy+sh+my)
        sg=gray[y1:y2, x1:x2]; ox,oy=x1,y1
    else:
        sg=gray; ox,oy=0,0
    orb=cv2.ORB_create(nfeatures=200)
    kps,descs=orb.detectAndCompute(sg,None)
    if descs is None or len(descs)<ORB_MIN_MATCHES: return None
    bf=cv2.BFMatcher(cv2.NORM_HAMMING,crossCheck=False)
    try: matches=bf.knnMatch(ref_descs,descs,k=2)
    except cv2.error: return None
    good=[m for m,n in matches if m.distance<ORB_MATCH_THRESHOLD*n.distance]
    if len(good)<ORB_MIN_MATCHES: return None
    pts=np.array([kps[m.trainIdx].pt for m in good])
    rx,ry=int(pts[:,0].min())+ox,int(pts[:,1].min())+oy
    rw,rh=max(int(pts[:,0].max()-pts[:,0].min()),20)+ox,max(int(pts[:,1].max()-pts[:,1].min()),20)+oy
    return (rx,ry,rw,rh)


# ── Rotation / shape-change detector ──────────────────────────────────────────

class _RotationSignal:
    """
    Tracks visible area and minAreaRect angle of the phone as it moves
    toward and into the slot.

    Returns (area_ratio, angle_delta):
        area_ratio  = current_contour_area / initial_area
                      (None until bootstrapped with MIN_TRACK_AREA_PX)
        angle_delta = |current_minAreaRect_angle - initial_angle| in degrees
                      (None if angle not computable)
    """
    def __init__(self):
        self._init_area  = None
        self._init_angle = None

    def update(self, gray, bbox):
        x,y,w,h=(int(v) for v in bbox)
        if w<8 or h<8: return None,None
        roi=gray[y:y+h, x:x+w]
        _,thresh=cv2.threshold(roi,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        cnts,_=cv2.findContours(thresh,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        if not cnts: return None,None
        lg=max(cnts,key=cv2.contourArea)
        area=cv2.contourArea(lg)
        if area<100: return None,None

        angle=None
        if len(lg)>=5:
            angle=cv2.minAreaRect(lg)[2]

        if self._init_area is None:
            if area>=MIN_TRACK_AREA_PX:
                self._init_area=area; self._init_angle=angle
            return None,None

        area_ratio=area/self._init_area
        angle_delta=None
        if self._init_angle is not None and angle is not None:
            d=abs(angle-self._init_angle)
            angle_delta=90-d if d>45 else d

        return area_ratio, angle_delta


# ══════════════════════════════════════════════════════════════════════════════
# PHONE TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class PhoneTracker:
    """
    4-layer tracking pipeline with rotation-aware state machine.

    Parameters
    ──────────
    verify_fn : Optional[() -> bool]
        Called once when the tracker reaches STABILIZING.  Queries the
        BOTTOM camera embedding to confirm the phone is physically in the
        slot.  If None, skipped (less secure but works without bottom cam).
    """

    def __init__(self, pid, lid, slot_roi, background_frame, cancel_event,
                 socketio, client_id, staging_rois=None, verify_fn=None):
        self._pid             = pid
        self._lid             = lid
        self._slot_roi        = slot_roi
        self._bg              = background_frame
        self._cancel_event    = cancel_event
        self._socketio        = socketio
        self._client_id       = client_id
        self._staging_rois    = staging_rois or []
        self._verify_fn       = verify_fn
        # Overlay-visible (written by tracking thread, read by OpenCV thread)
        self._bbox:       Optional[Tuple] = None
        self._state:      _TS             = _TS.DETECTING
        self._qr_visible: bool            = True
        # Callbacks
        self._on_success  = None
        self._on_failure  = None
        self._on_staged   = None

    def start(self, on_success, on_failure, on_staged=None):
        self._on_success = on_success
        self._on_failure = on_failure
        self._on_staged  = on_staged
        threading.Thread(
            target=self._run, daemon=True,
            name=f"PhoneTracker-{self._pid[:8]}-lid{self._lid}",
        ).start()

    @staticmethod
    def _safe_frame(raw_fn, fb_fn):
        f=raw_fn(); return f if f is not None else fb_fn()

    # ── Phase 0: detect phone entering view ───────────────────────────────────

    def _detect_phone(self, tc):
        bg=tc.get_raw_frame() or self._bg
        bg_g=cv2.GaussianBlur(cv2.cvtColor(bg,cv2.COLOR_BGR2GRAY),(MOTION_BLUR_K,)*2,0)
        deadline=time.time()+DETECT_TIMEOUT
        while time.time()<deadline:
            if self._cancel_event.is_set(): return None
            if not tc.wait_for_frame(timeout=0.05): continue
            frame=self._safe_frame(tc.get_raw_frame,tc.get_frame)
            tc.clear_frame_event()
            if frame is None: continue
            g=cv2.GaussianBlur(cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY),(MOTION_BLUR_K,)*2,0)
            b=_motion_bbox(bg_g,g)
            if b: return b
        return None

    # ── Main entry ────────────────────────────────────────────────────────────

    def _run(self):
        from back_end.slot_monitor.camera.top_camera import top_camera as tc
        try:
            self._state=_TS.DETECTING
            logger.info(f"[Tracker] PID={self._pid} LID={self._lid} DETECTING")
            bbox=self._detect_phone(tc)
            if bbox is None:
                return self._fail("cancelled" if self._cancel_event.is_set()
                                  else "detect_timeout", tc)
            frame=self._safe_frame(tc.get_raw_frame,tc.get_frame)
            if frame is None: return self._fail("detect_timeout",tc)
            csrt=cv2.TrackerCSRT_create(); csrt.init(frame,bbox)
            self._bbox=bbox; self._state=_TS.TRACKING
            tc.set_tracker_overlay(self._draw_overlay)
            logger.info(f"[Tracker] CSRT init bbox={bbox} TRACKING")
            self._track(csrt,tc)
        except Exception as e:
            logger.error(f"[Tracker] Fatal: {e}",exc_info=True)
            try:
                from back_end.slot_monitor.camera.top_camera import top_camera as t2
                t2.clear_tracker_overlay()
            except Exception: pass
            if self._on_failure: self._on_failure("error")

    # ── Main tracking loop (state machine) ────────────────────────────────────

    def _track(self, csrt, tc):  # noqa: C901
        deadline        = time.time()+PLACEMENT_TIMEOUT
        last_emit       = 0.0
        frame_count     = 0
        # QR
        last_qr_ts      = time.time()
        qr_confirmed    = False
        self._qr_visible= True
        # CSRT
        csrt_ok         = True
        reinit_count    = 0
        # LK
        lk_pts          = None
        prev_gray       = None
        # ORB
        orb_ref_descs   = None
        last_bbox       = None
        # Rotation
        rot_sig         = _RotationSignal()
        # Velocity
        prev_cx=prev_cy = None
        still_count     = 0
        # State counters
        roi_count       = 0
        state_ts        = time.time()
        # Staging
        stg_idx=-1; stg_ts=None; stg_qr=False

        def centroid(bx,by,bw,bh): return bx+bw//2, by+bh//2

        while time.time()<deadline:
            if self._cancel_event.is_set():
                return self._fail("cancelled",tc)
            if not tc.wait_for_frame(timeout=0.05): continue
            frame=self._safe_frame(tc.get_raw_frame,tc.get_frame)
            tc.clear_frame_event()
            if frame is None: continue

            fh,fw=frame.shape[:2]
            curr_gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
            frame_count+=1

            # ── QR ────────────────────────────────────────────────────────────
            if frame_count%QR_CHECK_EVERY_N==0:
                if self._check_qr(frame):
                    last_qr_ts=time.time(); qr_confirmed=True
            qr_absent=time.time()-last_qr_ts
            self._qr_visible=(qr_absent<QR_ABSENT_FAIL_S)

            # ── Layer 1: CSRT ─────────────────────────────────────────────────
            bx=by=bw=bh=0
            if csrt_ok:
                ok,raw=csrt.update(frame)
                if ok:
                    bx,by,bw,bh=(int(v) for v in raw)
                    if bx+bw<=0 or bx>=fw or by+bh<=0 or by>=fh:
                        csrt_ok=False
                    else:
                        reinit_count+=1
                        # blend with motion
                        if prev_gray is not None:
                            pb=cv2.GaussianBlur(prev_gray,(MOTION_BLUR_K,)*2,0)
                            cb=cv2.GaussianBlur(curr_gray,(MOTION_BLUR_K,)*2,0)
                            mo=_motion_bbox(pb,cb)
                            if mo and _iou((bx,by,bw,bh),mo)>=MOTION_IOU_MERGE:
                                bx,by,bw,bh=_merge_bbox((bx,by,bw,bh),mo)
                        if reinit_count>=CSRT_REINIT_INTERVAL:
                            csrt=cv2.TrackerCSRT_create(); csrt.init(frame,(bx,by,bw,bh))
                            reinit_count=0
                else:
                    csrt_ok=False

            # ── Layer 2: motion contour ───────────────────────────────────────
            if not csrt_ok:
                if prev_gray is not None:
                    pb=cv2.GaussianBlur(prev_gray,(MOTION_BLUR_K,)*2,0)
                    cb=cv2.GaussianBlur(curr_gray,(MOTION_BLUR_K,)*2,0)
                    mo=_motion_bbox(pb,cb)
                else: mo=None

                if mo:
                    bx,by,bw,bh=mo
                    csrt=cv2.TrackerCSRT_create(); csrt.init(frame,mo)
                    csrt_ok=True; reinit_count=0; lk_pts=None

                # ── Layer 3: LK optical flow ──────────────────────────────────
                elif lk_pts is not None and prev_gray is not None:
                    lk_pts,lk_bbox=_lk_update(prev_gray,curr_gray,lk_pts)
                    if lk_bbox: bx,by,bw,bh=lk_bbox
                    else: bx=by=bw=bh=0

                # ── Layer 4: ORB re-identification ────────────────────────────
                elif frame_count%RE_ID_EVERY_N==0 and orb_ref_descs is not None:
                    rec=_orb_reidentify(curr_gray,orb_ref_descs,last_bbox)
                    if rec:
                        bx,by,bw,bh=rec
                        csrt=cv2.TrackerCSRT_create(); csrt.init(frame,rec)
                        csrt_ok=True; reinit_count=0; lk_pts=None
                        logger.debug(f"[Tracker] PID={self._pid} ORB re-ID")
                    else:
                        # All 4 layers failed
                        if self._state in (_TS.ENTERING,_TS.INSERTING):
                            placed=self._run_verify(tc)
                            if placed: return self._succeed(tc)
                            return self._fail("phone_not_in_slot",tc)
                        return self._fail("tracker_lost",tc)
                else:
                    prev_gray=curr_gray; continue

            if bw==0 or bh==0: prev_gray=curr_gray; continue

            # Keep LK points fresh
            if csrt_ok and (lk_pts is None or frame_count%10==0):
                new_lk=_lk_init(curr_gray,(bx,by,bw,bh))
                if new_lk is not None: lk_pts=new_lk

            # Bootstrap ORB reference
            if orb_ref_descs is None and self._state==_TS.TRACKING and qr_confirmed:
                _,orb_ref_descs=_orb_descriptors(curr_gray,(bx,by,bw,bh))

            self._bbox=(bx,by,bw,bh); last_bbox=(bx,by,bw,bh)
            cx,cy=centroid(bx,by,bw,bh)

            # ── Velocity ──────────────────────────────────────────────────────
            if prev_cx is not None:
                vel=math.hypot(cx-prev_cx,cy-prev_cy)
                if vel<STILL_VEL_THRESHOLD: still_count=min(still_count+1,STILL_REQUIRED_FRAMES+5)
                else: still_count=max(0,still_count-STILL_PENALTY_ON_MOVE)
            prev_cx,prev_cy=cx,cy

            # ── Rotation signal ───────────────────────────────────────────────
            area_ratio,angle_delta=rot_sig.update(curr_gray,(bx,by,bw,bh))
            rotation_detected=(
                (area_ratio  is not None and area_ratio <AREA_REDUCTION_TRIGGER) or
                (angle_delta is not None and angle_delta>ANGLE_SWING_TRIGGER)
            )

            # ── Centroid-in-ROI (replaces old bbox-overlap %) ─────────────────
            in_roi=self._centroid_in_roi(cx,cy)

            # ══════════════════════════════════════════════════════════════════
            # STATE TRANSITIONS
            # ══════════════════════════════════════════════════════════════════

            if self._state==_TS.TRACKING:
                # Staging detection
                if not in_roi:
                    si=self._in_which_staging(bx,by,bw,bh)
                    if si>=0:
                        if si!=stg_idx: stg_idx=si; stg_ts=time.time(); stg_qr=False
                        else:
                            if self._qr_visible: stg_qr=True
                            if stg_ts and time.time()-stg_ts>=STAGING_HOLD_TIME and stg_qr:
                                tc.clear_tracker_overlay()
                                if self._on_staged: self._on_staged(si)
                                return
                    else: stg_idx=-1; stg_ts=None; stg_qr=False

                # QR loss guard (only if not approaching ROI)
                if qr_confirmed and qr_absent>QR_ABSENT_FAIL_S and not in_roi and stg_ts is None:
                    return self._fail("qr_lost",tc)

                # Advance to ENTERING
                if in_roi:
                    roi_count+=1
                    if roi_count>=ROI_APPROACH_FRAMES:
                        self._state=_TS.ENTERING; state_ts=time.time(); still_count=0
                        logger.info(f"[Tracker] PID={self._pid} → ENTERING")
                else:
                    roi_count=0

            elif self._state==_TS.ENTERING:
                if not in_roi:
                    self._state=_TS.TRACKING; roi_count=0
                    logger.debug(f"[Tracker] PID={self._pid} left ROI → TRACKING")
                    prev_gray=curr_gray; continue

                # Rotation / insertion path
                if rotation_detected:
                    self._state=_TS.INSERTING; state_ts=time.time(); still_count=0
                    logger.info(f"[Tracker] PID={self._pid} → INSERTING "
                                f"area={area_ratio} angle={angle_delta}")

                # Flat placement path: QR gone + still
                elif qr_confirmed and qr_absent>QR_ABSENT_FAIL_S and still_count>=STILL_REQUIRED_FRAMES:
                    self._state=_TS.STABILIZING; state_ts=time.time()
                    logger.info(f"[Tracker] PID={self._pid} → STABILIZING (flat)")

                elif time.time()-state_ts>TRACKER_SUCCESS_TIMEOUT:
                    return self._fail("stabilization_timeout",tc)

            elif self._state==_TS.INSERTING:
                if still_count>=STILL_REQUIRED_FRAMES:
                    self._state=_TS.STABILIZING; state_ts=time.time()
                    logger.info(f"[Tracker] PID={self._pid} → STABILIZING (insertion)")
                elif time.time()-state_ts>INSERTION_TIMEOUT:
                    return self._fail("insertion_timeout",tc)

            elif self._state==_TS.STABILIZING:
                placed=self._run_verify(tc)
                if placed: return self._succeed(tc)
                return self._fail("phone_not_in_slot",tc)

            # ── Progress event ────────────────────────────────────────────────
            now=time.time()
            if now-last_emit>0.5:
                self._emit_update(qr_absent,in_roi,area_ratio,angle_delta)
                last_emit=now
            prev_gray=curr_gray

        self._fail("timeout",tc)

    # ── Geometry helpers ──────────────────────────────────────────────────────

    def _centroid_in_roi(self,cx,cy):
        """Centroid-based check — replaces old bbox overlap %."""
        rx,ry,rw,rh=self._slot_roi
        m=max(rw,rh)*ROI_APPROACH_MARGIN
        return rx-m<=cx<=rx+rw+m and ry-m<=cy<=ry+rh+m

    def _in_which_staging(self,bx,by,bw,bh):
        for i,roi in enumerate(self._staging_rois):
            if not roi or len(roi)<4: continue
            rx,ry,rw,rh=(int(v) for v in roi)
            ix1,iy1=max(bx,rx),max(by,ry)
            ix2,iy2=min(bx+bw,rx+rw),min(by+bh,ry+rh)
            if ix2<=ix1 or iy2<=iy1: continue
            if bw*bh>0 and (ix2-ix1)*(iy2-iy1)/(bw*bh)>=0.30: return i
        return -1

    # ── QR ────────────────────────────────────────────────────────────────────

    def _check_qr(self,frame):
        try:
            for obj in decode(frame):
                raw=obj.data.decode("utf-8",errors="ignore").strip()
                if raw.upper().startswith("PID:"): raw=raw[4:].strip()
                if _UUID_RE_TRACKER.match(raw) and raw.lower()==self._pid.lower():
                    return True
        except Exception: pass
        return False

    # ── Bottom-cam verify ─────────────────────────────────────────────────────

    def _run_verify(self,tc):
        if self._verify_fn is None:
            logger.debug(f"[Tracker] PID={self._pid} no verify_fn — assuming placed")
            return True
        try:
            ok=self._verify_fn()
            if not ok:
                logger.warning(f"[Tracker] PID={self._pid} LID={self._lid} "
                                "bottom-cam REJECTED — spoofing blocked")
            return ok
        except Exception as e:
            logger.warning(f"[Tracker] verify_fn raised {e} — assuming placed")
            return True

    # ── Terminal helpers ──────────────────────────────────────────────────────

    def _succeed(self,tc):
        tc.clear_tracker_overlay()
        logger.info(f"[Tracker] PID={self._pid} LID={self._lid} SUCCESS")
        self._state=_TS.SUCCESS
        if self._on_success: self._on_success()

    def _fail(self,reason,tc=None):
        if tc: tc.clear_tracker_overlay()
        logger.warning(f"[Tracker] PID={self._pid} LID={self._lid} FAILED: {reason}")
        self._state=_TS.FAILED
        if self._on_failure: self._on_failure(reason)

    # ── Emit ──────────────────────────────────────────────────────────────────

    def _emit_update(self,qr_absent,in_roi,area_ratio,angle_delta):
        try:
            self._socketio.emit("tracking_update",{
                "pid":self._pid,"lid":self._lid,
                "qr_visible":self._qr_visible,"in_roi":in_roi,
                "qr_absent":round(qr_absent,1),"state":self._state.name,
                "area_ratio":round(area_ratio,2) if area_ratio else None,
                "angle_delta":round(angle_delta,1) if angle_delta else None,
            },to=self._client_id,namespace="/")
        except Exception as e:
            logger.debug(f"[Tracker] emit_update: {e}")

    # ── Overlay ────────────────────────────────────────────────────────────────

    def _draw_overlay(self,frame):
        if self._bbox is None: return
        bx,by,bw,bh=self._bbox
        col={
            _TS.TRACKING:   _COL_TRACKING if self._qr_visible else _COL_QR_WARN,
            _TS.ENTERING:   _COL_ENTERING,
            _TS.INSERTING:  _COL_INSERTING,
            _TS.STABILIZING:_COL_STABILIZING,
        }.get(self._state,_COL_TRACKING)
        cv2.rectangle(frame,(bx,by),(bx+bw,by+bh),col,2)
        arm=max(6,min(14,bw//5,bh//5))
        x2,y2=bx+bw,by+bh
        for ax,ay,ex,ey,cx,cy in [
            (bx+arm,by,bx,by,bx,by+arm),(x2-arm,by,x2,by,x2,by+arm),
            (bx,y2-arm,bx,y2,bx+arm,y2),(x2,y2-arm,x2,y2,x2-arm,y2),
        ]:
            cv2.line(frame,(ax,ay),(ex,ey),col,2)
            cv2.line(frame,(ex,ey),(cx,cy),col,2)
        lbl={
            _TS.TRACKING:   "QR OK" if self._qr_visible else "QR LOST",
            _TS.ENTERING:   "APPROACHING SLOT",
            _TS.INSERTING:  "INSERTING...",
            _TS.STABILIZING:"VERIFYING...",
        }.get(self._state,"")
        _label(frame,lbl,bx,y2+16,col)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def create_tracker_for_operation(op, socketio, slot_ops=None):
    """
    Build a PhoneTracker for a DVW operation.
    slot_ops provides the bottom-camera verify_fn for placement confirmation.
    """
    from back_end.slot_monitor.camera.top_camera import top_camera
    if op.background_frame is None:
        top_camera.wait_for_frame(timeout=0.3)
        raw=top_camera.get_raw_frame()
        op.background_frame=raw if raw is not None else top_camera.get_frame()
    if op.background_frame is None:
        logger.warning(f"[Tracker] No background frame for PID={op.pid}"); return None
    slot_roi=_load_top_roi(op.lid)
    if slot_roi is None:
        logger.warning(f"[Tracker] No top-cam ROI for lid={op.lid}"); return None
    verify_fn=None
    if slot_ops is not None:
        try: verify_fn=slot_ops.make_placement_verifier(op.lid)
        except Exception as e: logger.warning(f"[Tracker] verify_fn error: {e}")
    return PhoneTracker(pid=op.pid,lid=op.lid,slot_roi=slot_roi,
                        background_frame=op.background_frame,
                        cancel_event=op.cancel_event,socketio=socketio,
                        client_id=op.client_id,verify_fn=verify_fn)