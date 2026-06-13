"""
LightLeakOverlay: a scene-space gradient drawn on top of everything else
to simulate light leaking into the box from above.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QLinearGradient
from PySide6.QtWidgets import QGraphicsItem


class LightLeakOverlay(QGraphicsItem):
    def __init__(self, width: float, height: float):
        super().__init__()
        self.w = width
        self.h = height
        self.intensity = 0.0
        self.setZValue(1000)

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.w, self.h)

    def resize(self, width: float, height: float):
        self.w = width
        self.h = height
        self.update()

    def set_intensity(self, value: float):
        self.intensity = max(0.0, min(1.0, value))
        self.update()

    def paint(self, painter, option, widget=None):
        if self.intensity <= 0:
            return
        gradient = QLinearGradient(0, 0, 0, self.h * 0.6)
        top_color = QColor(255, 255, 220)
        top_color.setAlphaF(0.85 * self.intensity)
        bottom_color = QColor(255, 255, 220)
        bottom_color.setAlphaF(0.0)
        gradient.setColorAt(0.0, top_color)
        gradient.setColorAt(1.0, bottom_color)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(gradient))
        painter.drawRect(self.boundingRect())
