#!/usr/bin/env python3
"""
PhoneBox — Alarm Threshold Calibrator
======================================
Interactive tool for visualising and tuning alarm-related thresholds.

Usage
-----
Standalone (config editing + static visualisation only):
    python -m back_end.slot_monitor.tools.threshold_calibrator

With live server connection (adds real-time alarm event markers):
    python -m back_end.slot_monitor.tools.threshold_calibrator --url http://localhost:5000

Options
-------
  --url URL     Base URL of the running PhoneBox server.
                Default: http://localhost:5000
  --history N   Number of distance samples to keep in the rolling chart.
                Default: 120  (~60 s at a 2 Hz server emit rate)
  --no-live     Disable SocketIO connection even if --url is given.

Keyboard shortcuts (when the chart window is focused)
------------------------------------------------------
  s             Save current slider values to back_end/config.py
  r             Reset all sliders to the values read from config.py at startup
  q / Escape    Quit

What the sliders control
-------------------------
  MISMATCH_THRESHOLD   Distance above which a slot is flagged as changed.
  RECALC_THRESHOLD     Distance drift that triggers a soft baseline recalibration.
  GRACE_PERIOD         Seconds a slot must stay above threshold before alarm fires.
  SLOT_CHANGE_THRESH   Min distance between before/after embeddings to confirm
                       that a slot physically changed (alarm verification).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import threading
from collections import deque
from pathlib import Path

# ── Repo root resolution ─────────────────────────────────────────────────────
_HERE      = Path(__file__).resolve().parent            # tools/
_REPO_ROOT = _HERE.parent.parent.parent                 # project root

# ── Config path ───────────────────────────────────────────────────────────────
_CONFIG_PY = _REPO_ROOT / "back_end" / "config.py"

# ── Configurable chart constants ──────────────────────────────────────────────
# Change these to adjust the look and feel without touching main logic.

#: Seconds covered by the x-axis of the rolling distance chart.
CHART_WINDOW_SECONDS: int = 60

#: Maximum y-axis value shown on the distance chart.
CHART_Y_MAX: float = 0.40

#: Colours used for each slot's distance line (cycles when > len(SLOT_COLOURS)).
SLOT_COLOURS: list[str] = [
    "#4fc3f7",  # blue
    "#81c784",  # green
    "#ffb74d",  # amber
    "#e57373",  # red
    "#ba68c8",  # purple
    "#4dd0e1",  # cyan
    "#f06292",  # pink
    "#a1887f",  # brown
]

#: Alpha for the alarm-zone shading behind MISMATCH_THRESHOLD.
ZONE_ALPHA: float = 0.07

# ─────────────────────────────────────────────────────────────────────────────

def _read_config() -> dict:
    """Import the current threshold values from back_end/config.py."""
    sys.path.insert(0, str(_REPO_ROOT))
    try:
        # Force re-import in case the module was already cached.
        import importlib, back_end.config as _cfg
        importlib.reload(_cfg)
        from back_end.config import SlotMonitorConfig as SM, AlarmConfig as AL
        return {
            "MISMATCH_THRESHOLD":    SM.MISMATCH_THRESHOLD,
            "RECALC_THRESHOLD":      SM.RECALC_THRESHOLD,
            "GRACE_PERIOD":          SM.GRACE_PERIOD,
            "SLOT_CHANGE_THRESHOLD": AL.SLOT_CHANGE_THRESHOLD,
        }
    except Exception as exc:
        print(f"[calibrator] WARNING: could not import config ({exc}); using defaults.")
        return {
            "MISMATCH_THRESHOLD":    0.15,
            "RECALC_THRESHOLD":      0.05,
            "GRACE_PERIOD":          3.0,
            "SLOT_CHANGE_THRESHOLD": 0.07,
        }


def _write_config(values: dict) -> None:
    """
    Write updated threshold values back into back_end/config.py using
    regex substitution so comments and formatting are preserved.
    """
    if not _CONFIG_PY.exists():
        print(f"[calibrator] ERROR: {_CONFIG_PY} not found — cannot save.")
        return

    text = _CONFIG_PY.read_text(encoding="utf-8")

    # Float thresholds (4 decimal places is enough precision).
    for key in ("MISMATCH_THRESHOLD", "RECALC_THRESHOLD", "SLOT_CHANGE_THRESHOLD"):
        if key not in values:
            continue
        pattern     = rf"(\b{re.escape(key)}\s*=\s*)\d+\.?\d*"
        replacement = rf"\g<1>{values[key]:.4f}"
        text        = re.sub(pattern, replacement, text)

    # GRACE_PERIOD — 1 decimal place.
    if "GRACE_PERIOD" in values:
        pattern     = r"(\bGRACE_PERIOD\s*=\s*)\d+\.?\d*"
        replacement = rf"\g<1>{values['GRACE_PERIOD']:.1f}"
        text        = re.sub(pattern, replacement, text)

    _CONFIG_PY.write_text(text, encoding="utf-8")
    print(f"[calibrator] Saved → {_CONFIG_PY}")


# ─────────────────────────────────────────────────────────────────────────────
# Live data receiver (SocketIO — optional)
# ─────────────────────────────────────────────────────────────────────────────

class _LiveReceiver:
    """
    Connects to the running server via python-socketio and collects live
    alarm events.  Falls back gracefully if python-socketio is not installed.
    """

    def __init__(self, url: str, history: deque):
        self._url     = url
        self._history = history   # shared with the chart — append (t, slot, dist)
        self._sio     = None
        self._thread  = None
        self._events: list[tuple[float, str]] = []   # (timestamp, label)
        self._lock    = threading.Lock()

    @property
    def events(self) -> list:
        with self._lock:
            return list(self._events)

    def start(self) -> bool:
        try:
            import socketio as _sio_pkg  # python-socketio[client]
        except ImportError:
            print(
                "[calibrator] python-socketio not installed — live data disabled.\n"
                "             pip install 'python-socketio[client]' to enable."
            )
            return False

        self._sio = _sio_pkg.Client(reconnection=True, logger=False)

        @self._sio.on("alarm_triggered")
        def _on_alarm(data):
            now = time.time()
            with self._lock:
                self._events.append((now, "⚠ alarm"))

        @self._sio.on("alarm_cleared")
        def _on_cleared(data):
            now = time.time()
            with self._lock:
                self._events.append((now, "✓ cleared"))

        @self._sio.on("alarm_updated")
        def _on_updated(data):
            # alarm_updated carries a 'mismatches' list; each entry has
            # distance info we can record for the chart.
            now = time.time()
            mismatches = data.get("mismatches", []) if isinstance(data, dict) else []
            for m in mismatches:
                slot = m.get("lid", "?")
                dist = m.get("distance")
                if dist is not None:
                    self._history.append((now, slot, float(dist)))

        # Connect in a background thread so the GUI stays responsive.
        def _run():
            try:
                self._sio.connect(self._url, transports=["websocket"])
                self._sio.wait()
            except Exception as exc:
                print(f"[calibrator] SocketIO error: {exc}")

        self._thread = threading.Thread(target=_run, daemon=True, name="calibrator-sio")
        self._thread.start()
        print(f"[calibrator] Connecting to {self._url} …")
        return True

    def stop(self):
        if self._sio:
            try:
                self._sio.disconnect()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Matplotlib GUI
# ─────────────────────────────────────────────────────────────────────────────

def _run_gui(initial: dict, history: deque, receiver: "_LiveReceiver | None"):
    import matplotlib
    matplotlib.use("TkAgg")          # works headless with Tcl/Tk; change to Qt5Agg if preferred
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from matplotlib.widgets import Slider, Button

    # ── Figure layout ────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(13, 8), facecolor="#1a1a2e")
    fig.canvas.manager.set_window_title("PhoneBox — Alarm Threshold Calibrator")

    # Axes: chart (top), sliders (bottom row)
    ax_chart  = fig.add_axes([0.07, 0.38, 0.88, 0.55], facecolor="#0f0f1a")
    ax_mis    = fig.add_axes([0.10, 0.25, 0.55, 0.03], facecolor="#1e1e3a")
    ax_rec    = fig.add_axes([0.10, 0.20, 0.55, 0.03], facecolor="#1e1e3a")
    ax_grace  = fig.add_axes([0.10, 0.15, 0.55, 0.03], facecolor="#1e1e3a")
    ax_slot   = fig.add_axes([0.10, 0.10, 0.55, 0.03], facecolor="#1e1e3a")
    ax_save   = fig.add_axes([0.76, 0.17, 0.11, 0.05])
    ax_reset  = fig.add_axes([0.76, 0.10, 0.11, 0.05])

    # ── Slider colour helper ──────────────────────────────────────────────────
    _sc = dict(color="#4fc3f7", track_color="#2e2e4a")

    s_mis   = Slider(ax_mis,   "MISMATCH_THRESHOLD   ",
                     0.01, 0.40, valinit=initial["MISMATCH_THRESHOLD"],    **_sc)
    s_rec   = Slider(ax_rec,   "RECALC_THRESHOLD      ",
                     0.01, 0.20, valinit=initial["RECALC_THRESHOLD"],      **_sc)
    s_grace = Slider(ax_grace, "GRACE_PERIOD (s)      ",
                     0.5,  15.0, valinit=initial["GRACE_PERIOD"],          **_sc)
    s_slot  = Slider(ax_slot,  "SLOT_CHANGE_THRESHOLD ",
                     0.01, 0.30, valinit=initial["SLOT_CHANGE_THRESHOLD"], **_sc)

    for s in (s_mis, s_rec, s_grace, s_slot):
        s.label.set_color("#c8c8e8")
        s.valtext.set_color("#ffcc80")

    btn_save  = Button(ax_save,  "Save to config",
                       color="#1b5e20", hovercolor="#2e7d32")
    btn_reset = Button(ax_reset, "Reset",
                       color="#311b92", hovercolor="#4527a0")
    for b in (btn_save, btn_reset):
        b.label.set_color("white")
        b.label.set_fontsize(9)

    # ── Chart state ───────────────────────────────────────────────────────────
    slot_lines: dict[str, object] = {}   # slot_key → Line2D
    slot_xs:    dict[str, list]   = {}
    slot_ys:    dict[str, list]   = {}

    # Persistent threshold lines
    line_mis  = ax_chart.axhline(s_mis.val,   color="#ef5350", lw=1.5, ls="--", label="MISMATCH")
    line_rec  = ax_chart.axhline(s_rec.val,   color="#ffb300", lw=1.0, ls=":",  label="RECALC")
    line_slot = ax_chart.axhline(s_slot.val,  color="#ab47bc", lw=1.0, ls="-.", label="SLOT_CHANGE")

    # Alarm zone shading
    zone_fill = ax_chart.axhspan(s_mis.val, CHART_Y_MAX,
                                 color="#ef5350", alpha=ZONE_ALPHA)
    recalc_fill = ax_chart.axhspan(s_rec.val, s_mis.val,
                                   color="#ffb300", alpha=ZONE_ALPHA / 2)

    ax_chart.set_ylim(0, CHART_Y_MAX)
    ax_chart.set_xlim(-CHART_WINDOW_SECONDS, 0)
    ax_chart.set_xlabel("seconds ago", color="#9090c0", fontsize=9)
    ax_chart.set_ylabel("embedding distance", color="#9090c0", fontsize=9)
    ax_chart.set_title("Live slot embedding distances   (alarm events marked with ▼)",
                        color="#c8c8e8", fontsize=10)
    ax_chart.tick_params(colors="#6060a0")
    ax_chart.spines[:]
    for sp in ax_chart.spines.values():
        sp.set_color("#2e2e4e")
    ax_chart.legend(loc="upper left", fontsize=8,
                    facecolor="#1a1a2e", labelcolor="#d0d0f0",
                    edgecolor="#3a3a5a")

    # Event annotation list (redrawn each tick)
    _event_artists = []

    status_text = fig.text(
        0.07, 0.03,
        f"Config: {_CONFIG_PY}  |  server: {'connected' if receiver else 'offline'}",
        color="#606080", fontsize=8, family="monospace",
    )

    # ── Slider update callbacks ───────────────────────────────────────────────
    def _refresh_lines(_=None):
        mv = s_mis.val
        rv = s_rec.val
        gv = s_grace.val
        sv = s_slot.val

        line_mis.set_ydata([mv, mv])
        line_rec.set_ydata([rv, rv])
        line_slot.set_ydata([sv, sv])

        # Redraw zone shading (remove + re-add because set_xy is awkward)
        zone_fill.set_bounds(0, mv, CHART_WINDOW_SECONDS, CHART_Y_MAX - mv)
        recalc_fill.set_bounds(0, rv, CHART_WINDOW_SECONDS, mv - rv)

        fig.canvas.draw_idle()

    s_mis.on_changed(_refresh_lines)
    s_rec.on_changed(_refresh_lines)
    s_grace.on_changed(_refresh_lines)
    s_slot.on_changed(_refresh_lines)

    # ── Save / Reset ──────────────────────────────────────────────────────────
    def _save(_=None):
        vals = {
            "MISMATCH_THRESHOLD":    round(s_mis.val,   4),
            "RECALC_THRESHOLD":      round(s_rec.val,   4),
            "GRACE_PERIOD":          round(s_grace.val, 1),
            "SLOT_CHANGE_THRESHOLD": round(s_slot.val,  4),
        }
        _write_config(vals)
        status_text.set_text(
            f"Saved at {time.strftime('%H:%M:%S')}  |  {_CONFIG_PY.name}"
        )
        fig.canvas.draw_idle()

    def _reset(_=None):
        fresh = _read_config()
        s_mis.set_val(fresh["MISMATCH_THRESHOLD"])
        s_rec.set_val(fresh["RECALC_THRESHOLD"])
        s_grace.set_val(fresh["GRACE_PERIOD"])
        s_slot.set_val(fresh["SLOT_CHANGE_THRESHOLD"])
        status_text.set_text("Reset to values from config.py")
        fig.canvas.draw_idle()

    btn_save.on_clicked(_save)
    btn_reset.on_clicked(_reset)

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    def _on_key(event):
        if event.key in ("s", "S"):
            _save()
        elif event.key in ("r", "R"):
            _reset()
        elif event.key in ("q", "Q", "escape"):
            plt.close("all")

    fig.canvas.mpl_connect("key_press_event", _on_key)

    # ── Annotation for chart tip labels ──────────────────────────────────────
    def _label_line(ax, line, txt, color):
        """Place a small label at the right edge of a threshold line."""
        y = line.get_ydata()[0]
        ax.text(0, y, f" {txt}", color=color, fontsize=7,
                va="bottom", ha="left", transform=ax.transAxes)

    # ── Animation timer ───────────────────────────────────────────────────────
    _colour_idx: dict[str, int] = {}

    def _tick(_=None):
        nonlocal _event_artists
        now = time.time()

        # Remove stale event markers
        for art in _event_artists:
            try:
                art.remove()
            except Exception:
                pass
        _event_artists = []

        # Build per-slot time series from shared history deque
        new_xs: dict[str, list] = {}
        new_ys: dict[str, list] = {}
        for (t, slot, dist) in list(history):
            age = now - t
            if age > CHART_WINDOW_SECONDS:
                continue
            key = str(slot)
            new_xs.setdefault(key, []).append(-age)
            new_ys.setdefault(key, []).append(dist)

        for key, xs in new_xs.items():
            ys = new_ys[key]
            if key not in slot_lines:
                cidx = len(_colour_idx)
                _colour_idx[key] = cidx
                col = SLOT_COLOURS[cidx % len(SLOT_COLOURS)]
                (line,) = ax_chart.plot([], [], color=col, lw=1.4,
                                        label=f"slot {key}", alpha=0.9)
                slot_lines[key] = line
                ax_chart.legend(loc="upper left", fontsize=8,
                                facecolor="#1a1a2e", labelcolor="#d0d0f0",
                                edgecolor="#3a3a5a")
            slot_lines[key].set_data(sorted(zip(xs, ys)))

        # Draw alarm event markers
        if receiver:
            for (t, label) in receiver.events:
                age = now - t
                if age > CHART_WINDOW_SECONDS:
                    continue
                vl = ax_chart.axvline(-age, color="#ef5350", lw=0.8,
                                      ls="--", alpha=0.6)
                ann = ax_chart.annotate(
                    label, xy=(-age, CHART_Y_MAX * 0.9),
                    fontsize=7, color="#ef9a9a", rotation=90,
                    va="top", ha="right",
                )
                _event_artists.extend([vl, ann])

        fig.canvas.draw_idle()

    _timer = fig.canvas.new_timer(interval=500)   # refresh every 500 ms
    _timer.add_callback(_tick)
    _timer.start()

    # ── Figure labels ─────────────────────────────────────────────────────────
    fig.text(0.07, 0.33, "Thresholds (drag sliders or type  s = save  r = reset  q = quit)",
             color="#8080a0", fontsize=8)
    fig.text(
        0.76, 0.235,
        "MISMATCH  →  alarm fires\n"
        "RECALC    →  soft recalibrate\n"
        "GRACE     →  seconds to confirm\n"
        "SLOT CHG  →  verify real change",
        color="#707090", fontsize=7.5, va="top",
    )

    plt.show()

    if receiver:
        receiver.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PhoneBox alarm threshold calibrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--url", default="http://localhost:5000",
        help="Running server URL for live alarm events (default: %(default)s)",
    )
    parser.add_argument(
        "--history", type=int, default=120,
        help="Rolling distance history size — samples kept in memory (default: %(default)s)",
    )
    parser.add_argument(
        "--no-live", action="store_true",
        help="Disable SocketIO live connection",
    )
    args = parser.parse_args()

    initial = _read_config()
    print("[calibrator] Loaded thresholds from config:")
    for k, v in initial.items():
        print(f"  {k} = {v}")

    history: deque = deque(maxlen=args.history)

    receiver = None
    if not args.no_live:
        receiver = _LiveReceiver(args.url, history)
        receiver.start()

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print(
            "[calibrator] matplotlib not installed — cannot show GUI.\n"
            "             pip install matplotlib"
        )
        sys.exit(1)

    _run_gui(initial, history, receiver)


if __name__ == "__main__":
    main()
