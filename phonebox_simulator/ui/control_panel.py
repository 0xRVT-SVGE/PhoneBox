"""
ControlPanel: all user-facing controls.

Sections:
  - Grid configuration (rows/cols/slot size) with an Apply button that
    rebuilds the whole simulator.
  - Phone creation (PID, charging port, texture, color).
  - Slot selection + Deposit/Withdraw buttons + rotate-on-insertion toggle.
  - Fault injection sliders for the top camera and the bottom camera.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt

PHONE_COLORS = {
    "Space Gray": "#2b2b2b",
    "Silver": "#c8c8c8",
    "Gold": "#d4af7a",
    "Blue": "#3a5fcd",
    "Red": "#b22222",
}


class FaultControlGroup(QGroupBox):
    """Light leak / shake / blur / zoom sliders for one camera."""

    light_leak_changed = Signal(float)
    shake_changed = Signal(float)
    blur_changed = Signal(float)
    zoom_changed = Signal(float)

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        layout = QFormLayout(self)

        self.light_leak_slider = self._make_slider(0, 100, 0)
        self.shake_slider = self._make_slider(0, 100, 0)
        self.blur_slider = self._make_slider(0, 20, 0)
        self.zoom_slider = self._make_slider(50, 200, 100)

        layout.addRow("Light leak", self.light_leak_slider)
        layout.addRow("Shake", self.shake_slider)
        layout.addRow("Blur", self.blur_slider)
        layout.addRow("Zoom", self.zoom_slider)

        self.light_leak_slider.valueChanged.connect(
            lambda v: self.light_leak_changed.emit(v / 100.0)
        )
        self.shake_slider.valueChanged.connect(
            lambda v: self.shake_changed.emit(v / 100.0)
        )
        self.blur_slider.valueChanged.connect(
            lambda v: self.blur_changed.emit(float(v))
        )
        self.zoom_slider.valueChanged.connect(
            lambda v: self.zoom_changed.emit(v / 100.0)
        )

    @staticmethod
    def _make_slider(minimum, maximum, value):
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(minimum)
        slider.setMaximum(maximum)
        slider.setValue(value)
        return slider


class ControlPanel(QWidget):
    grid_apply_requested = Signal(int, int, int, int)
    create_phone_requested = Signal(str, bool, bool, str)
    deposit_requested = Signal()
    withdraw_requested = Signal()
    slot_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        layout.addWidget(self._build_grid_group())
        layout.addWidget(self._build_phone_group())
        layout.addWidget(self._build_operation_group())

        self.top_faults = FaultControlGroup("Top camera faults")
        self.bottom_faults = FaultControlGroup("Bottom camera faults")
        layout.addWidget(self.top_faults)
        layout.addWidget(self.bottom_faults)

        layout.addStretch(1)

    # ------------------------------------------------------------------
    def _build_grid_group(self):
        group = QGroupBox("Slot grid configuration")
        form = QFormLayout(group)

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 10)
        self.rows_spin.setValue(2)

        self.cols_spin = QSpinBox()
        self.cols_spin.setRange(1, 10)
        self.cols_spin.setValue(3)

        self.slot_w_spin = QSpinBox()
        self.slot_w_spin.setRange(60, 400)
        self.slot_w_spin.setValue(180)
        self.slot_w_spin.setSuffix(" px")

        self.slot_h_spin = QSpinBox()
        self.slot_h_spin.setRange(80, 500)
        self.slot_h_spin.setValue(260)
        self.slot_h_spin.setSuffix(" px")

        apply_btn = QPushButton("Apply / Rebuild")
        apply_btn.clicked.connect(self._emit_grid_apply)

        form.addRow("Rows", self.rows_spin)
        form.addRow("Columns", self.cols_spin)
        form.addRow("Slot width", self.slot_w_spin)
        form.addRow("Slot height", self.slot_h_spin)
        form.addRow(apply_btn)
        return group

    def _emit_grid_apply(self):
        self.grid_apply_requested.emit(
            self.rows_spin.value(),
            self.cols_spin.value(),
            self.slot_w_spin.value(),
            self.slot_h_spin.value(),
        )

    # ------------------------------------------------------------------
    def _build_phone_group(self):
        group = QGroupBox("Phone")
        form = QFormLayout(group)

        self.pid_edit = QLineEdit("PHONE_001")

        self.port_check = QCheckBox("Has charging port")
        self.port_check.setChecked(True)

        self.texture_check = QCheckBox("Has texture")
        self.texture_check.setChecked(True)

        self.color_combo = QComboBox()
        self.color_combo.addItems(PHONE_COLORS.keys())

        create_btn = QPushButton("Create phone (staging area)")
        create_btn.clicked.connect(self._emit_create_phone)

        form.addRow("PID", self.pid_edit)
        form.addRow(self.port_check)
        form.addRow(self.texture_check)
        form.addRow("Color", self.color_combo)
        form.addRow(create_btn)
        return group

    def _emit_create_phone(self):
        color_hex = PHONE_COLORS[self.color_combo.currentText()]
        self.create_phone_requested.emit(
            self.pid_edit.text().strip() or "PHONE_001",
            self.port_check.isChecked(),
            self.texture_check.isChecked(),
            color_hex,
        )

    # ------------------------------------------------------------------
    def _build_operation_group(self):
        group = QGroupBox("Operation")
        form = QFormLayout(group)

        self.slot_combo = QComboBox()
        self.slot_combo.currentIndexChanged.connect(self.slot_changed.emit)

        self.rotate_check = QCheckBox("Rotate phone on insertion (hide QR)")
        self.rotate_check.setChecked(True)

        btn_row = QHBoxLayout()
        self.deposit_btn = QPushButton("Deposit")
        self.withdraw_btn = QPushButton("Withdraw")
        self.deposit_btn.clicked.connect(self.deposit_requested.emit)
        self.withdraw_btn.clicked.connect(self.withdraw_requested.emit)
        btn_row.addWidget(self.deposit_btn)
        btn_row.addWidget(self.withdraw_btn)

        self.status_label = QLabel("Ready")

        form.addRow("Target slot", self.slot_combo)
        form.addRow(self.rotate_check)
        form.addRow(btn_row)
        form.addRow("Status", self.status_label)
        return group

    # ------------------------------------------------------------------
    def set_slot_count(self, count: int):
        self.slot_combo.blockSignals(True)
        self.slot_combo.clear()
        for i in range(count):
            self.slot_combo.addItem(f"Slot {i}", i)
        self.slot_combo.blockSignals(False)
        if count > 0:
            self.slot_combo.setCurrentIndex(0)
            self.slot_changed.emit(0)

    def selected_slot(self) -> int:
        return self.slot_combo.currentData()

    def rotate_enabled(self) -> bool:
        return self.rotate_check.isChecked()

    def set_status(self, text: str):
        self.status_label.setText(text)
