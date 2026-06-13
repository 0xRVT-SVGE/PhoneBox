"""
PhoneTopItem: renders a Phone as seen by the top (back-face / QR) camera.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen, QRadialGradient
from PySide6.QtWidgets import QGraphicsItem


class PhoneTopItem(QGraphicsItem):
    def __init__(self, phone):
        super().__init__()
        self.phone = phone
        self.setZValue(10)
        self.setTransformOriginPoint(0, 0)

    def _vis_w(self):
        return self.phone.spec.height

    def _vis_h(self):
        return self.phone.spec.width

    def boundingRect(self) -> QRectF:
        diag = (self._vis_w() ** 2 + self._vis_h() ** 2) ** 0.5
        return QRectF(-diag / 2, -diag / 2, diag, diag)

    def paint(self, painter, option, widget=None):
        phone = self.phone
        vw = self._vis_w()
        vh = self._vis_h()
        rect = QRectF(-vw / 2, -vh / 2, vw, vh)

        painter.setBrush(QBrush(QColor(phone.spec.body_color)))
        painter.setPen(QPen(QColor("#111111"), 1.5))
        painter.drawRoundedRect(rect, 6, 6)

        bevel = rect.adjusted(3, 3, -3, -3)
        bevel_color = QColor(phone.spec.body_color).lighter(115)
        bevel_color.setAlpha(60)
        painter.setBrush(QBrush(bevel_color))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(bevel, 4, 4)

        lens_cx = rect.left() + vh * 0.55
        lens_cy = rect.center().y()
        lens_r  = vh * 0.22
        grad = QRadialGradient(lens_cx, lens_cy, lens_r)
        grad.setColorAt(0.0, QColor("#1a1a2e"))
        grad.setColorAt(0.6, QColor("#0d0d1a"))
        grad.setColorAt(1.0, QColor("#333355"))
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(QColor("#444466"), 1))
        painter.drawEllipse(
            QRectF(lens_cx - lens_r, lens_cy - lens_r, lens_r * 2, lens_r * 2)
        )
        painter.setBrush(QBrush(QColor(255, 255, 255, 35)))
        painter.setPen(Qt.NoPen)
        hl_r = lens_r * 0.35
        painter.drawEllipse(
            QRectF(lens_cx - lens_r * 0.5, lens_cy - lens_r * 0.5, hl_r, hl_r)
        )

        led_x = lens_cx + lens_r + 4
        led_r = lens_r * 0.3
        painter.setBrush(QBrush(QColor("#ccaa44")))
        painter.drawEllipse(QRectF(led_x, lens_cy - led_r, led_r * 2, led_r * 2))

        qr_margin = 4
        qr_area = QRectF(
            rect.left() + lens_cx - rect.left() + lens_r * 2 + 8,
            rect.top() + qr_margin,
            rect.right() - (lens_cx + lens_r * 2 + 10) - qr_margin,
            rect.height() - qr_margin * 2,
        )

        if phone.qr_visible and qr_area.width() > 10:
            painter.setBrush(QBrush(QColor("#ffffff")))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(qr_area, 3, 3)
            qr_size = min(qr_area.width(), qr_area.height()) - 2
            qr_rect = QRectF(
                qr_area.center().x() - qr_size / 2,
                qr_area.center().y() - qr_size / 2,
                qr_size,
                qr_size,
            )
            painter.drawImage(qr_rect, phone.qr_image)
        else:
            painter.setPen(QPen(QColor("#555577")))
            font = QFont()
            font.setPointSize(6)
            painter.setFont(font)
            painter.drawText(qr_area, Qt.AlignCenter, "QR\nhidden")

        painter.save()
        painter.rotate(-self.rotation())
        painter.setPen(QPen(QColor("#cccccc")))
        lbl_font = QFont()
        lbl_font.setPointSize(7)
        painter.setFont(lbl_font)
        lbl_rect = QRectF(-vw / 2, vh / 2 + 3, vw, 14)
        painter.drawText(lbl_rect, Qt.AlignCenter, phone.spec.pid)
        painter.restore()
