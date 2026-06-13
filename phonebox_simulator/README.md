# PhoneBox Digital Twin Simulator

A PySide6 desktop app that simulates the deposit/withdrawal pipeline of the
Secure Automated Phone Storage System, so the vision pipeline can be
developed and stress-tested without physical cameras or hardware.

## Running

```bash
pip install -r requirements.txt
cd <directory containing phonebox_simulator/>
python -m phonebox_simulator.main
```

## Layout

```
phonebox_simulator/
├── main.py                     entry point
├── config.py                   GridConfig, PhoneSpec dataclasses
├── models/
│   ├── phone.py                Phone state machine + QR generation
│   └── slot_grid.py            Slot grid + staging-area geometry
├── graphics/
│   ├── phone_item.py           top-view phone (QGraphicsItem)
│   ├── top_camera_view.py      top/QR camera (slot grid + faults)
│   ├── bottom_camera_view.py   bottom/slot camera (phone underside + faults)
│   └── fault_overlay.py        light-leak gradient overlay
├── simulation/
│   └── scenario_engine.py      deposit/withdraw animation state machine
└── ui/
    ├── control_panel.py         all controls (grid, phone, ops, faults)
    └── main_window.py            wires everything together
```

## Workflow

1. **Configure the grid** (rows/cols/slot size) and click "Apply / Rebuild".
2. **Create a phone**: set a PID, toggle charging port / texture / color,
   click "Create phone". It appears in the staging area above the grid
   in the top camera view, showing its generated QR code.
3. **Select a target slot**, optionally toggle "Rotate phone on insertion"
   (this is what hides the QR code as the phone goes in, matching the
   real ENTERING/EXITING + rotation behavior).
4. **Deposit**: the phone animates from the staging area into the slot.
   - 0-40%: slides toward the slot (QR visible to the top camera).
   - 40-60%: rotates 90 degrees (QR disappears if rotation is enabled).
   - 60-100%: "drops" into the slot — `depth` goes 0 -> 1, and the
     bottom camera view starts showing the phone's underside (charging
     port / speaker holes / texture) sliding into frame.
5. **Withdraw**: reverses the animation — the bottom camera view loses
   the phone, it rotates back, the QR reappears, and it returns to the
   staging area.
6. **Fault injection**: independent sliders for the top and bottom
   cameras —
   - **Light leak**: a soft gradient overlay from above.
   - **Shake**: jittered camera transform (simulates handheld/vibration).
   - **Blur**: Gaussian blur via `QGraphicsBlurEffect`.
   - **Zoom**: camera zoom in/out.

## Notes / extension points

- `Phone` is the single shared object both camera views read from — the
  top view hides the phone item once `depth >= 1.0`, and the bottom view
  only draws once `depth > 0`.
- To simulate a "removed then returned" theft-style scenario, run a
  Withdraw and then immediately a Deposit on the same slot.
- To simulate a foreign object (paper, wallet, hand) instead of a phone,
  create a `PhoneSpec` with `has_charging_port=False`, `has_texture=False`
  and a PID that won't resolve to anything meaningful in your QR
  detector — the bottom view will still render a generic body shape.
- The `ScenarioEngine` emits `tick`, `state_changed`, and `finished`
  signals — useful hooks if you later want to record frames to disk or
  feed a virtual camera device.
