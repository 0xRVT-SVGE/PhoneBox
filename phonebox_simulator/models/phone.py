"""
Phone model.

A Phone is the single shared object observed by both the top (QR/back)
camera and the bottom camera.  Its mutable state (position, depth,
lifecycle state) is updated by the ScenarioEngine and read by both views.

Orientation convention
──────────────────────
The phone is always in LANDSCAPE orientation as seen from the top camera:
its long axis runs left-right.  The BACK of the phone faces up (toward
the top camera) while it travels through the transit lane and sits in the
slot.  The QR code is printed on a label on the back panel, so it IS
visible to the top camera — but because the phone arrives with the back
facing up, there is no need to rotate around the Z axis to hide the QR;
the phone simply descends straight down into the slot.

The 'rotation' field is kept for compatibility with the existing
ScenarioEngine; it is driven to 0 for the new entry behaviour and is
only used if the operator explicitly requests rotation (legacy toggle).
"""

from enum import Enum

import qrcode
from PySide6.QtGui import QImage

from phonebox_simulator.config import PhoneSpec


class PhoneState(Enum):
    OUTSIDE    = "outside"       # off-screen / at entry point
    ENTERING   = "entering"      # sliding left through transit lane
    DROPPING   = "dropping"      # dropping straight down into slot
    INSERTED   = "inserted"      # fully in the slot
    RISING     = "rising"        # rising out of slot (withdraw)
    EXITING    = "exiting"       # sliding right back to exit


class Phone:
    """A single simulated phone."""

    def __init__(self, spec: PhoneSpec):
        self.spec = spec

        # Scene-space position of the phone item's centre (top-camera coords).
        self.x: float = 0.0
        self.y: float = 0.0

        # Z-axis rotation in degrees.  0 = phone in landscape with back up.
        self.rotation: float = 0.0

        # 0.0 = above slot (not inserted), 1.0 = fully inserted.
        self.depth: float = 0.0

        self.slot_index: int | None = None
        self.state: PhoneState = PhoneState.OUTSIDE

        # QR image drawn on the back face (visible to top camera).
        self.qr_image: QImage = self._generate_qr_image()

    # ------------------------------------------------------------------
    def _generate_qr_image(self) -> QImage:
        qr = qrcode.QRCode(border=1, box_size=4)
        qr.add_data(self.spec.pid)
        qr.make(fit=True)

        pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        data = pil_img.tobytes("raw", "RGB")
        qimage = QImage(data, pil_img.width, pil_img.height, QImage.Format_RGB888)
        return qimage.copy()

    # ------------------------------------------------------------------
    @property
    def qr_visible(self) -> bool:
        """True when the back (QR) face is pointing toward the top camera.

        Because the phone enters back-face-up the QR is always visible
        while in transit and in the slot — unless the operator enables the
        legacy Z-rotation effect, in which case it disappears past 45 °.
        """
        norm = self.rotation % 180
        return norm < 45 or norm > 135