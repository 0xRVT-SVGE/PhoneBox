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
    """Describes a single simulated phone."""
    pid: str
    width: float = 70.0
    height: float = 150.0
    has_charging_port: bool = True
    has_texture: bool = True
    body_color: str = "#2b2b2b"
