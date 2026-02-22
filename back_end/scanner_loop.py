# ============================================================
# FILE: back_end/scanner_loop.py
# ============================================================
import time
import threading
import cv2
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import stop_scan

current_overlay = {"face_verified": False, "barcode_verified": False, "current_name": "Idle"}


# ---------------- CALLBACK ----------------
def overlay_update(new_state):
    global current_overlay
    current_overlay = new_state


scanner_state.register_callback(overlay_update)


# ---------------- FRAME PROCESSING ----------------
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
    h, w = frame.shape[:2]
    roi_coords = (0, h // 2, w // 2, h)

    needs_raw = scanning or (
        scanner_state.preview_requested.is_set()
        and not scanner_state.photo_taken_event.is_set()
    )

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

    # Feed raw frame to WebRTC preview stream.
    if (
        scanner_state.preview_requested.is_set()
        and not scanner_state.photo_taken_event.is_set()
        and rframe is not None
    ):
        scanner_state.set_rframe(rframe)
        scanner_state._preview_frame_event.set()

    # Draw debug overlay in-place on the original frame (main stream).
    if debug:
        cv2.rectangle(
            frame,
            (roi_coords[0], roi_coords[1]),
            (roi_coords[2], roi_coords[3]),
            (255, 255, 0), 2,
        )
        face_ok = current_overlay.get("face_verified", False)
        barcode_ok = current_overlay.get("barcode_verified", False)
        name = current_overlay.get("current_name", "Idle")
        color = (0, 255, 0) if (face_ok and barcode_ok) else (0, 0, 255)
        cv2.putText(
            frame,
            f"Face:{face_ok} | Barcode:{barcode_ok} | {name}",
            (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 2, color, 4,
        )

    # Push the (possibly annotated) frame to the main WebRTC stream.
    scanner_state.set_frame(frame)
    scanner_state._main_frame_event.set()
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
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)

    if not cap.isOpened():
        print("[-] Cannot open camera.")
        stop_event.set()  # propagate failure so other modules exit too
        return

    try:
        while not stop_event.is_set():
            ret, frame = cap.read()
            if not ret:
                continue

            processed_frame = process_frame(
                frame,
                time.time(),
                scanning=scanner_state.scan_request["running"],
                debug=debugroi,
            )

            if debugwindow:
                cv2.imshow("Scanner Debug", processed_frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    stop_event.set()  # signal every other module to stop
                    break
    finally:
        # scanner_loop owns scan_worker — always stop it before releasing camera
        stop_scan()
        cap.release()
        if debugwindow:
            cv2.destroyAllWindows()