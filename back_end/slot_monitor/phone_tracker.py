# ============================================================
# FILE: back_end/slot_monitor/phone_tracker.py
# ============================================================
"""
Phone Tracker  —  4-layer tracking + rotation-aware state machine.
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

from back_end.config import (
    TrackerConfig   as _TC,
    MotionConfig    as _MC,
    LKConfig        as _LK,
    OrbConfig       as _OC,
    OverlayConfig   as _OV,
)

import cv2
import numpy as np

_UUID_RE_TRACKER = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
_qr_detector_tracker = cv2.QRCodeDetector()
_orb_descriptor    = cv2.ORB_create(nfeatures=100)
_orb_reidentifier  = cv2.ORB_create(nfeatures=200)

logger = logging.getLogger(__name__)


# ── CSRT compatibility helper ────────────────────────────────────────────────
def _make_csrt_tracker():
    """
    Create a CSRT tracker compatible with any OpenCV 4.x build.

    OpenCV < 4.5  : cv2.TrackerCSRT_create()  (old top-level API)
    OpenCV 4.5+   : cv2.TrackerCSRT.create()  (class-method API)
    opencv-contrib: cv2.legacy.TrackerCSRT.create()
    """
    # Preferred: modern class-method API
    tracker_cls = getattr(cv2, 'TrackerCSRT', None)
    if tracker_cls is not None and hasattr(tracker_cls, 'create'):
        return tracker_cls.create()
    # Fallback: old factory function
    factory = getattr(cv2, 'TrackerCSRT_create', None)
    if factory is not None:
        return factory()
    # Last resort: contrib module
    legacy = getattr(cv2, 'legacy', None)
    if legacy is not None:
        cls = getattr(legacy, 'TrackerCSRT', None)
        if cls is not None:
            return cls.create()
    raise RuntimeError(
        "cv2.TrackerCSRT not found. "
        "Install opencv-contrib-python: pip install opencv-contrib-python"
    )


# ══════════════════════════════════════════════════════════════════════════════
# CONSTANTS  (sourced from back_end/config.py — edit there, not here)
# ══════════════════════════════════════════════════════════════════════════════

DETECT_TIMEOUT          = _TC.DETECT_TIMEOUT
PLACEMENT_TIMEOUT       = _TC.PLACEMENT_TIMEOUT
INSERTION_TIMEOUT       = _TC.INSERTION_TIMEOUT
STABILIZING_TIMEOUT     = _TC.STABILIZING_TIMEOUT
TRACKER_SUCCESS_TIMEOUT = _TC.TRACKER_SUCCESS_TIMEOUT

QR_CHECK_EVERY_N  = _TC.QR_CHECK_EVERY_N
QR_ABSENT_FAIL_S  = _TC.QR_ABSENT_FAIL_S

ROI_APPROACH_MARGIN = _TC.ROI_APPROACH_MARGIN
ROI_APPROACH_FRAMES = _TC.ROI_APPROACH_FRAMES

AREA_REDUCTION_TRIGGER = _TC.AREA_REDUCTION_TRIGGER
ANGLE_SWING_TRIGGER    = _TC.ANGLE_SWING_TRIGGER
MIN_TRACK_AREA_PX      = _TC.MIN_TRACK_AREA_PX

STILL_VEL_THRESHOLD   = _TC.STILL_VEL_THRESHOLD
STILL_REQUIRED_FRAMES = _TC.STILL_REQUIRED_FRAMES
STILL_PENALTY_ON_MOVE = _TC.STILL_PENALTY_ON_MOVE

MOTION_BLUR_K        = _MC.BLUR_K
MOTION_THRESH        = _MC.THRESH
MOTION_DILATE        = _MC.DILATE
MOTION_MIN_AREA      = _MC.MIN_AREA
MOTION_IOU_MERGE     = _MC.IOU_MERGE
CSRT_REINIT_INTERVAL = _MC.CSRT_REINIT_INTERVAL
CSRT_MOTION_GATE_N   = _MC.CSRT_MOTION_GATE_N

LK_MAX_POINTS   = _LK.MAX_POINTS
LK_MIN_POINTS   = _LK.MIN_POINTS
LK_GOOD_QUALITY = _LK.GOOD_QUALITY
LK_WIN_SIZE     = _LK.WIN_SIZE
LK_MAX_LEVEL    = _LK.MAX_LEVEL
LK_CRITERIA     = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03)

RE_ID_EVERY_N       = _OC.RE_ID_EVERY_N
ORB_MATCH_THRESHOLD = _OC.MATCH_THRESHOLD
ORB_MIN_MATCHES     = _OC.MIN_MATCHES

STAGING_HOLD_TIME = _TC.STAGING_HOLD_TIME

# BGR colour palette (from config.OverlayConfig)
_COL_SOURCE        = _OV.COL_SOURCE
_COL_DEST_BASE     = _OV.COL_DEST_BASE
_COL_STAGING_EMPTY = _OV.COL_STAGING_EMPTY
_COL_STAGING_OCC   = _OV.COL_STAGING_OCC
_COL_TRACKING      = _OV.COL_TRACKING
_COL_QR_WARN       = _OV.COL_QR_WARN
_COL_ENTERING      = _OV.COL_ENTERING
_COL_INSERTING     = _OV.COL_INSERTING
_COL_STABILIZING   = _OV.COL_STABILIZING
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
            _label(frame,f"SLOT {dest_lid+1}  - PLACE HERE",rx+4,ry+rh+17,col)
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
                _label(frame,f"[*] {name}  [{tail}]",rx+4,ry+rh//2+7,col)
            else:
                _draw_dashed_rect(frame,rx,ry,rw,rh,col,2,dash=14,gap=6)
                _label(frame,f"[ ] {name}  - empty",rx+4,ry+rh//2+7,col)
        if source_lid is not None and source_lid in all_slot_rois:
            rx,ry,rw,rh = all_slot_rois[source_lid]
            same = (source_lid==dest_lid)
            _fill_alpha(frame,rx,ry,rw,rh,_COL_SOURCE,0.11)
            _draw_dashed_rect(frame,rx,ry,rw,rh,_COL_SOURCE,2)
            lbl = f"FROM  slot {source_lid+1}" + (" <> RETURN HERE" if same else "")
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
            _label(frame,f"SLOT {dest_lid+1}  - PLACE HERE",rx+4,ry+rh+17,col)
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
    x,y,w,h=(int(v) for v in bbox)
    if w<10 or h<10: return None
    roi=gray[y:y+h, x:x+w]
    pts=cv2.goodFeaturesToTrack(roi,maxCorners=LK_MAX_POINTS,
                                qualityLevel=LK_GOOD_QUALITY,minDistance=6,blockSize=7)
    if pts is None or len(pts)<LK_MIN_POINTS: return None
    pts[:,0,0]+=x; pts[:,0,1]+=y
    return pts


def _lk_update(prev_gray, curr_gray, prev_pts):
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
    kps, descs = _orb_descriptor.detectAndCompute(roi, None)
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
    kps,descs=_orb_reidentifier.detectAndCompute(sg,None)
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


class _RotationSignal:
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
    """

    # ── Debug toggle ─────────────────────────────────────────────────────────
    # Set False to hide the phone bounding-box overlay on the top-camera feed.
    # Can also be toggled at runtime: PhoneTracker.DRAW_TRACKING_BOX = False
    DRAW_TRACKING_BOX: bool = True

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
        self._bbox:       Optional[Tuple] = None
        self._state:      _TS             = _TS.DETECTING
        self._qr_visible: bool            = True
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

    def _detect_phone(self, tc):
        _raw = tc.get_raw_frame()
        bg = _raw if _raw is not None else self._bg
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
            # ── Use compat helper instead of cv2.TrackerCSRT_create() ──
            csrt=_make_csrt_tracker(); csrt.init(frame,bbox)
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

    def _track(self, csrt, tc):  # noqa: C901
        deadline        = time.time()+PLACEMENT_TIMEOUT
        last_emit       = 0.0
        frame_count     = 0
        last_qr_ts      = time.time()
        qr_confirmed    = False
        self._qr_visible= True
        csrt_ok         = True
        reinit_count    = 0
        lk_pts          = None
        prev_gray       = None
        orb_ref_descs   = None
        last_bbox       = None
        rot_sig         = _RotationSignal()
        prev_cx=prev_cy = None
        still_count     = 0
        roi_count       = 0
        state_ts        = time.time()
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

            #Skip QR decode in states where QR is no longer needed.
            # INSERTING/STABILIZING: phone is already in the slot area;
            # QR loss at this point is expected and should not fail the operation.
            _qr_check_needed = self._state in (_TS.DETECTING, _TS.TRACKING, _TS.ENTERING)
            if _qr_check_needed and frame_count % QR_CHECK_EVERY_N == 0:
            #remove this to disable deposit failure when entering slot _qr_check_needed
                if self._check_qr(frame):
                    last_qr_ts = time.time()
                    qr_confirmed = True
            qr_absent = time.time() - last_qr_ts
            self._qr_visible = (qr_absent < QR_ABSENT_FAIL_S)

            bx=by=bw=bh=0
            if csrt_ok:
                 ok,raw=csrt.update(frame)
                 if ok:
                     bx,by,bw,bh=(int(v) for v in raw)
                     if bx+bw<=0 or bx>=fw or by+bh<=0 or by>=fh:
                         csrt_ok=False
                     else:
                         reinit_count+=1
                         # Opt #13: motion-bbox merge every CSRT_MOTION_GATE_N (=5) frames.
                         # More frequent than CSRT reinit (=12) for responsive correction.
                         if prev_gray is not None and frame_count % CSRT_MOTION_GATE_N == 0:
                             pb=cv2.GaussianBlur(prev_gray,(MOTION_BLUR_K,)*2,0)
                             cb=cv2.GaussianBlur(curr_gray,(MOTION_BLUR_K,)*2,0)
                             mo=_motion_bbox(pb,cb)
                             if mo and _iou((bx,by,bw,bh),mo)>=MOTION_IOU_MERGE:
                                 bx,by,bw,bh=_merge_bbox((bx,by,bw,bh),mo)

                         if reinit_count>=CSRT_REINIT_INTERVAL:
                            # ── compat helper ──
                            csrt=_make_csrt_tracker(); csrt.init(frame,(bx,by,bw,bh))
                            reinit_count=0
                 else:
                    csrt_ok=False

            if not csrt_ok:
                if prev_gray is not None:
                    pb=cv2.GaussianBlur(prev_gray,(MOTION_BLUR_K,)*2,0)
                    cb=cv2.GaussianBlur(curr_gray,(MOTION_BLUR_K,)*2,0)
                    mo=_motion_bbox(pb,cb)
                else: mo=None

                if mo:
                    bx,by,bw,bh=mo
                    # ── compat helper ──
                    csrt=_make_csrt_tracker(); csrt.init(frame,mo)
                    csrt_ok=True; reinit_count=0; lk_pts=None

                elif lk_pts is not None and prev_gray is not None:
                    lk_pts,lk_bbox=_lk_update(prev_gray,curr_gray,lk_pts)
                    if lk_bbox: bx,by,bw,bh=lk_bbox
                    else: bx=by=bw=bh=0

                elif frame_count%RE_ID_EVERY_N==0 and orb_ref_descs is not None:
                    rec=_orb_reidentify(curr_gray,orb_ref_descs,last_bbox)
                    if rec:
                        bx,by,bw,bh=rec
                        # ── compat helper ──
                        csrt=_make_csrt_tracker(); csrt.init(frame,rec)
                        csrt_ok=True; reinit_count=0; lk_pts=None
                        logger.debug(f"[Tracker] PID={self._pid} ORB re-ID")
                    else:
                        if self._state in (_TS.ENTERING,_TS.INSERTING):
                            placed=self._run_verify(tc)
                            if placed: return self._succeed(tc)
                            return self._fail("phone_not_in_slot",tc)
                        return self._fail("tracker_lost",tc)
                else:
                    prev_gray=curr_gray; continue

            if bw==0 or bh==0: prev_gray=curr_gray; continue

            if csrt_ok and (lk_pts is None or frame_count%10==0):
                new_lk=_lk_init(curr_gray,(bx,by,bw,bh))
                if new_lk is not None: lk_pts=new_lk

            if orb_ref_descs is None and self._state==_TS.TRACKING and qr_confirmed:
                _,orb_ref_descs=_orb_descriptors(curr_gray,(bx,by,bw,bh))

            self._bbox=(bx,by,bw,bh); last_bbox=(bx,by,bw,bh)
            cx,cy=centroid(bx,by,bw,bh)

            if prev_cx is not None:
                vel=math.hypot(cx-prev_cx,cy-prev_cy)
                if vel<STILL_VEL_THRESHOLD: still_count=min(still_count+1,STILL_REQUIRED_FRAMES+5)
                else: still_count=max(0,still_count-STILL_PENALTY_ON_MOVE)
            prev_cx,prev_cy=cx,cy

            area_ratio,angle_delta=rot_sig.update(curr_gray,(bx,by,bw,bh))
            rotation_detected=(
                (area_ratio  is not None and area_ratio <AREA_REDUCTION_TRIGGER) or
                (angle_delta is not None and angle_delta>ANGLE_SWING_TRIGGER)
            )

            in_roi=self._centroid_in_roi(cx,cy)

            if self._state==_TS.TRACKING:
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

                if qr_confirmed and qr_absent>QR_ABSENT_FAIL_S and not in_roi and stg_ts is None:
                    return self._fail("qr_lost",tc)

                if in_roi:
                    roi_count+=1
                    if roi_count>=ROI_APPROACH_FRAMES:
                        self._state=_TS.ENTERING; state_ts=time.time(); still_count=0
                        logger.info(f"[Tracker] PID={self._pid}  ENTERING")
                else:
                    roi_count=0

            elif self._state==_TS.ENTERING:
                if not in_roi:
                    self._state=_TS.TRACKING; roi_count=0
                    prev_gray=curr_gray; continue

                if rotation_detected:
                    self._state=_TS.INSERTING; state_ts=time.time(); still_count=0
                    logger.info(f"[Tracker] PID={self._pid}  INSERTING "
                                f"area={area_ratio} angle={angle_delta}")

                elif qr_confirmed and qr_absent>QR_ABSENT_FAIL_S and still_count>=STILL_REQUIRED_FRAMES:
                    self._state=_TS.STABILIZING; state_ts=time.time()
                    logger.info(f"[Tracker] PID={self._pid}  STABILIZING (flat)")

                elif time.time()-state_ts>TRACKER_SUCCESS_TIMEOUT:
                    return self._fail("stabilization_timeout",tc)

            elif self._state==_TS.INSERTING:
                if still_count>=STILL_REQUIRED_FRAMES:
                    self._state=_TS.STABILIZING; state_ts=time.time()
                    logger.info(f"[Tracker] PID={self._pid}  STABILIZING (insertion)")
                elif time.time()-state_ts>INSERTION_TIMEOUT:
                    return self._fail("insertion_timeout",tc)

            elif self._state==_TS.STABILIZING:
                placed=self._run_verify(tc)
                if placed: return self._succeed(tc)
                return self._fail("phone_not_in_slot",tc)

            now=time.time()
            if now-last_emit>0.5:
                self._emit_update(qr_absent,in_roi,area_ratio,angle_delta)
                last_emit=now
            prev_gray=curr_gray

        self._fail("timeout",tc)

    def _centroid_in_roi(self,cx,cy):
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

    def _check_qr(self, frame: np.ndarray) -> bool:

     try:
           # Convert to gray once — cv2.QRCodeDetector works faster on grayscale
           gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
           data, _, _ = _qr_detector_tracker.detectAndDecode(gray)
           if not data:
               return False
           raw = data.strip()
           if raw.upper().startswith("PID:"):
               raw = raw[4:].strip()
           return bool(_UUID_RE_TRACKER.match(raw) and raw.lower() == self._pid.lower())
     except Exception:
           return False

    def _run_verify(self,tc):
        if self._verify_fn is None:
            logger.debug(f"[Tracker] PID={self._pid} no verify_fn -- assuming placed")
            return True
        try:
            ok=self._verify_fn()
            if not ok:
                logger.warning(f"[Tracker] PID={self._pid} LID={self._lid} "
                                "bottom-cam REJECTED -- spoofing blocked")
            return ok
        except Exception as e:
            logger.warning(f"[Tracker] verify_fn raised {e} -- assuming placed")
            return True

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

    def _draw_overlay(self,frame):
        if not PhoneTracker.DRAW_TRACKING_BOX:
            return
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
    """Build a PhoneTracker for a DVW operation."""
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