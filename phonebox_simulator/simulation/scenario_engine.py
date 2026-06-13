"""
ScenarioEngine: drives deposit / withdraw animations.

Constructor signature change
────────────────────────────
Previously required callers to pass entry_pos and transit_y explicitly.
Now accepts the SlotGrid directly and reads those properties itself,
which matches how MainWindow actually calls it.

Deposit timeline (progress 0 → 1)
──────────────────────────────────
  Phase 1  ENTERING   0.00 – 0.40
    Phone slides LEFT from staging_pos (left of transit lane) to the right-
    edge entry point, then continues left until its X aligns with the target
    slot's centre X.  During this phase the phone is visible on the top
    camera and the QR code can be verified.

  Phase 2  DROPPING   0.40 – 1.00
    Phone drops STRAIGHT DOWN from transit_y into the slot centre.
    Simultaneously depth goes 0 → 1 (bottom camera picks it up).
    The optional rotate_enabled flag applies a 0 → 90 ° Z rotation during
    the drop (legacy behaviour — hides QR on insertion).

Withdraw is the exact mirror:
  Phase 1  RISING     0.00 – 0.60   depth 1 → 0, rise from slot to transit_y
  Phase 2  EXITING    0.60 – 1.00   slide RIGHT back to staging_pos
"""

from PySide6.QtCore import QObject, QTimer, Signal


def _ease_in_out(t: float) -> float:
    """Smooth-step easing (3t²−2t³)."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


class ScenarioEngine(QObject):
    tick          = Signal()
    state_changed = Signal(str)
    finished      = Signal()

    # Phase boundaries for deposit.
    _PH_ENTER_END = 0.40   # end of lateral travel
    _PH_DROP_END  = 1.00   # end of vertical drop

    def __init__(self, phone, slot, slot_grid, tick_ms: int = 16):
        """
        Parameters
        ----------
        phone      : Phone  — shared state object
        slot       : Slot   — target slot
        slot_grid  : SlotGrid — provides entry_pos, staging_pos, transit_y
        tick_ms    : int    — timer interval (default ~60 fps)
        """
        super().__init__()
        self.phone      = phone
        self.slot       = slot
        self.slot_grid  = slot_grid
        self.tick_ms    = tick_ms

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self.mode           = None
        self.duration       = 2.5
        self.rotate_enabled = False
        self._t             = 0.0

    # ------------------------------------------------------------------
    # Convenience properties so tick methods stay readable.
    # ------------------------------------------------------------------
    @property
    def _entry_pos(self):
        return self.slot_grid.entry_pos

    @property
    def _staging_pos(self):
        return self.slot_grid.staging_pos

    @property
    def _transit_y(self):
        return self.slot_grid.transit_y

    # ------------------------------------------------------------------
    def start_deposit(self, duration: float = 2.5, rotate_enabled: bool = False):
        from phonebox_simulator.models.phone import PhoneState

        self.mode           = "deposit"
        self.duration       = max(0.1, duration)
        self.rotate_enabled = rotate_enabled
        self._t             = 0.0

        # Start from the right-edge entry point in the transit lane.
        ex, _ = self._entry_pos
        self.phone.x        = ex
        self.phone.y        = self._transit_y
        self.phone.rotation = 0.0
        self.phone.depth    = 0.0
        self.phone.slot_index = self.slot.index
        self.phone.state    = PhoneState.ENTERING

        self.state_changed.emit(self.phone.state.value)
        self._timer.start(self.tick_ms)

    def start_withdraw(self, duration: float = 2.5, rotate_enabled: bool = False):
        from phonebox_simulator.models.phone import PhoneState

        self.mode           = "withdraw"
        self.duration       = max(0.1, duration)
        self.rotate_enabled = rotate_enabled
        self._t             = 0.0

        slot_cx = self.slot.rect.center().x()
        slot_cy = self.slot.rect.center().y()
        self.phone.x        = slot_cx
        self.phone.y        = slot_cy
        self.phone.rotation = 90.0 if rotate_enabled else 0.0
        self.phone.depth    = 1.0
        self.phone.state    = PhoneState.RISING

        self.state_changed.emit(self.phone.state.value)
        self._timer.start(self.tick_ms)

    def stop(self):
        self._timer.stop()

    # ------------------------------------------------------------------
    def _on_tick(self):
        self._t   += self.tick_ms / 1000.0
        progress   = min(1.0, self._t / self.duration)

        slot_cx = self.slot.rect.center().x()
        slot_cy = self.slot.rect.center().y()
        ex, _   = self._entry_pos

        if self.mode == "deposit":
            self._tick_deposit(progress, slot_cx, slot_cy, ex)
        elif self.mode == "withdraw":
            self._tick_withdraw(progress, slot_cx, slot_cy, ex)

        self.state_changed.emit(self.phone.state.value)
        self.tick.emit()

        if progress >= 1.0:
            self._finish()

    # ── deposit ────────────────────────────────────────────────────────
    def _tick_deposit(self, p, slot_cx, slot_cy, entry_x):
        from phonebox_simulator.models.phone import PhoneState

        if p <= self._PH_ENTER_END:
            # Phase 1: slide left in transit lane from entry_x → slot_cx.
            t = _ease_in_out(p / self._PH_ENTER_END)
            self.phone.x        = entry_x + (slot_cx - entry_x) * t
            self.phone.y        = self._transit_y
            self.phone.rotation = 0.0
            self.phone.depth    = 0.0
            self.phone.state    = PhoneState.ENTERING
        else:
            # Phase 2: drop straight down.
            t = _ease_in_out((p - self._PH_ENTER_END) / (1.0 - self._PH_ENTER_END))
            self.phone.x    = slot_cx
            self.phone.y    = self._transit_y + (slot_cy - self._transit_y) * t
            self.phone.depth = t
            self.phone.state = PhoneState.DROPPING
            self.phone.rotation = 90.0 * t if self.rotate_enabled else 0.0

        if p >= 1.0:
            self.phone.x, self.phone.y = slot_cx, slot_cy
            self.phone.depth    = 1.0
            self.phone.state    = PhoneState.INSERTED
            self.phone.rotation = 90.0 if self.rotate_enabled else 0.0

    # ── withdraw ───────────────────────────────────────────────────────
    def _tick_withdraw(self, p, slot_cx, slot_cy, entry_x):
        from phonebox_simulator.models.phone import PhoneState

        _PH_RISE_END = 0.60
        sx, _ = self._staging_pos   # withdraw returns to staging, not entry_x

        if p <= _PH_RISE_END:
            # Phase 1: rise straight up.
            t = _ease_in_out(p / _PH_RISE_END)
            self.phone.x    = slot_cx
            self.phone.y    = slot_cy + (self._transit_y - slot_cy) * t
            self.phone.depth = 1.0 - t
            self.phone.state = PhoneState.RISING
            self.phone.rotation = 90.0 * (1.0 - t) if self.rotate_enabled else 0.0
        else:
            # Phase 2: slide left back to staging position.
            t = _ease_in_out((p - _PH_RISE_END) / (1.0 - _PH_RISE_END))
            self.phone.x    = slot_cx + (sx - slot_cx) * t
            self.phone.y    = self._transit_y
            self.phone.depth = 0.0
            self.phone.rotation = 0.0
            self.phone.state = PhoneState.EXITING

        if p >= 1.0:
            self.phone.x, self.phone.y = sx, self._transit_y
            self.phone.depth    = 0.0
            self.phone.state    = PhoneState.OUTSIDE
            self.phone.slot_index = None
            self.phone.rotation  = 0.0

    # ------------------------------------------------------------------
    def _finish(self):
        self._timer.stop()
        self.tick.emit()
        self.finished.emit()
