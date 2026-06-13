"""
ScenarioEngine: drives deposit / withdraw animations.

Deposit timeline (progress 0 → 1)
──────────────────────────────────
  Phase 1  ENTERING   0.00 – 0.40
    Phone slides LEFT from the right-edge entry point, staying in the
    transit lane (constant Y = transit_y), until its X centre aligns with
    the target slot's centre X.

  Phase 2  HOVERING   (instantaneous transition, no extra time)
    The phone is now directly above the target slot, still in the transit
    lane.

  Phase 3  DROPPING   0.40 – 1.00
    Phone drops STRAIGHT DOWN from transit_y into the slot centre.
    Simultaneously depth goes 0 → 1 (bottom camera picks it up).
    No Z rotation occurs in the default mode — the phone is already
    back-face-up.  The optional legacy rotate_enabled flag still applies a
    0 → 90 ° Z rotation during the drop for compatibility.

Withdraw is the exact mirror:
  Phase 1  RISING     0.00 – 0.60   depth 1 → 0, rise from slot to transit_y
  Phase 2  EXITING    0.60 – 1.00   slide RIGHT back off-screen
"""

from PySide6.QtCore import QObject, QTimer, Signal

from phonebox_simulator.models.slot_grid import TRANSIT_HEIGHT


def _ease_in_out(t: float) -> float:
    """Smooth-step easing."""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


class ScenarioEngine(QObject):
    tick          = Signal()
    state_changed = Signal(str)
    finished      = Signal()

    # Phase boundaries for deposit.
    _PH_ENTER_END = 0.40   # end of lateral travel
    _PH_DROP_END  = 1.00   # end of vertical drop

    def __init__(self, phone, slot, entry_pos, transit_y, tick_ms=16):
        super().__init__()
        self.phone      = phone
        self.slot       = slot
        self.entry_pos  = entry_pos    # (x, y) — right-edge entry point
        self.transit_y  = transit_y   # Y of transit lane centre
        self.tick_ms    = tick_ms

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self.mode           = None
        self.duration       = 2.5
        self.rotate_enabled = False   # default OFF — not needed for back-face-up
        self._t             = 0.0

    # ------------------------------------------------------------------
    def start_deposit(self, duration: float = 2.5, rotate_enabled: bool = False):
        from phonebox_simulator.models.phone import PhoneState

        self.mode           = "deposit"
        self.duration       = max(0.1, duration)
        self.rotate_enabled = rotate_enabled
        self._t             = 0.0

        # Start at right-edge entry point, in transit lane.
        self.phone.x, self.phone.y = self.entry_pos
        self.phone.y               = self.transit_y
        self.phone.rotation        = 0.0
        self.phone.depth           = 0.0
        self.phone.slot_index      = self.slot.index
        self.phone.state           = PhoneState.ENTERING

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
        from phonebox_simulator.models.phone import PhoneState

        self._t   += self.tick_ms / 1000.0
        progress   = min(1.0, self._t / self.duration)

        slot_cx = self.slot.rect.center().x()
        slot_cy = self.slot.rect.center().y()
        ex, _   = self.entry_pos

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
            # Phase 1: slide left in transit lane.
            t = _ease_in_out(p / self._PH_ENTER_END)
            self.phone.x   = entry_x + (slot_cx - entry_x) * t
            self.phone.y   = self.transit_y
            self.phone.rotation = 0.0
            self.phone.depth    = 0.0
            self.phone.state    = PhoneState.ENTERING
        else:
            # Phase 2: drop straight down.
            t = _ease_in_out((p - self._PH_ENTER_END) / (1.0 - self._PH_ENTER_END))
            self.phone.x   = slot_cx
            self.phone.y   = self.transit_y + (slot_cy - self.transit_y) * t
            self.phone.depth    = t
            self.phone.state    = PhoneState.DROPPING
            # Optional legacy Z-rotation during drop.
            self.phone.rotation = 90.0 * t if self.rotate_enabled else 0.0

        if p >= 1.0:
            self.phone.x, self.phone.y = slot_cx, slot_cy
            self.phone.depth  = 1.0
            self.phone.state  = PhoneState.INSERTED
            self.phone.rotation = 90.0 if self.rotate_enabled else 0.0

    # ── withdraw ───────────────────────────────────────────────────────
    def _tick_withdraw(self, p, slot_cx, slot_cy, entry_x):
        from phonebox_simulator.models.phone import PhoneState

        _PH_RISE_END = 0.60

        if p <= _PH_RISE_END:
            # Phase 1: rise straight up.
            t = _ease_in_out(p / _PH_RISE_END)
            self.phone.x   = slot_cx
            self.phone.y   = slot_cy + (self.transit_y - slot_cy) * t
            self.phone.depth    = 1.0 - t
            self.phone.state    = PhoneState.RISING
            self.phone.rotation = 90.0 * (1.0 - t) if self.rotate_enabled else 0.0
        else:
            # Phase 2: slide right back off-screen.
            t = _ease_in_out((p - _PH_RISE_END) / (1.0 - _PH_RISE_END))
            self.phone.x   = slot_cx + (entry_x - slot_cx) * t
            self.phone.y   = self.transit_y
            self.phone.depth    = 0.0
            self.phone.rotation = 0.0
            self.phone.state    = PhoneState.EXITING

        if p >= 1.0:
            self.phone.x, self.phone.y = entry_x, self.transit_y
            self.phone.depth  = 0.0
            self.phone.state  = PhoneState.OUTSIDE
            self.phone.slot_index = None
            self.phone.rotation   = 0.0

    # ------------------------------------------------------------------
    def _finish(self):
        self._timer.stop()
        self.tick.emit()
        self.finished.emit()