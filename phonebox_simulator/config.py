"""
Configuration objects for the PhoneBox simulator.
"""

from dataclasses import dataclass


@dataclass
class GridConfig:
    """Describes the slot grid layout for the top camera."""
    rows: int = 2
    cols: int = 3
    slot_width: int = 180
    slot_height: int = 260
    margin: int = 20
    gap: int = 15


@dataclass
class PhoneSpec:
    """Describes a single simulated phone.

    The phone is always presented in landscape orientation to the top camera
    (width > height from the camera's perspective, since the phone lies flat
    with its long axis horizontal).  The 'width' and 'height' here are the
    phone's real-world dimensions; the item renders it rotated 90 ° so that
    width runs left-right and height runs into the slot.
    """
    pid: str
    # Physical dimensions (portrait).  The top-camera item renders these
    # transposed (landscape) because the phone enters on its side.
    width: float = 70.0
    height: float = 150.0
    has_charging_port: bool = True
    has_texture: bool = True
    body_color: str = "#2b2b2b"