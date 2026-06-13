"""
BottomCameraView: simulates the camera mounted below a slot.

Shows only the bottom edge of the phone currently associated with the
selected slot (charging port, speaker holes, texture), sliding into and
out of frame as `phone.depth` changes. Supports the same fault set as
the top camera (light leak, blur, shake, zoom).
"""

import random

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QGraphicsBlurEffect, QWidget


class BottomCameraView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 360)

        self.phone = None
        self.selected_slot = None

        self.light_leak = 0.0
        self.zoom_factor = 1.0

        self.shake_intensity = 0.0
        self._shake_offset = (0.0, 0.0)
        self._shake_timer = QTimer(self)
        self._shake_timer.timeout.connect(self._update_shake)

        self.blur_effect = QGraphicsBlurEffect()
        self.blur_effect.setBlurRadius(0)
        self.setGraphicsEffect(self.blur_effect)

    # ------------------------------------------------------------------
    def set_selected_slot(self, index):
        self.selected_slot = index
        self.update()

    def set_phone(self, phone):
        self.phone = phone
        self.update()

    def refresh(self):
        self.update()

    # ------------------------------------------------------------------
    # Fault injection
    # ------------------------------------------------------------------
    def set_light_leak(self, value: float):
        self.light_leak = max(0.0, min(1.0, value))
        self.update()

    def set_blur(self, radius: float):
        self.blur_effect.setBlurRadius(max(0.0, radius))

    def set_zoom(self, factor: float):
        self.zoom_factor = max(0.1, factor)
        self.update()

    def set_shake(self, intensity: float):
        self.shake_intensity = max(0.0, min(1.0, intensity))
        if self.shake_intensity > 0 and not self._shake_timer.isActive():
            self._shake_timer.start(33)
        elif self.shake_intensity == 0:
            self._shake_timer.stop()
            self._shake_offset = (0.0, 0.0)
            self.update()

    def _update_shake(self):
        self._shake_offset = (
            random.uniform(-1, 1) * self.shake_intensity * 8,
            random.uniform(-1, 1) * self.shake_intensity * 8,
        )
        self.update()

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = QRectF(0, 0, self.width(), self.height())

        # camera shake + zoom, applied around the widget center
        center = rect.center()
        painter.translate(*self._shake_offset)
        if self.zoom_factor != 1.0:
            painter.translate(center)
            painter.scale(self.zoom_factor, self.zoom_factor)
            painter.translate(-center)

        # Slot interior background
        painter.fillRect(rect, QColor("#0a0a0a"))
        painter.setPen(QPen(QColor("#444444"), 2))
        painter.drawRect(rect.adjusted(4, 4, -4, -4))

        if self.selected_slot is None:
            painter.setPen(QColor("#666666"))
            painter.drawText(rect, Qt.AlignCenter, "No slot selected")
        else:
            painter.setPen(QColor("#00ffaa"))
            label_rect = QRectF(8, 4, rect.width() - 16, 20)
            painter.drawText(label_rect, Qt.AlignLeft, f"Slot {self.selected_slot} - Bottom View")

            if self.phone is not None and self.phone.depth > 0.01:
                self._draw_phone_bottom(painter, rect, self.phone)
            else:
                painter.setPen(QColor("#555555"))
                painter.drawText(rect, Qt.AlignCenter, "Empty")

        # Light leak overlay (drawn last, in the same transformed space)
        if self.light_leak > 0:
            gradient = QLinearGradient(0, 0, 0, self.height() * 0.5)
            top_color = QColor(255, 255, 220)
            top_color.setAlphaF(0.8 * self.light_leak)
            gradient.setColorAt(0.0, top_color)
            gradient.setColorAt(1.0, QColor(255, 255, 220, 0))
            painter.fillRect(rect, QBrush(gradient))

    def _draw_phone_bottom(self, painter, rect, phone):
        spec = phone.spec
        depth = max(0.0, min(1.0, phone.depth))

        margin = 20
        full_w = rect.width() - margin * 2
        full_h = rect.height() - margin * 2

        # The phone slides down from the top of the slot interior; the
        # visible height grows with insertion depth.
        ph_w = full_w * 0.7
        ph_h = full_h * depth
        x = (rect.width() - ph_w) / 2
        y = margin
        ph_rect = QRectF(x, y, ph_w, ph_h)

        painter.setBrush(QBrush(QColor(spec.body_color)))
        painter.setPen(QPen(QColor("#000000"), 1))
        painter.drawRoundedRect(ph_rect, 6, 6)

        if spec.has_texture and ph_h > 0:
            painter.setPen(QPen(QColor(255, 255, 255, 25), 1))
            yy = ph_rect.top() + 6
            while yy < ph_rect.bottom() - 4:
                painter.drawLine(ph_rect.left() + 4, yy, ph_rect.right() - 4, yy)
                yy += 4

        if spec.has_charging_port and depth > 0.4:
            port_y = ph_rect.bottom() - 18
            if port_y > ph_rect.top():
                port_w, port_h = 22, 6
                port_x = ph_rect.left() + (ph_rect.width() - port_w) / 2

                painter.setBrush(QBrush(QColor("#111111")))
                painter.setPen(QPen(QColor("#333333"), 1))
                painter.drawRoundedRect(QRectF(port_x, port_y, port_w, port_h), 2, 2)

                painter.setBrush(QBrush(QColor("#222222")))
                painter.setPen(Qt.NoPen)
                for i in range(4):
                    hx = ph_rect.left() + 12 + i * 8
                    painter.drawEllipse(QRectF(hx, port_y - 10, 2.5, 2.5))
