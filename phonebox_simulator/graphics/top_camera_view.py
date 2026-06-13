"""
TopCameraView: simulates the camera mounted above the box.

Shows:
  - The slot grid (with the currently-selected slot highlighted).
  - Each phone, rendered with its QR code (or hidden, depending on
    rotation).
  - Fault overlays: light leak, blur, shake, zoom.
"""

import random

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
)

from phonebox_simulator.graphics.fault_overlay import LightLeakOverlay
from phonebox_simulator.graphics.phone_item import PhoneTopItem

NORMAL_SLOT_PEN = QPen(QColor("#00ffaa"), 2, Qt.DashLine)
HIGHLIGHT_SLOT_PEN = QPen(QColor("#ffaa00"), 3, Qt.SolidLine)
OCCUPIED_SLOT_BRUSH = QBrush(QColor(0, 255, 170, 25))
EMPTY_SLOT_BRUSH = QBrush(QColor(255, 255, 255, 10))


class TopCameraView(QGraphicsView):
    def __init__(self, slot_grid, parent=None):
        super().__init__(parent)
        self.slot_grid = slot_grid

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor("#1a1a1a")))

        w, h = slot_grid.scene_size
        self._scene.setSceneRect(0, 0, w, h)

        self.slot_rect_items = {}
        self._draw_slots()

        self.phone_items = {}  # pid -> PhoneTopItem

        self.light_leak = LightLeakOverlay(w, h)
        self._scene.addItem(self.light_leak)

        # Blur fault, applied as a Qt graphics effect on the whole view.
        self.blur_effect = QGraphicsBlurEffect()
        self.blur_effect.setBlurRadius(0)
        self.setGraphicsEffect(self.blur_effect)

        # Shake fault: jitter the view transform on a timer.
        self._base_transform = QTransform()
        self._shake_intensity = 0.0
        self._shake_timer = QTimer(self)
        self._shake_timer.timeout.connect(self._apply_shake)

        self._zoom_factor = 1.0

    # ------------------------------------------------------------------
    # Slot grid drawing
    # ------------------------------------------------------------------
    def _draw_slots(self):
        for slot in self.slot_grid.slots:
            rect_item = QGraphicsRectItem(slot.rect)
            rect_item.setPen(NORMAL_SLOT_PEN)
            rect_item.setBrush(EMPTY_SLOT_BRUSH)
            self._scene.addItem(rect_item)

            label = QGraphicsTextItem(f"Slot {slot.index}")
            label.setDefaultTextColor(QColor("#00ffaa"))
            label.setPos(slot.rect.x() + 4, slot.rect.y() + 2)
            self._scene.addItem(label)

            self.slot_rect_items[slot.index] = rect_item

    def highlight_slot(self, index, active=True):
        item = self.slot_rect_items.get(index)
        if item is None:
            return
        item.setPen(HIGHLIGHT_SLOT_PEN if active else NORMAL_SLOT_PEN)

    def set_slot_occupied(self, index, occupied: bool):
        item = self.slot_rect_items.get(index)
        if item is None:
            return
        item.setBrush(OCCUPIED_SLOT_BRUSH if occupied else EMPTY_SLOT_BRUSH)

    # ------------------------------------------------------------------
    # Phone management
    # ------------------------------------------------------------------
    def add_phone(self, phone):
        item = PhoneTopItem(phone)
        item.setPos(phone.x, phone.y)
        item.setRotation(phone.rotation)
        self._scene.addItem(item)
        self.phone_items[phone.spec.pid] = item
        return item

    def update_phone(self, phone):
        item = self.phone_items.get(phone.spec.pid)
        if item is None:
            return
        item.setPos(phone.x, phone.y)
        item.setRotation(phone.rotation)
        # Once fully inserted the phone has "fallen below" the top camera's
        # view and is only visible to the bottom camera.
        item.setVisible(phone.depth < 1.0)
        item.update()

    def remove_phone(self, pid):
        item = self.phone_items.pop(pid, None)
        if item is not None:
            self._scene.removeItem(item)

    # ------------------------------------------------------------------
    # Fault injection
    # ------------------------------------------------------------------
    def set_light_leak(self, value: float):
        self.light_leak.set_intensity(value)

    def set_blur(self, radius: float):
        self.blur_effect.setBlurRadius(max(0.0, radius))

    def set_shake(self, intensity: float):
        self._shake_intensity = max(0.0, min(1.0, intensity))
        if self._shake_intensity > 0 and not self._shake_timer.isActive():
            self._shake_timer.start(33)
        elif self._shake_intensity == 0:
            self._shake_timer.stop()
            self.setTransform(self._base_transform)

    def _apply_shake(self):
        dx = random.uniform(-1, 1) * self._shake_intensity * 12
        dy = random.uniform(-1, 1) * self._shake_intensity * 12
        shaken = QTransform(self._base_transform).translate(dx, dy)
        self.setTransform(shaken)

    def set_zoom(self, factor: float):
        self._zoom_factor = max(0.1, factor)
        transform = QTransform()
        transform.scale(self._zoom_factor, self._zoom_factor)
        self._base_transform = transform
        self.setTransform(self._base_transform)
