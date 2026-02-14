import cv2

# Open camera indices 0, 1, 2
caps = [
    cv2.VideoCapture(0),
    cv2.VideoCapture(1),
    cv2.VideoCapture(2)
]

window_names = ["Camera 0", "Camera 1", "Camera 2"]

while True:
    for i, cap in enumerate(caps):
        if cap.isOpened():
            ret, frame = cap.read()
            if ret:
                cv2.imshow(window_names[i], frame)

    # Press 'q' to quit
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

# Release all cameras
for cap in caps:
    cap.release()

cv2.destroyAllWindows()
