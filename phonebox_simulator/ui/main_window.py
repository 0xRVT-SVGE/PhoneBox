"""
MainWindow: assembles the PhoneBox digital twin.

Owns:
  - The SlotGrid (rebuilt whenever the grid config changes).
  - All Phone objects created during the session.
  - The TopCameraView and BottomCameraView.
  - One ScenarioEngine per in-flight deposit/withdraw animation.

Phone lifecycle:
  staging area  -> Deposit -> slot (occupied)
  slot (occupied) -> Withdraw -> staging area
Only one phone may sit in the staging area at a time.
"""

from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from phonebox_simulator.config import GridConfig, PhoneSpec
from phonebox_simulator.graphics.bottom_camera_view import BottomCameraView
from phonebox_simulator.graphics.top_camera_view import TopCameraView
from phonebox_simulator.models.phone import Phone
from phonebox_simulator.models.slot_grid import SlotGrid
from phonebox_simulator.simulation.scenario_engine import ScenarioEngine
from phonebox_simulator.ui.control_panel import ControlPanel


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PhoneBox Digital Twin - Simulator")

        self.grid_config = GridConfig()
        self.slot_grid = SlotGrid(self.grid_config)

        self.phones = {}              # pid -> Phone
        self.slot_occupants = {}      # slot_index -> pid or None
        self.slot_active_pid = {}     # slot_index -> pid (mid-animation)
        self.staging_pid = None
        self.engines = {}             # slot_index -> ScenarioEngine
        self.selected_slot = 0

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)

        # --- camera views -------------------------------------------------
        cameras_widget = QWidget()
        cameras_layout = QVBoxLayout(cameras_widget)

        self.top_view = TopCameraView(self.slot_grid)
        top_group = QGroupBox("Top camera (QR / slot grid view)")
        top_group_layout = QVBoxLayout(top_group)
        top_group_layout.addWidget(self.top_view)

        self.bottom_view = BottomCameraView()
        bottom_group = QGroupBox("Bottom camera (slot underside view)")
        bottom_group_layout = QVBoxLayout(bottom_group)
        bottom_group_layout.addWidget(self.bottom_view)

        cameras_layout.addWidget(top_group, 2)
        cameras_layout.addWidget(bottom_group, 1)

        # --- control panel --------------------------------------------------
        self.control_panel = ControlPanel()
        scroll = QScrollArea()
        scroll.setWidget(self.control_panel)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(340)
        scroll.setMaximumWidth(380)

        root_layout.addWidget(cameras_widget, 3)
        root_layout.addWidget(scroll, 1)

        self._top_group_layout = top_group_layout

        self._wire_signals()
        self.control_panel.set_slot_count(len(self.slot_grid))

        self.resize(1200, 800)

    # ------------------------------------------------------------------
    def _wire_signals(self):
        cp = self.control_panel

        cp.grid_apply_requested.connect(self._on_grid_apply)
        cp.create_phone_requested.connect(self._on_create_phone)
        cp.deposit_requested.connect(self._on_deposit)
        cp.withdraw_requested.connect(self._on_withdraw)
        cp.slot_changed.connect(self._on_slot_changed)

        self._connect_top_faults()

        cp.bottom_faults.light_leak_changed.connect(self.bottom_view.set_light_leak)
        cp.bottom_faults.shake_changed.connect(self.bottom_view.set_shake)
        cp.bottom_faults.blur_changed.connect(self.bottom_view.set_blur)
        cp.bottom_faults.zoom_changed.connect(self.bottom_view.set_zoom)

    def _connect_top_faults(self):
        cp = self.control_panel
        cp.top_faults.light_leak_changed.connect(self.top_view.set_light_leak)
        cp.top_faults.shake_changed.connect(self.top_view.set_shake)
        cp.top_faults.blur_changed.connect(self.top_view.set_blur)
        cp.top_faults.zoom_changed.connect(self.top_view.set_zoom)

    # ------------------------------------------------------------------
    # Grid rebuild
    # ------------------------------------------------------------------
    def _on_grid_apply(self, rows, cols, slot_w, slot_h):
        # Stop any running animations before tearing down the scene.
        for engine in list(self.engines.values()):
            engine.stop()
        self.engines.clear()

        self.grid_config = GridConfig(rows=rows, cols=cols, slot_width=slot_w, slot_height=slot_h)
        self.slot_grid = SlotGrid(self.grid_config)

        self.phones.clear()
        self.slot_occupants.clear()
        self.slot_active_pid.clear()
        self.staging_pid = None

        # Recreate the top view with the new grid.
        old_top_view = self.top_view
        self.top_view = TopCameraView(self.slot_grid)
        self._top_group_layout.replaceWidget(old_top_view, self.top_view)
        old_top_view.deleteLater()

        self._connect_top_faults()

        self.bottom_view.set_phone(None)
        self.bottom_view.set_selected_slot(None)

        self.control_panel.set_slot_count(len(self.slot_grid))
        self.control_panel.set_status("Grid rebuilt")

    # ------------------------------------------------------------------
    # Phone creation
    # ------------------------------------------------------------------
    def _on_create_phone(self, pid, has_port, has_texture, color_hex):
        if self.staging_pid is not None:
            # Replace the phone currently waiting in the staging area.
            old = self.phones.pop(self.staging_pid, None)
            if old is not None:
                self.top_view.remove_phone(self.staging_pid)
            self.staging_pid = None

        if pid in self.phones:
            self.control_panel.set_status(f"PID '{pid}' already exists")
            return

        spec = PhoneSpec(pid=pid, has_charging_port=has_port, has_texture=has_texture, body_color=color_hex)
        phone = Phone(spec)
        phone.x, phone.y = self.slot_grid.staging_pos
        phone.rotation = 0.0
        phone.depth = 0.0

        self.phones[pid] = phone
        self.top_view.add_phone(phone)
        self.staging_pid = pid
        self.control_panel.set_status(f"Phone {pid} ready in staging area")

    # ------------------------------------------------------------------
    # Slot selection
    # ------------------------------------------------------------------
    def _on_slot_changed(self, index):
        if index is None:
            return
        if self.selected_slot is not None:
            self.top_view.highlight_slot(self.selected_slot, active=False)

        self.selected_slot = index
        self.top_view.highlight_slot(index, active=True)
        self.bottom_view.set_selected_slot(index)
        self._refresh_bottom_phone(index)

    def _refresh_bottom_phone(self, slot_index):
        pid = self.slot_active_pid.get(slot_index) or self.slot_occupants.get(slot_index)
        phone = self.phones.get(pid) if pid else None
        self.bottom_view.set_phone(phone)

    # ------------------------------------------------------------------
    # Deposit
    # ------------------------------------------------------------------
    def _on_deposit(self):
        slot_index = self.control_panel.selected_slot()
        if slot_index is None:
            return
        if self.staging_pid is None:
            self.control_panel.set_status("No phone in staging area - create one first")
            return
        if self.slot_occupants.get(slot_index) is not None:
            self.control_panel.set_status(f"Slot {slot_index} is already occupied")
            return
        if slot_index in self.engines:
            self.control_panel.set_status(f"Slot {slot_index} is busy")
            return

        pid = self.staging_pid
        phone = self.phones[pid]
        slot = self.slot_grid.get(slot_index)

        engine = ScenarioEngine(phone, slot, self.slot_grid.staging_pos)
        self.engines[slot_index] = engine
        self.slot_active_pid[slot_index] = pid
        self.staging_pid = None

        engine.tick.connect(lambda: self._on_tick(pid, slot_index))
        engine.state_changed.connect(self.control_panel.set_status)
        engine.finished.connect(lambda: self._on_deposit_finished(slot_index, pid))

        if slot_index == self.selected_slot:
            self._refresh_bottom_phone(slot_index)

        self.control_panel.set_status(f"Depositing {pid} into slot {slot_index}...")
        engine.start_deposit(rotate_enabled=self.control_panel.rotate_enabled())

    def _on_deposit_finished(self, slot_index, pid):
        self.engines.pop(slot_index, None)
        self.slot_active_pid.pop(slot_index, None)
        self.slot_occupants[slot_index] = pid
        self.top_view.set_slot_occupied(slot_index, True)
        if slot_index == self.selected_slot:
            self._refresh_bottom_phone(slot_index)
        self.control_panel.set_status(f"{pid} deposited into slot {slot_index}")

    # ------------------------------------------------------------------
    # Withdraw
    # ------------------------------------------------------------------
    def _on_withdraw(self):
        slot_index = self.control_panel.selected_slot()
        if slot_index is None:
            return
        pid = self.slot_occupants.get(slot_index)
        if pid is None:
            self.control_panel.set_status(f"Slot {slot_index} is empty")
            return
        if self.staging_pid is not None:
            self.control_panel.set_status("Staging area is occupied - deposit or remove that phone first")
            return
        if slot_index in self.engines:
            self.control_panel.set_status(f"Slot {slot_index} is busy")
            return

        phone = self.phones[pid]
        slot = self.slot_grid.get(slot_index)

        engine = ScenarioEngine(phone, slot, self.slot_grid.staging_pos)
        self.engines[slot_index] = engine
        self.slot_active_pid[slot_index] = pid
        self.slot_occupants[slot_index] = None
        self.top_view.set_slot_occupied(slot_index, False)

        engine.tick.connect(lambda: self._on_tick(pid, slot_index))
        engine.state_changed.connect(self.control_panel.set_status)
        engine.finished.connect(lambda: self._on_withdraw_finished(slot_index, pid))

        self.control_panel.set_status(f"Withdrawing {pid} from slot {slot_index}...")
        engine.start_withdraw(rotate_enabled=self.control_panel.rotate_enabled())

    def _on_withdraw_finished(self, slot_index, pid):
        self.engines.pop(slot_index, None)
        self.slot_active_pid.pop(slot_index, None)
        self.staging_pid = pid
        if slot_index == self.selected_slot:
            self._refresh_bottom_phone(slot_index)
        self.control_panel.set_status(f"{pid} withdrawn to staging area")

    # ------------------------------------------------------------------
    # Per-tick refresh
    # ------------------------------------------------------------------
    def _on_tick(self, pid, slot_index):
        phone = self.phones.get(pid)
        if phone is None:
            return
        self.top_view.update_phone(phone)
        if slot_index == self.selected_slot:
            self.bottom_view.refresh()
