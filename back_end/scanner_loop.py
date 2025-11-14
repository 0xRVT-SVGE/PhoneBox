import threading, time
import cv2
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import scan_worker

worker_thread = None
current_overlay = {"face_verified": False, "barcode_verified": False, "current_name": "Idle"}

# ---------------- CALLBACK ----------------
def overlay_update(new_state):
    global current_overlay
    current_overlay = new_state

# Register callback once
scanner_state.register_callback(overlay_update)

# ---------------- WORKER ----------------
def start_worker():
    global worker_thread
    if not worker_thread or not worker_thread.is_alive():
        worker_thread = threading.Thread(target=scan_worker, daemon=True)
        worker_thread.start()

# ---------------- FRAME PROCESSING ----------------
def process_frame(frame, timestamp, scanning=False, debug=True):
    h, w = frame.shape[:2]
    roi_coords = (0, h // 2, w // 2, h)

    if scanning and not scanner_state.stop_requested:
        if scanner_state.task_queue.full():
            try:
                scanner_state.task_queue.get_nowait()
            except Exception:
                pass
        scanner_state.task_queue.put((frame.copy(), roi_coords, timestamp))

    if debug:
        cv2.rectangle(frame, (roi_coords[0], roi_coords[1]), (roi_coords[2], roi_coords[3]), (255, 255, 0), 2)
        face_ok = current_overlay.get("face_verified", False)
        barcode_ok = current_overlay.get("barcode_verified", False)
        name = current_overlay.get("current_name", "Idle")
        color = (0, 255, 0) if (face_ok and barcode_ok) else (0, 0, 255)
        cv2.putText(frame, f"Face:{face_ok} | Barcode:{barcode_ok} | {name}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    scanner_state.set_frame(frame)
    return frame

# ---------------- SCANNER LOOP ----------------
def scanner_loop(debugwindow=True, debugroi=True):
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[-] Cannot open camera.")
        return

    while True:
        ret, frame = cap.read()
        if not ret:
            continue

        # Restart worker if scanning restarted
        if scanner_state.scan_request["running"] and not scanner_state.stop_requested:
            start_worker()

        processed_frame = process_frame(
            frame,
            time.time(),
            scanning=scanner_state.scan_request["running"],
            debug=debugroi
        )

        if debugwindow:
            cv2.imshow("Scanner Debug", processed_frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break


    cap.release()
    if debugwindow:
        cv2.destroyAllWindows()
