"""
Phone model.

A Phone is the single shared object observed by both the top (QR) camera
and the bottom (slot) camera. Its mutable state (position, rotation,
insertion depth, lifecycle state) is updated by the ScenarioEngine and
read by both camera views.
"""

from enum import Enum

import qrcode
from PySide6.QtGui import QImage

from phonebox_simulator.config import PhoneSpec


class PhoneState(Enum):
    OUTSIDE = "outside"          # sitting in the staging area, not yet deposited
    APPROACHING = "approaching"  # moving toward the target slot
    ROTATING = "rotating"        # rotating to hide/reveal the QR code
    INSERTING = "inserting"      # sliding down into the slot (depth increasing)
    INSERTED = "inserted"        # fully inserted, resting in the slot
    WITHDRAWING = "withdrawing"  # sliding up out of the slot (depth decreasing)
    EXITING = "exiting"          # moving back to the staging area


class Phone:
    """A single simulated phone with its own QR code and bottom texture."""

    def __init__(self, spec: PhoneSpec):
        self.spec = spec

        # Position in the top-camera scene (scene coordinates, item center).
        self.x = 0.0
        self.y = 0.0

        # 0 deg -> QR code facing the top camera (visible).
        # 90 deg -> phone rotated on its side, QR no longer visible.
        self.rotation = 0.0

        # 0.0 -> phone is fully above the slot (not inserted).
        # 1.0 -> phone is fully inserted (visible to the bottom camera).
        self.depth = 0.0

        self.slot_index = None
        self.state = PhoneState.OUTSIDE

        self.qr_image: QImage = self._generate_qr_image()

    # ------------------------------------------------------------------
    def _generate_qr_image(self) -> QImage:
        """Generate a QImage containing a QR code that encodes the PID."""
        qr = qrcode.QRCode(border=1, box_size=4)
        qr.add_data(self.spec.pid)
        qr.make(fit=True)

        pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        data = pil_img.tobytes("raw", "RGB")

        qimage = QImage(data, pil_img.width, pil_img.height, QImage.Format_RGB888)
        # .copy() detaches the QImage from the temporary `data` buffer.
        return qimage.copy()

    # ------------------------------------------------------------------
    @property
    def qr_visible(self) -> bool:
        """True while the QR-bearing face points toward the top camera."""
        normalized = self.rotation % 180
        return normalized < 45 or normalized > 135
