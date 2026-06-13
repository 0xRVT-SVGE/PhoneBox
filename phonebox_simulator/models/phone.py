"""
Phone model — shared state object read by both camera views.
"""

from enum import Enum

import qrcode
from PySide6.QtGui import QImage

from phonebox_simulator.config import PhoneSpec


class PhoneState(Enum):
    OUTSIDE  = "outside"
    ENTERING = "entering"
    DROPPING = "dropping"
    INSERTED = "inserted"
    RISING   = "rising"
    EXITING  = "exiting"


class Phone:
    def __init__(self, spec: PhoneSpec):
        self.spec = spec
        self.x: float = 0.0
        self.y: float = 0.0
        self.rotation: float = 0.0
        self.depth: float = 0.0
        self.slot_index: int | None = None
        self.state: PhoneState = PhoneState.OUTSIDE
        self.qr_image: QImage = self._generate_qr_image()

    def _generate_qr_image(self) -> QImage:
        qr = qrcode.QRCode(border=1, box_size=4)
        qr.add_data(self.spec.pid)
        qr.make(fit=True)
        pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        data = pil_img.tobytes("raw", "RGB")
        qimage = QImage(data, pil_img.width, pil_img.height, QImage.Format_RGB888)
        return qimage.copy()

    @property
    def qr_visible(self) -> bool:
        norm = self.rotation % 180
        return norm < 45 or norm > 135
