"""
PhoneTopItem: renders a Phone as seen by the top (QR) camera.

The item draws the phone body and, when the phone's QR-bearing face is
turned toward the camera (phone.qr_visible), the generated QR code.
When the phone is rotated past 45 degrees the QR is no longer drawn.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen
from PySide6.QtWidgets import QGraphicsItem


class PhoneTopItem(QGraphicsItem):
    def __init__(self, phone):
        super().__init__()
        self.phone = phone
        self.setZValue(10)
        # Rotate around the center of the phone, which is the origin of
        # the item's local coordinate system (see boundingRect below).
        self.setTransformOriginPoint(0, 0)

    def boundingRect(self) -> QRectF:
        w, h = self.phone.spec.width, self.phone.spec.height
        # Use a square bounding box large enough to cover the item at
        # any rotation, so Qt repaints the correct region after rotating.
        diag = (w ** 2 + h ** 2) ** 0.5
        return QRectF(-diag / 2, -diag / 2, diag, diag)

    def paint(self, painter, option, widget=None):
        phone = self.phone
        w, h = phone.spec.width, phone.spec.height
        rect = QRectF(-w / 2, -h / 2, w, h)

        painter.setBrush(QBrush(QColor(phone.spec.body_color)))
        painter.setPen(QPen(QColor("#111111"), 2))
        painter.drawRoundedRect(rect, 8, 8)

        if phone.qr_visible:
            qr_size = min(w, h) * 0.7
            target = QRectF(-qr_size / 2, -qr_size / 2, qr_size, qr_size)
            painter.drawImage(target, phone.qr_image)
        else:
            painter.setPen(QPen(QColor("#999999")))
            font = painter.font()
            font.setPointSize(7)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignCenter, "QR\nhidden")

        # PID label under the phone, always readable regardless of rotation
        painter.save()
        painter.rotate(-self.rotation())
        painter.setPen(QPen(QColor("#dddddd")))
        label_rect = QRectF(-w, h / 2 + 4, 2 * w, 16)
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(label_rect, Qt.AlignCenter, phone.spec.pid)
        painter.restore()
