"""
BottomCameraView: simulates the camera mounted below a slot.

Improvements over original:
  • Slot-wall surround drawn as dark side rails so the phone appears to
    slide into a physical enclosure.
  • Speaker grille rendered as a proper grid of holes (not just dots on
    the wrong y).
  • Charging port connector pins rendered inside the port recess.
  • Scanline overlay for CCD-style realism.
  • Per-frame noise grain (low alpha) at high brightness.
  • Vignette darkening at the frame edges.
  • Depth label shows insertion % so operators can read progress.
  • Supports the same fault set as the top camera.
"""

import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QGraphicsBlurEffect, QWidget

# How many scanlines to draw across the full widget height.
_SCANLINE_COUNT = 80
# Noise grain alpha (0–255); keep very low so it's subliminal.
_NOISE_ALPHA = 18


class BottomCameraView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(260, 380)

        self.phone = None
        self.selected_slot = None

        self.light_leak  = 0.0
        self.zoom_factor = 1.0

        self.shake_intensity = 0.0
        self._shake_offset   = (0.0, 0.0)
        self._shake_timer    = QTimer(self)
        self._shake_timer.timeout.connect(self._update_shake)

        self.blur_effect = QGraphicsBlurEffect()
        self.blur_effect.setBlurRadius(0)
        self.setGraphicsEffect(self.blur_effect)

        # Noise timer — repaints at ~30 fps when noise would be visible.
        self._noise_timer = QTimer(self)
        self._noise_timer.setInterval(33)
        self._noise_timer.timeout.connect(self.update)

    # ------------------------------------------------------------------
    def set_selected_slot(self, index):
        self.selected_slot = index
        self.update()

    def set_phone(self, phone):
        self.phone = phone
        # Enable rolling noise repaint only when a phone is partially inserted.
        if phone is not None:
            self._noise_timer.start()
        else:
            self._noise_timer.stop()
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

        w, h = self.width(), self.height()
        rect = QRectF(0, 0, w, h)
        center = rect.center()

        # ── Camera shake + zoom ───────────────────────────────────────
        painter.save()
        painter.translate(*self._shake_offset)
        if self.zoom_factor != 1.0:
            painter.translate(center)
            painter.scale(self.zoom_factor, self.zoom_factor)
            painter.translate(-center)

        # ── Background — slot interior ────────────────────────────────
        painter.fillRect(rect, QColor("#0a0a0a"))

        if self.selected_slot is None:
            painter.restore()
            painter.setPen(QColor("#666666"))
            painter.drawText(rect, Qt.AlignCenter, "No slot selected")
            return

        self._draw_slot_walls(painter, rect)
        self._draw_header(painter, rect)

        if self.phone is not None and self.phone.depth > 0.01:
            self._draw_phone_bottom(painter, rect, self.phone)
            self._draw_depth_indicator(painter, rect, self.phone.depth)
        else:
            painter.setPen(QColor("#444444"))
            painter.drawText(rect, Qt.AlignCenter, "— slot empty —")

        # ── Post-process overlays (drawn in widget space, on top) ─────
        self._draw_scanlines(painter, rect)
        self._draw_noise(painter, rect)
        self._draw_vignette(painter, rect)

        if self.light_leak > 0:
            self._draw_light_leak(painter, rect)

        painter.restore()

    # ------------------------------------------------------------------
    # Scene sub-draws
    # ------------------------------------------------------------------
    def _draw_slot_walls(self, painter: QPainter, rect: QRectF):
        """Dark side rails simulating the physical slot enclosure."""
        rail_w = rect.width() * 0.08
        rail_color = QColor("#1a1a1a")

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(rail_color))
        # Left rail
        painter.drawRect(QRectF(0, 0, rail_w, rect.height()))
        # Right rail
        painter.drawRect(QRectF(rect.right() - rail_w, 0, rail_w, rect.height()))

        # Subtle inner edge highlight
        painter.setPen(QPen(QColor(80, 80, 80, 60), 1))
        painter.drawLine(QPointF(rail_w, 0), QPointF(rail_w, rect.height()))
        painter.drawLine(
            QPointF(rect.right() - rail_w, 0),
            QPointF(rect.right() - rail_w, rect.height()),
        )

    def _draw_header(self, painter: QPainter, rect: QRectF):
        painter.setPen(QColor("#00ffaa"))
        lbl = f"Slot {self.selected_slot} — Bottom View"
        if self.phone is not None and self.phone.depth > 0.01:
            lbl += f"  [{self.phone.spec.pid}]"
        painter.drawText(QRectF(8, 5, rect.width() - 16, 18), Qt.AlignLeft, lbl)

    def _draw_depth_indicator(self, painter: QPainter, rect: QRectF, depth: float):
        """Thin progress bar on the right edge showing insertion depth."""
        bar_w  = 6
        bar_h  = rect.height() - 40
        bar_x  = rect.right() - 14
        bar_y  = 28.0
        filled = bar_h * depth

        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 40, 30)))
        painter.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 3, 3)

        color = QColor("#00ffaa") if depth < 0.95 else QColor("#ffaa00")
        painter.setBrush(QBrush(color))
        painter.drawRoundedRect(
            QRectF(bar_x, bar_y + bar_h - filled, bar_w, filled), 3, 3
        )

        painter.setPen(QPen(color))
        font = painter.font()
        font.setPointSize(6)
        painter.setFont(font)
        pct = f"{int(depth * 100)}%"
        painter.drawText(
            QRectF(bar_x - 4, bar_y + bar_h + 2, bar_w + 8, 12),
            Qt.AlignCenter,
            pct,
        )

    def _draw_phone_bottom(self, painter: QPainter, rect: QRectF, phone):
        spec  = phone.spec
        depth = max(0.0, min(1.0, phone.depth))

        rail_w = rect.width() * 0.08
        margin_top = 28.0
        usable_w = rect.width() - rail_w * 2
        usable_h = rect.height() - margin_top

        ph_w = usable_w * 0.78
        ph_h = usable_h * depth          # grows as phone descends
        ph_x = rail_w + (usable_w - ph_w) / 2
        ph_y = margin_top
        ph_rect = QRectF(ph_x, ph_y, ph_w, ph_h)

        if ph_h < 2:
            return

        # ── Body ──────────────────────────────────────────────────────
        body_color = QColor(spec.body_color)
        painter.setBrush(QBrush(body_color))
        painter.setPen(QPen(QColor("#000000"), 1.2))
        painter.drawRoundedRect(ph_rect, 8, 8)

        # Side-edge reflection (thin bright strip on left)
        grad = QLinearGradient(ph_rect.left(), 0, ph_rect.left() + ph_w * 0.12, 0)
        grad.setColorAt(0.0, QColor(255, 255, 255, 30))
        grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(ph_rect, 8, 8)

        # ── Texture (horizontal brush lines on back panel) ─────────────
        if spec.has_texture and ph_h > 12:
            painter.setPen(QPen(QColor(255, 255, 255, 18), 0.8))
            yy = ph_rect.top() + 8
            while yy < ph_rect.bottom() - 4:
                painter.drawLine(
                    QPointF(ph_rect.left() + 5, yy),
                    QPointF(ph_rect.right() - 5, yy),
                )
                yy += 5

        # ── Speaker grille ─────────────────────────────────────────────
        if ph_h > 40:
            self._draw_speaker_grille(painter, ph_rect)

        # ── Charging port ──────────────────────────────────────────────
        if spec.has_charging_port and depth > 0.35:
            self._draw_charging_port(painter, ph_rect, depth)

    def _draw_speaker_grille(self, painter: QPainter, ph_rect: QRectF):
        """Realistic speaker grille: two rows of small oval perforations."""
        grille_w  = ph_rect.width() * 0.30
        grille_h  = 18.0
        grille_x  = ph_rect.left() + (ph_rect.width() - grille_w) / 2
        # Position near the top of the visible phone body.
        grille_y  = ph_rect.top() + max(8, ph_rect.height() * 0.08)

        if grille_y + grille_h > ph_rect.bottom() - 4:
            return

        cols = 7
        rows = 2
        hole_w = 3.0
        hole_h = 2.2
        col_gap = grille_w / (cols + 1)
        row_gap = grille_h / (rows + 1)

        painter.setPen(Qt.NoPen)
        for r in range(rows):
            for c in range(cols):
                hx = grille_x + col_gap * (c + 1) - hole_w / 2
                hy = grille_y + row_gap * (r + 1) - hole_h / 2
                # Hole recess — dark fill with tiny highlight
                painter.setBrush(QBrush(QColor("#080808")))
                painter.drawEllipse(QRectF(hx, hy, hole_w, hole_h))
                painter.setBrush(QBrush(QColor(255, 255, 255, 25)))
                painter.drawEllipse(QRectF(hx, hy, hole_w * 0.5, hole_h * 0.4))

    def _draw_charging_port(self, painter: QPainter, ph_rect: QRectF, depth: float):
        """USB-C style port with visible connector pins."""
        # Port appears near the bottom of the phone body.
        port_w  = 28.0
        port_h  = 9.0
        port_x  = ph_rect.left() + (ph_rect.width() - port_w) / 2
        port_y  = ph_rect.bottom() - 22.0

        if port_y < ph_rect.top() + 10:
            return

        # Outer shell
        shell_color = QColor("#1c1c1c")
        painter.setBrush(QBrush(shell_color))
        painter.setPen(QPen(QColor("#3a3a3a"), 1))
        painter.drawRoundedRect(QRectF(port_x - 2, port_y - 2, port_w + 4, port_h + 4), 3, 3)

        # Port recess interior
        painter.setBrush(QBrush(QColor("#0a0a0a")))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(port_x, port_y, port_w, port_h), 2, 2)

        # Connector tongue (centre bar)
        tongue_h = port_h * 0.45
        tongue_w = port_w * 0.60
        painter.setBrush(QBrush(QColor("#2e2e2e")))
        painter.drawRoundedRect(
            QRectF(
                port_x + (port_w - tongue_w) / 2,
                port_y + (port_h - tongue_h) / 2,
                tongue_w,
                tongue_h,
            ),
            1,
            1,
        )

        # Gold contact pins on the tongue
        pin_count = 6
        pin_w = 1.5
        pin_h = tongue_h * 0.65
        pin_gap = tongue_w / (pin_count + 1)
        tx = port_x + (port_w - tongue_w) / 2
        ty = port_y + (port_h - tongue_h) / 2 + (tongue_h - pin_h) / 2
        painter.setBrush(QBrush(QColor("#c8a84b")))
        for i in range(pin_count):
            px = tx + pin_gap * (i + 1) - pin_w / 2
            painter.drawRect(QRectF(px, ty, pin_w, pin_h))

    # ------------------------------------------------------------------
    # Post-process overlays
    # ------------------------------------------------------------------
    def _draw_scanlines(self, painter: QPainter, rect: QRectF):
        """Horizontal CCD scanlines — alternating faint dark bands."""
        painter.setPen(QPen(QColor(0, 0, 0, 28), 1))
        step = rect.height() / _SCANLINE_COUNT
        y = 0.0
        while y < rect.height():
            painter.drawLine(QPointF(0, y), QPointF(rect.width(), y))
            y += step * 2   # draw every other line

    def _draw_noise(self, painter: QPainter, rect: QRectF):
        """Sparse random pixel noise — simulates sensor grain."""
        painter.setPen(Qt.NoPen)
        for _ in range(120):
            nx = random.uniform(0, rect.width())
            ny = random.uniform(0, rect.height())
            brightness = random.randint(160, 255)
            alpha      = random.randint(8, _NOISE_ALPHA)
            painter.setBrush(QBrush(QColor(brightness, brightness, brightness, alpha)))
            painter.drawRect(QRectF(nx, ny, 1.5, 1.5))

    def _draw_vignette(self, painter: QPainter, rect: QRectF):
        """Radial darkening at the frame edges — lens vignette."""
        cx, cy = rect.center().x(), rect.center().y()
        r = max(rect.width(), rect.height()) * 0.72
        grad = QRadialGradient(cx, cy, r)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(0.65, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 120))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRect(rect)

    def _draw_light_leak(self, painter: QPainter, rect: QRectF):
        gradient = QLinearGradient(0, 0, 0, rect.height() * 0.55)
        top = QColor(255, 255, 200)
        top.setAlphaF(0.82 * self.light_leak)
        gradient.setColorAt(0.0, top)
        gradient.setColorAt(1.0, QColor(255, 255, 200, 0))
        painter.fillRect(rect, QBrush(gradient))
