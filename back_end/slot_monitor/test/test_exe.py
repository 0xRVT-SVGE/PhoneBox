# File: test_one_camera.py
from test_virtual_cam import VirtualCameraManager
import cv2

manager = VirtualCameraManager()

# Real camera for QR scanning
manager.add_camera(
    camera_id=0,
    mode="real",
    real_camera_id=0  # Your physical camera index
)

# Mock camera for slot monitoring
manager.add_camera(
    camera_id=1,
    mode="mock"
)

manager.start_all()

while True:
    ret0, frame0 = manager.read(0)  # Real camera
    ret1, frame1 = manager.read(1)  # Mock camera

    if ret0:
        cv2.imshow("Real Camera", frame0)
    if ret1:
        cv2.imshow("Mock Camera", frame1)

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

manager.stop_all()
cv2.destroyAllWindows()