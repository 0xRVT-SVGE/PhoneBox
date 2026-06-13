"""
TopCameraView: simulates the top (back-face / QR) camera.

Improvements over original:
  • Staging-area dock drawn in the transit lane so operators can see
    where a freshly created phone waits before deposit.
  • Scanline + noise overlays for CCD realism (same as bottom cam).
  • Vignette at the frame edges.
  • Slot index labels updated to R{r+1}C{c+1} notation and show
    occupant PID when a phone is inserted.
  • Light-leak, blur and shake faults unchanged.
"""

import random

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QRadialGradient,
    QTransform,
)
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
_SLOT_PEN_NORMAL     = QPen(QColor("#00cc88"), 1, Qt.DashLine)
_SLOT_PEN_HIGHLIGHT  = QPen(QColor("#ffaa00"), 2, Qt.SolidLine)
_SLOT_BRUSH_EMPTY    = QBrush(QColor(255, 255, 255, 8))
_SLOT_BRUSH_OCCUPIED = QBrush(QColor(0, 200, 130, 30))
_TRANSIT_BRUSH       = QBrush(QColor(100, 160, 255, 18))
_TRANSIT_PEN         = QPen(QColor(100, 160, 255, 60), 1, Qt.DashLine)
_STAGING_PEN         = QPen(QColor(255, 200, 60, 120), 1, Qt.DashLine)
_STAGING_BRUSH       = QBrush(QColor(255, 200, 60, 15))

# Scanlines / noise parameters
_SCANLINE_COUNT = 60
_NOISE_ALPHA    = 14


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
        self._draw_staging_dock()
        self._slot_items:  dict[int, QGraphicsRectItem]  = {}
        self._slot_labels: dict[int, QGraphicsTextItem]  = {}
        self._draw_slots()

        self.phone_items: dict[str, PhoneTopItem] = {}

        self.light_leak = LightLeakOverlay(w, h)
        self._scene.addItem(self.light_leak)

        # Blur
        self.blur_effect = QGraphicsBlurEffect()
        self.blur_effect.setBlurRadius(0)
        self.setGraphicsEffect(self.blur_effect)

        # Shake
        self._base_transform  = QTransform()
        self._shake_intensity = 0.0
        self._shake_timer     = QTimer(self)
        self._shake_timer.timeout.connect(self._apply_shake)

        # Noise repaint timer
        self._noise_timer = QTimer(self)
        self._noise_timer.setInterval(50)
        self._noise_timer.timeout.connect(self.viewport().update)
        self._noise_timer.start()

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

        label = QGraphicsTextItem("◀  TRANSIT LANE  (phones travel from right → slot)")
        label.setDefaultTextColor(QColor(140, 180, 255, 140))
        f = QFont()
        f.setPointSize(6)
        label.setFont(f)
        label.setPos(6, TRANSIT_HEIGHT / 2 - 7)
        self._scene.addItem(label)

        # Separator line.
        sep = QGraphicsRectItem(0, TRANSIT_HEIGHT - 1, scene_w, 1)
        sep.setBrush(QBrush(QColor(100, 160, 255, 80)))
        sep.setPen(Qt.NoPen)
        self._scene.addItem(sep)

    def _draw_staging_dock(self):
        """
        Yellow dashed dock box in the transit lane where a created phone waits.
        Gives operators a clear visual anchor before hitting Deposit.
        """
        sx, sy = self.slot_grid.staging_pos
        cfg    = self.slot_grid.config

        # Dock sized to match the phone's landscape footprint.
        dock_w = cfg.slot_height * 0.8   # landscape: long axis
        dock_h = cfg.slot_width  * 0.45  # landscape: short axis
        dock_x = sx - dock_w / 2
        dock_y = sy - dock_h / 2

        dock = QGraphicsRectItem(dock_x, dock_y, dock_w, dock_h)
        dock.setPen(_STAGING_PEN)
        dock.setBrush(_STAGING_BRUSH)
        dock.setZValue(-1)
        self._scene.addItem(dock)

        lbl = QGraphicsTextItem("STAGING")
        lbl.setDefaultTextColor(QColor(255, 200, 60, 160))
        f = QFont()
        f.setPointSize(5)
        f.setBold(True)
        lbl.setFont(f)
        lbl.setPos(dock_x + 2, dock_y - 14)
        self._scene.addItem(lbl)

    def _draw_slots(self):
        cfg = self.slot_grid.config
        for slot in self.slot_grid.slots:
            item = QGraphicsRectItem(slot.rect)
            item.setPen(_SLOT_PEN_NORMAL)
            item.setBrush(_SLOT_BRUSH_EMPTY)
            self._scene.addItem(item)

            label = QGraphicsTextItem(f"R{slot.row + 1}C{slot.col + 1}")
            lbl_font = QFont()
            lbl_font.setPointSize(max(6, int(cfg.slot_height * 0.045)))
            label.setFont(lbl_font)
            label.setDefaultTextColor(QColor("#00cc88"))
            label.setPos(slot.rect.x() + 4, slot.rect.y() + 4)
            self._scene.addItem(label)

            self._slot_items[slot.index]  = item
            self._slot_labels[slot.index] = label

    # ------------------------------------------------------------------
    # Slot state
    # ------------------------------------------------------------------
    def highlight_slot(self, index: int, active: bool):
        item = self._slot_items.get(index)
        if item:
            item.setPen(_SLOT_PEN_HIGHLIGHT if active else _SLOT_PEN_NORMAL)

    def set_slot_occupied(self, index: int, occupied: bool, pid: str = ""):
        item  = self._slot_items.get(index)
        label = self._slot_labels.get(index)
        if item:
            item.setBrush(_SLOT_BRUSH_OCCUPIED if occupied else _SLOT_BRUSH_EMPTY)
        if label:
            slot = self.slot_grid.slots[index]
            base = f"R{slot.row + 1}C{slot.col + 1}"
            label.setPlainText(f"{base}\n{pid}" if (occupied and pid) else base)

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
        item.setVisible(phone.depth < 1.0)
        item.update()

    def remove_phone(self, pid: str):
        item = self.phone_items.pop(pid, None)
        if item:
            self._scene.removeItem(item)

    # ------------------------------------------------------------------
    # Viewport painting override — scanlines, noise, vignette
    # ------------------------------------------------------------------
    def drawForeground(self, painter: QPainter, rect):
        """Called after all scene items; draws post-process overlays."""
        super().drawForeground(painter, rect)

        vr = self.viewport().rect()
        vrf = self.mapToScene(vr).boundingRect()

        self._draw_scanlines(painter, vrf)
        self._draw_noise(painter, vrf)
        self._draw_vignette(painter, vrf)

    def _draw_scanlines(self, painter: QPainter, rect):
        painter.save()
        painter.setPen(QPen(QColor(0, 0, 0, 22), 1))
        step = rect.height() / _SCANLINE_COUNT
        y = rect.top()
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), int(y), int(rect.right()), int(y))
            y += step * 2
        painter.restore()

    def _draw_noise(self, painter: QPainter, rect):
        painter.save()
        painter.setPen(Qt.NoPen)
        for _ in range(80):
            nx = random.uniform(rect.left(), rect.right())
            ny = random.uniform(rect.top(), rect.bottom())
            b  = random.randint(140, 255)
            a  = random.randint(6, _NOISE_ALPHA)
            painter.setBrush(QBrush(QColor(b, b, b, a)))
            painter.drawRect(int(nx), int(ny), 2, 2)
        painter.restore()

    def _draw_vignette(self, painter: QPainter, rect):
        painter.save()
        cx = rect.center().x()
        cy = rect.center().y()
        r  = max(rect.width(), rect.height()) * 0.70
        grad = QRadialGradient(cx, cy, r)
        grad.setColorAt(0.0,  QColor(0, 0, 0, 0))
        grad.setColorAt(0.60, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0,  QColor(0, 0, 0, 110))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRect(rect)
        painter.restore()

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
        t  = QTransform(self._base_transform)
        t.translate(dx, dy)
        self.setTransform(t)

    def set_zoom(self, factor: float):
        t = QTransform()
        t.scale(max(0.1, factor), max(0.1, factor))
        self._base_transform = t
        self.setTransform(t)
