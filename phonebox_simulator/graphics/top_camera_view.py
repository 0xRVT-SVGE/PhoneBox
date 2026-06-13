"""
TopCameraView: simulates the top (back-face / QR) camera.

Key design changes vs. the original:
  • The view always calls fitInView(sceneRect) so the full slot grid is
    always visible — no manual zoom or scrollbars needed.
  • A clearly labelled TRANSIT LANE is drawn above the slot grid; incoming
    phones travel left through it before dropping into their slot.
  • Slot labels show row/column notation (R1C1 …) and resize with the slot.
  • The light-leak overlay, blur and shake faults are unchanged.
"""

import random

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QTransform
from PySide6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
)

from phonebox_simulator.graphics.fault_overlay import LightLeakOverlay
from phonebox_simulator.graphics.phone_item import PhoneTopItem
from phonebox_simulator.models.slot_grid import TRANSIT_HEIGHT

# ── style constants ────────────────────────────────────────────────────
_SLOT_PEN_NORMAL    = QPen(QColor("#00cc88"), 1, Qt.DashLine)
_SLOT_PEN_HIGHLIGHT = QPen(QColor("#ffaa00"), 2, Qt.SolidLine)
_SLOT_BRUSH_EMPTY   = QBrush(QColor(255, 255, 255, 8))
_SLOT_BRUSH_OCCUPIED= QBrush(QColor(0, 200, 130, 30))
_TRANSIT_BRUSH      = QBrush(QColor(100, 160, 255, 18))
_TRANSIT_PEN        = QPen(QColor(100, 160, 255, 60), 1, Qt.DashLine)


class TopCameraView(QGraphicsView):
    def __init__(self, slot_grid, parent=None):
        super().__init__(parent)
        self.slot_grid = slot_grid

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor("#141414")))

        # Disable scrollbars — the view always fits the whole scene.
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        w, h = slot_grid.scene_size
        self._scene.setSceneRect(0, 0, w, h)

        self._draw_transit_lane(w)
        self._slot_items: dict[int, QGraphicsRectItem] = {}
        self._draw_slots()

        self.phone_items: dict[str, PhoneTopItem] = {}

        self.light_leak = LightLeakOverlay(w, h)
        self._scene.addItem(self.light_leak)

        # Blur
        self.blur_effect = QGraphicsBlurEffect()
        self.blur_effect.setBlurRadius(0)
        self.setGraphicsEffect(self.blur_effect)

        # Shake
        self._base_transform = QTransform()
        self._shake_intensity = 0.0
        self._shake_timer = QTimer(self)
        self._shake_timer.timeout.connect(self._apply_shake)

    # ------------------------------------------------------------------
    # Auto-fit on every resize so the grid always fills the view.
    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit()

    def _fit(self):
        if self._scene.sceneRect().isValid():
            self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
            # Store the new base transform so shake works in screen space.
            self._base_transform = self.transform()

    # ------------------------------------------------------------------
    # Scene drawing
    # ------------------------------------------------------------------
    def _draw_transit_lane(self, scene_w: float):
        """Blue-tinted band representing the transit lane."""
        lane = QGraphicsRectItem(0, 0, scene_w, TRANSIT_HEIGHT)
        lane.setBrush(_TRANSIT_BRUSH)
        lane.setPen(_TRANSIT_PEN)
        lane.setZValue(-1)
        self._scene.addItem(lane)

        label = QGraphicsTextItem("◀  transit lane  (phones enter from the right)")
        label.setDefaultTextColor(QColor(140, 180, 255, 160))
        f = QFont()
        f.setPointSize(7)
        label.setFont(f)
        label.setPos(6, TRANSIT_HEIGHT / 2 - 8)
        self._scene.addItem(label)

        # Dashed separator line between transit lane and slot grid.
        sep = QGraphicsRectItem(0, TRANSIT_HEIGHT - 1, scene_w, 1)
        sep.setBrush(QBrush(QColor(100, 160, 255, 80)))
        sep.setPen(Qt.NoPen)
        self._scene.addItem(sep)

    def _draw_slots(self):
        cfg = self.slot_grid.config
        for slot in self.slot_grid.slots:
            item = QGraphicsRectItem(slot.rect)
            item.setPen(_SLOT_PEN_NORMAL)
            item.setBrush(_SLOT_BRUSH_EMPTY)
            self._scene.addItem(item)

            # Label — font size scales with slot height.
            label = QGraphicsTextItem(f"R{slot.row + 1}C{slot.col + 1}")
            lbl_font = QFont()
            lbl_font.setPointSize(max(6, int(cfg.slot_height * 0.045)))
            label.setFont(lbl_font)
            label.setDefaultTextColor(QColor("#00cc88"))
            label.setPos(
                slot.rect.x() + 4,
                slot.rect.y() + 4,
            )
            self._scene.addItem(label)

            self._slot_items[slot.index] = item

    # ------------------------------------------------------------------
    # Slot state
    # ------------------------------------------------------------------
    def highlight_slot(self, index: int, active: bool):
        item = self._slot_items.get(index)
        if item:
            item.setPen(_SLOT_PEN_HIGHLIGHT if active else _SLOT_PEN_NORMAL)

    def set_slot_occupied(self, index: int, occupied: bool):
        item = self._slot_items.get(index)
        if item:
            item.setBrush(_SLOT_BRUSH_OCCUPIED if occupied else _SLOT_BRUSH_EMPTY)

    # ------------------------------------------------------------------
    # Phone management
    # ------------------------------------------------------------------
    def add_phone(self, phone) -> PhoneTopItem:
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
        # Hide phone from top camera once fully inserted (only bottom cam sees it).
        item.setVisible(phone.depth < 1.0)
        item.update()

    def remove_phone(self, pid: str):
        item = self.phone_items.pop(pid, None)
        if item:
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
        self.setTransform(QTransform(self._base_transform).translate(dx, dy))

    def set_zoom(self, factor: float):
        t = QTransform()
        t.scale(max(0.1, factor), max(0.1, factor))
        self._base_transform = t
        self.setTransform(t)