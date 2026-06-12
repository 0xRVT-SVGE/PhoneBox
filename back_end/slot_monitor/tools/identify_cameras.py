"""
Quick camera identifier — run this BEFORE starting the server.
Opens each camera index in a labelled window so you can see exactly
what each index captures. Press Q to close a window and move to the next.
Run: python tools/identify_cameras.py
"""
import cv2

MAX_INDEX = 5   # test indices 0..4

for idx in range(MAX_INDEX):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
    if not cap.isOpened():
        print(f"  index {idx}: NOT FOUND / failed to open")
        continue

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"  index {idx}: opened  ({w}x{h}) — showing preview, press Q to continue")

    while True:
        ret, frame = cap.read()
        if not ret:
            print(f"  index {idx}: opened but no frames")
            break
        label = f"Camera index {idx}  ({w}x{h})  |  press Q to go to next"
        cv2.putText(frame, label, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.imshow(label, frame)
        if cv2.waitKey(1) & 0xFF in (ord('q'), ord('Q')):
            break

    cap.release()
    cv2.destroyAllWindows()

print("\nDone. Set config.py accordingly:")
print("  FRONT_CAM_INDEX  = <face camera index>")
print("  TOP_CAM_INDEX    = <top-down / QR camera index>")
print("  BOTTOM_CAM_INDEX = <slot monitor camera index>")
