"""
ScenarioEngine: drives the deposit/withdraw animations.

Each engine instance owns one Phone for the duration of one animation.
On every tick it mutates the phone's position, rotation and insertion
depth, and emits `tick` so views can refresh and `finished` when the
animation completes.

Deposit timeline (progress 0 -> 1):
    0.0 - 0.4  APPROACHING : phone slides from the staging area to the
                             slot's (x, y), QR facing up, depth = 0.
    0.4 - 0.6  ROTATING    : phone rotates 0 -> 90 deg over the slot
                             (only if rotate_enabled), QR disappears.
    0.6 - 1.0  INSERTING   : phone slides down into the slot,
                             depth 0 -> 1.

Withdraw is the mirror image: INSERTING reversed, then ROTATING back to
0 deg, then EXITING back to the staging area.
"""

from PySide6.QtCore import QObject, QTimer, Signal


class ScenarioEngine(QObject):
    tick = Signal()
    state_changed = Signal(str)
    finished = Signal()

    def __init__(self, phone, slot, staging_pos, tick_ms=33):
        super().__init__()
        self.phone = phone
        self.slot = slot
        self.staging_pos = staging_pos
        self.tick_ms = tick_ms

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self.mode = None
        self.duration = 2.5
        self.rotate_enabled = True
        self._t = 0.0

    # ------------------------------------------------------------------
    def start_deposit(self, duration=2.5, rotate_enabled=True):
        from phonebox_simulator.models.phone import PhoneState

        self.mode = "deposit"
        self.duration = max(0.1, duration)
        self.rotate_enabled = rotate_enabled
        self._t = 0.0

        self.phone.x, self.phone.y = self.staging_pos
        self.phone.rotation = 0.0
        self.phone.depth = 0.0
        self.phone.slot_index = self.slot.index
        self.phone.state = PhoneState.APPROACHING

        self._timer.start(self.tick_ms)

    def start_withdraw(self, duration=2.5, rotate_enabled=True):
        from phonebox_simulator.models.phone import PhoneState

        self.mode = "withdraw"
        self.duration = max(0.1, duration)
        self.rotate_enabled = rotate_enabled
        self._t = 0.0

        center = self.slot.rect.center()
        self.phone.x, self.phone.y = center.x(), center.y()
        self.phone.rotation = 90.0 if rotate_enabled else 0.0
        self.phone.depth = 1.0
        self.phone.state = PhoneState.WITHDRAWING

        self._timer.start(self.tick_ms)

    def stop(self):
        self._timer.stop()

    # ------------------------------------------------------------------
    def _on_tick(self):
        from phonebox_simulator.models.phone import PhoneState

        self._t += self.tick_ms / 1000.0
        progress = min(1.0, self._t / self.duration)

        center = self.slot.rect.center()
        tx, ty = center.x(), center.y()
        sx, sy = self.staging_pos

        if self.mode == "deposit":
            if progress < 0.4:
                p = progress / 0.4
                self.phone.x = sx + (tx - sx) * p
                self.phone.y = sy + (ty - sy) * p
                self.phone.rotation = 0.0
                self.phone.depth = 0.0
                self.phone.state = PhoneState.APPROACHING
            elif progress < 0.6:
                p = (progress - 0.4) / 0.2
                self.phone.x, self.phone.y = tx, ty
                self.phone.rotation = 90.0 * p if self.rotate_enabled else 0.0
                self.phone.depth = 0.0
                self.phone.state = PhoneState.ROTATING
            else:
                p = (progress - 0.6) / 0.4
                self.phone.x, self.phone.y = tx, ty
                self.phone.rotation = 90.0 if self.rotate_enabled else 0.0
                self.phone.depth = p
                self.phone.state = PhoneState.INSERTING

            if progress >= 1.0:
                self.phone.depth = 1.0
                self.phone.state = PhoneState.INSERTED
                self.phone.slot_index = self.slot.index
                self._finish()
                return

        elif self.mode == "withdraw":
            if progress < 0.4:
                p = progress / 0.4
                self.phone.x, self.phone.y = tx, ty
                self.phone.rotation = 90.0 if self.rotate_enabled else 0.0
                self.phone.depth = 1.0 - p
                self.phone.state = PhoneState.WITHDRAWING
            elif progress < 0.6:
                p = (progress - 0.4) / 0.2
                self.phone.x, self.phone.y = tx, ty
                self.phone.rotation = 90.0 * (1.0 - p) if self.rotate_enabled else 0.0
                self.phone.depth = 0.0
                self.phone.state = PhoneState.ROTATING
            else:
                p = (progress - 0.6) / 0.4
                self.phone.x = tx + (sx - tx) * p
                self.phone.y = ty + (sy - ty) * p
                self.phone.rotation = 0.0
                self.phone.depth = 0.0
                self.phone.state = PhoneState.EXITING

            if progress >= 1.0:
                self.phone.x, self.phone.y = sx, sy
                self.phone.state = PhoneState.OUTSIDE
                self.phone.slot_index = None
                self._finish()
                return

        self.state_changed.emit(self.phone.state.value)
        self.tick.emit()

    def _finish(self):
        self._timer.stop()
        self.state_changed.emit(self.phone.state.value)
        self.tick.emit()
        self.finished.emit()
