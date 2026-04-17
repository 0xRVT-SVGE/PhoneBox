# ============================================================
# FILE: back_end/scanner_loop.py
# ============================================================
import time
import threading
import cv2
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import stop_scan
from back_end.camera_manager import cam_mgr

# Import once at module level — previously this was inside process_frame(),
# causing a dotted attribute walk on every single frame (~30/s × runtime).
try:
    from back_end.slot_monitor.camera.rolling_buffer import face_rolling_buffer as _face_buf
    _FACE_BUF_AVAILABLE = True
except Exception:
    _face_buf = None
    _FACE_BUF_AVAILABLE = False

current_overlay = {"face_verified": False, "barcode_verified": False, "current_name": "Idle"}


# ---------------- CALLBACK ----------------
def overlay_update(new_state):
    global current_overlay
    current_overlay = new_state


scanner_state.register_callback(overlay_update)


# ---------------- FRAME PROCESSING ----------------

# Cached ROI and debug text — avoids recomputing identical values every frame.
_cached_roi: tuple = ()          # (x1, y1, x2, y2, frame_w, frame_h)
_cached_debug_text: str = ""
_cached_debug_color: tuple = (0, 0, 255)


def process_frame(frame, timestamp, scanning=False, debug=True):
    """
    Process one camera frame.

    Two frame paths:
      - rframe (raw copy): fed to the face/barcode scanner and WebRTC
                           preview. Copied only when actually needed so
                           the debug overlay drawn on `frame` never
                           contaminates the scanner's input.
      - frame  (in-place): debug overlay drawn directly onto this; the
                           result is what gets streamed via WebRTC main.

    Args:
        frame:     Raw BGR frame from the camera (will be mutated if debug).
        timestamp: Capture time (time.time()).
        scanning:  Whether the scan worker is active.
        debug:     Whether to draw the ROI/status overlay onto frame.
    """
    global _cached_roi, _cached_debug_text, _cached_debug_color

    h, w = frame.shape[:2]

    # Recompute ROI only when frame dimensions change (effectively never).
    if len(_cached_roi) == 0 or _cached_roi[4] != w or _cached_roi[5] != h:
        _cached_roi = (0, h // 2, w // 2, h, w, h)
    roi_coords = _cached_roi[:4]  # (x1, y1, x2, y2)

    # Cache both Event.is_set() results — avoids calling them twice per frame.
    preview_active = scanner_state.preview_requested.is_set()
    photo_taken    = scanner_state.photo_taken_event.is_set()

    needs_raw = scanning or (preview_active and not photo_taken)

    # Copy only when something downstream actually needs the clean frame.
    rframe = frame.copy() if needs_raw else None

    # Feed raw frame to scan worker queue.
    if scanning and rframe is not None:
        if scanner_state.task_queue.full():
            try:
                scanner_state.task_queue.get_nowait()
            except Exception:
                pass
        scanner_state.task_queue.put((rframe, roi_coords, timestamp))

    # Feed raw frame to WebRTC preview stream (reuses the single is_set() result).
    if preview_active and not photo_taken and rframe is not None:
        scanner_state.set_rframe(rframe)
        scanner_state._preview_frame_event.set()

    # Draw debug overlay in-place on the original frame (main stream).
    if debug:
        cv2.rectangle(frame, (roi_coords[0], roi_coords[1]),
                      (roi_coords[2], roi_coords[3]), (255, 255, 0), 2)

        face_ok    = current_overlay.get("face_verified", False)
        barcode_ok = current_overlay.get("barcode_verified", False)
        name       = current_overlay.get("current_name", "Idle")
        color      = (0, 255, 0) if (face_ok and barcode_ok) else (0, 0, 255)

        # Rebuild the text string only when state actually changed.
        new_text = f"Face:{face_ok} | Barcode:{barcode_ok} | {name}"
        if new_text != _cached_debug_text or color != _cached_debug_color:
            _cached_debug_text  = new_text
            _cached_debug_color = color

        cv2.putText(frame, _cached_debug_text,
                    (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 2, _cached_debug_color, 4)

    # Push the (possibly annotated) frame to the main WebRTC stream.
    scanner_state.set_frame(frame)
    scanner_state._main_frame_event.set()

    # Feed the face rolling buffer (module-level import, no try/import overhead).
    if _FACE_BUF_AVAILABLE:
        try:
            _face_buf.push(frame)
        except Exception:
            pass  # never let evidence recording crash the scanner loop

    return frame


# ---------------- SCANNER LOOP ----------------
def scanner_loop(stop_event: threading.Event, debugwindow=True, debugroi=True):
    """
    Main camera capture loop.

    Owns scan_worker shutdown: when the loop exits for any reason
    (stop_event set or 'q' pressed) it stops the scan worker before returning.

    Args:
        stop_event:  Shared threading.Event from server_main.
        debugwindow: Show local OpenCV preview window.
        debugroi:    Draw ROI rectangle and status overlay on frames.
    """
    cap = cv2.VideoCapture(cam_mgr.index("front_cam"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    if not cap.isOpened():
        print("[-] Cannot open camera.")
        stop_event.set()  # propagate failure so other modules exit too
        return

    # Cache the dict reference — avoids re-hashing "running" on every frame.
    scan_request = scanner_state.scan_request

    try:
        while not stop_event.is_set():
            ret, frame = cap.read()
            if not ret:
                continue

            process_frame(
                frame,
                time.time(),
                scanning=scan_request["running"],
                debug=debugroi,
            )

            if debugwindow:
                cv2.imshow("Scanner Debug", frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    stop_event.set()
                    break
    finally:
        stop_scan()
        cap.release()
        if debugwindow:
            cv2.destroyAllWindows()