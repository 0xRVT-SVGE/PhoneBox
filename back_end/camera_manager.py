# ============================================================
# FILE: back_end/camera_manager.py
# ============================================================
"""
Platform-independent Camera Manager
=====================================
Maps symbolic camera roles (e.g. "bottom_cam", "top_cam") to stable
hardware identities, persists the mapping in JSON, and resolves it to
an OpenCV index at runtime.

Roles used by this project
───────────────────────────
    bottom_cam   — slot monitoring camera (bottom view, camera_async.py)
    top_cam      — top-down camera for DVW / admin sessions (top_camera.py)
    front_cam    — front-facing scanner camera (scanner_loop.py)

Each role entry in camera_config.json stores:
    index   — last-known OpenCV index (last-resort fallback)
    name    — human-readable device name
    uid     — stable hardware UID
                Windows: DeviceID from Win32_PnPEntity (WMI)
                Linux:   symlink name under /dev/v4l/by-id  OR
                         /sys/class/video4linux/videoN/name + uevent DEVPATH
    path    — (Linux only) resolved /dev/videoN path

Resolution fallback chain at runtime
──────────────────────────────────────
    1. UID match          — most stable; survives reboot and USB re-plug
    2. Name substring     — survives minor driver renames
    3. Last known index   — fragile but better than nothing
    4. Index 0            — hard fallback so the system always starts

Development mode (same physical camera for multiple roles)
────────────────────────────────────────────────────────────
Set  DEV_MODE = True  in camera_config.json, or pass  dev_mode=True  to
CameraManager().  In dev mode, one physical camera can be assigned to
multiple roles and the index is shared.  Disable for production — the
slot-monitoring bottom camera and the top-down DVW camera MUST be
distinct physical devices.

Usage
──────
    from back_end.camera_manager import CameraManager

    # At server startup:
    cam_mgr = CameraManager()
    cam_mgr.setup_if_needed()          # runs interactive UI if no config

    bottom_idx = cam_mgr.index("bottom_cam")   # → int
    top_idx    = cam_mgr.index("top_cam")
    front_idx  = cam_mgr.index("front_cam")

    # Or use the module-level singleton:
    from back_end.camera_manager import cam_mgr
    idx = cam_mgr.index("top_cam")
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2

logger = logging.getLogger(__name__)

# ── Config file location ──────────────────────────────────────────────────────
_HERE        = Path(__file__).parent
CONFIG_PATH  = _HERE/"camera_config.json"

# ── Role definitions ──────────────────────────────────────────────────────────
ALL_ROLES: Dict[str, str] = {
    "bottom_cam": "Bottom camera — slot monitoring (looking up at phone slots)",
    "top_cam":    "Top camera   — DVW / admin tracking (top-down view)",
    "front_cam":  "Front camera — student face + barcode scanner",
}

# ── Fallback defaults (used when no config exists and interactive setup is skipped)
DEFAULT_INDICES: Dict[str, int] = {
    "bottom_cam": 1,
    "top_cam":    2,
    "front_cam":  0,
}

# Maximum camera index to probe during enumeration
MAX_PROBE_INDEX = 10


# ══════════════════════════════════════════════════════════════════════════════
# DEVICE ENUMERATION  (platform-specific)
# ══════════════════════════════════════════════════════════════════════════════

def _enumerate_linux() -> List[Dict]:
    """
    Return a list of camera device dicts for Linux.

    Primary path: /dev/v4l/by-id symlinks (stable across reboots).
    Fallback path: /sys/class/video4linux/videoN — uses the kernel name
    and DEVPATH from the uevent file as a UID.

    Each dict: {index, name, uid, path}
    """
    devices: List[Dict] = []
    seen_paths: set = set()

    # ── Path A: /dev/v4l/by-id (most stable) ─────────────────────────────────
    by_id_dir = Path("/dev/v4l/by-id")
    if by_id_dir.exists():
        for symlink in sorted(by_id_dir.iterdir()):
            try:
                real = symlink.resolve()
                if not str(real).startswith("/dev/video"):
                    continue
                # Extract OpenCV index from /dev/videoN
                try:
                    idx = int(str(real).replace("/dev/video", ""))
                except ValueError:
                    continue
                if real in seen_paths:
                    continue
                seen_paths.add(real)
                devices.append({
                    "index": idx,
                    "name":  symlink.name,   # e.g. "usb-046d_HD_Pro_Webcam_C920-video-index0"
                    "uid":   symlink.name,   # symlink name is the stable UID
                    "path":  str(real),
                })
            except Exception:
                continue

    # ── Path B: /sys/class/video4linux/videoN ────────────────────────────────
    sysfs = Path("/sys/class/video4linux")
    if sysfs.exists():
        for vdir in sorted(sysfs.iterdir()):
            try:
                real = Path("/dev") / vdir.name
                if real in seen_paths:
                    continue  # already found via by-id

                idx_str = vdir.name.replace("video", "")
                if not idx_str.isdigit():
                    continue
                idx = int(idx_str)

                # Read human-readable name
                name_file = vdir / "name"
                name = name_file.read_text().strip() if name_file.exists() else vdir.name

                # Build a UID from the uevent DEVPATH (contains bus/port info)
                uid = vdir.name  # minimal fallback
                uevent = vdir / "device" / "uevent"
                if uevent.exists():
                    for line in uevent.read_text().splitlines():
                        if line.startswith("DEVPATH="):
                            uid = line.split("=", 1)[1].strip()
                            break

                seen_paths.add(real)
                devices.append({
                    "index": idx,
                    "name":  name,
                    "uid":   uid,
                    "path":  str(real),
                })
            except Exception:
                continue

    return devices


def _enumerate_windows() -> List[Dict]:
    """
    Return a list of camera device dicts for Windows.

    Uses WMI (Win32_PnPEntity) for stable DeviceID, with a winreg fallback
    when the wmi package is not installed.

    Each dict: {index, name, uid, path}
    """
    devices: List[Dict] = []

    # ── Primary: WMI ─────────────────────────────────────────────────────────
    try:
        import wmi  # type: ignore
        c = wmi.WMI()
        raw: List[Dict] = []
        for entity in c.Win32_PnPEntity():
            pnp_class = (entity.PNPClass or "").lower()
            name      = entity.Name or ""
            if pnp_class == "image" or "camera" in name.lower() or "webcam" in name.lower():
                raw.append({"name": name, "uid": entity.DeviceID or name})
        # WMI does not give us OpenCV indices directly — they must be probed.
        # We probe below and cross-match by name substring.
        return _match_wmi_to_indices(raw)
    except ImportError:
        logger.debug("[CamMgr] wmi package not available — falling back to winreg")
    except Exception as exc:
        logger.warning(f"[CamMgr] WMI enumeration failed: {exc}")

    # ── Fallback: winreg (HKLM\SYSTEM\...\Video\Capture) ────────────────────
    try:
        import winreg  # type: ignore
        key_path = r"SYSTEM\CurrentControlSet\Control\Class\{6BDD1FC6-810F-11D0-BEC7-08002BE2092F}"
        raw_reg: List[Dict] = []
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub_name) as sub:
                            try:
                                name, _ = winreg.QueryValueEx(sub, "FriendlyName")
                                driver, _ = winreg.QueryValueEx(sub, "DriverDesc")
                                raw_reg.append({
                                    "name": name,
                                    "uid":  f"reg:{sub_name}:{driver}",
                                })
                            except FileNotFoundError:
                                pass
                        i += 1
                    except OSError:
                        break
        except FileNotFoundError:
            pass
        if raw_reg:
            return _match_wmi_to_indices(raw_reg)
    except ImportError:
        pass
    except Exception as exc:
        logger.warning(f"[CamMgr] winreg enumeration failed: {exc}")

    return devices


def _match_wmi_to_indices(raw: List[Dict]) -> List[Dict]:
    """
    Windows helper: match WMI/winreg device list to OpenCV indices by probing
    with CAP_MSMF (safe — never CAP_DSHOW which crashes on bad indices).
    Cannot reliably match by name, so we return all probed cameras with UID
    from the OS list where name matches, else uid=index.
    """
    probed = _probe_opencv_indices()
    results: List[Dict] = []
    used_uids: set = set()

    for cam in probed:
        idx = cam["index"]
        # Try to find a name/uid match from OS list
        matched_uid  = f"index:{idx}"
        matched_name = f"Camera {idx}"
        for entry in raw:
            # Simple heuristic: we cannot reliably match without hardware metadata
            # so we assign OS entries in order
            if entry.get("uid") not in used_uids:
                matched_uid  = entry["uid"]
                matched_name = entry["name"]
                used_uids.add(matched_uid)
                break
        results.append({
            "index": idx,
            "name":  matched_name,
            "uid":   matched_uid,
            "path":  "",
        })
    return results


def _safe_backend() -> int:
    """
    Return the safest OpenCV backend for the current platform.

    Windows: CAP_MSMF (Windows Media Foundation)
        — NEVER use CAP_DSHOW for probing.  DSHOW does not safely return
          False for non-existent indices: it either crashes the process
          (0xC0000005 access violation) or returns isOpened()=True for
          ghost devices that yield unreadable frames.  MSMF fails cleanly.

    Linux:   CAP_V4L2 — direct kernel interface, no ghost-device issues.

    Other:   CAP_ANY  — let OpenCV choose; acceptable on macOS/BSD.
    """
    system = platform.system()
    if system == "Windows":
        return cv2.CAP_MSMF
    if system == "Linux":
        return cv2.CAP_V4L2
    return cv2.CAP_ANY


def _is_real_camera(cap: cv2.VideoCapture) -> bool:
    """
    Return True only when a VideoCapture represents a real, readable camera.

    isOpened() alone is NOT sufficient on Windows — ghost DSHOW/MSMF entries
    can report isOpened()=True while delivering no frames.

    Three independent checks must all pass:
        1. isOpened()          — basic handle validity
        2. frame width > 0     — camera reported a real resolution
        3. cap.read() succeeds — actual pixel data was delivered, frame is
                                  not None and not empty (size > 0)
    """
    if not cap.isOpened():
        return False
    if cap.get(cv2.CAP_PROP_FRAME_WIDTH) <= 0:
        return False
    ret, frame = cap.read()
    if not ret or frame is None or frame.size == 0:
        return False
    return True


def _probe_opencv_indices(max_index: int = MAX_PROBE_INDEX) -> List[Dict]:
    results: List[Dict] = []
    backend = _safe_backend()
    system  = platform.system()
    for i in range(max_index):
        cap = cv2.VideoCapture(i, backend)
        if not cap.isOpened():          # cheap check first — skip read()
            cap.release()
            continue
        if cap.get(cv2.CAP_PROP_FRAME_WIDTH) <= 0:
            cap.release()
            continue
        # Only call the expensive cap.read() if the first two checks passed
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None and frame.size > 0:
            results.append({
                "index": i,
                "name":  f"Camera {i}",
                "uid":   f"index:{i}",
                "path":  f"/dev/video{i}" if system == "Linux" else "",
            })
    return results


def enumerate_cameras() -> List[Dict]:
    """
    Enumerate all available cameras using the best method for this platform.

    Always returns at least the OpenCV-probed list so callers never get [].
    Each entry: {index, name, uid, path}
    """
    system = platform.system()
    try:
        if system == "Linux":
            devices = _enumerate_linux()
        elif system == "Windows":
            devices = _enumerate_windows()
        else:
            logger.warning(f"[CamMgr] Unknown platform '{system}' — using OpenCV probe")
            devices = []
    except Exception as exc:
        logger.warning(f"[CamMgr] OS enumeration failed ({exc}) — falling back to probe")
        devices = []

    # Always complement with OpenCV probe so we catch cameras the OS backend missed.
    # _probe_opencv_indices() uses _safe_backend() internally — no DSHOW.
    probed_indices = {d["index"] for d in devices}
    for cam in _probe_opencv_indices():
        if cam["index"] not in probed_indices:
            devices.append(cam)

    devices.sort(key=lambda d: d["index"])
    logger.info(f"[CamMgr] Enumerated {len(devices)} camera(s): "
                f"{[d['index'] for d in devices]}")
    return devices


# ══════════════════════════════════════════════════════════════════════════════
# INTERACTIVE SELECTION UI
# ══════════════════════════════════════════════════════════════════════════════

def _show_camera_previews(devices: List[Dict]) -> None:
    """Show all detected cameras simultaneously so the user can identify them."""
    caps: Dict[int, cv2.VideoCapture] = {}
    backend = _safe_backend()
    for dev in devices:
        idx = dev["index"]
        cap = cv2.VideoCapture(idx, backend)
        if cap.isOpened():
            caps[idx] = cap

    print("\n  Camera preview windows are open.")
    print("  Press any key in any window to close the previews.\n")

    while True:
        for idx, cap in list(caps.items()):
            ret, frame = cap.read()
            if ret:
                label = f"Index {idx} {devices[devices.index(next(d for d in devices if d['index']==idx))]['name']}"
                cv2.imshow(label, frame)
        key = cv2.waitKey(30) & 0xFF
        if key != 255:
            break

    for cap in caps.values():
        cap.release()
    cv2.destroyAllWindows()


def interactive_setup(
    roles:       Optional[Dict[str, str]] = None,
    dev_mode:    bool = False,
) -> Dict:
    """
    Interactive camera assignment UI.

    Shows all detected cameras simultaneously so the operator can visually
    identify each one.  For each role the operator types the numeric index.

    Args:
        roles:    {role_key: description} — defaults to ALL_ROLES
        dev_mode: If True, allows the same physical index for multiple roles
                  (useful for development with a single camera).

    Returns:
        config dict ready for CameraManager (includes 'DEV_MODE' key).
    """
    if roles is None:
        roles = ALL_ROLES

    print("\n" + "=" * 70)
    print("  CAMERA SETUP")
    print("=" * 70)

    devices = enumerate_cameras()
    if not devices:
        print("\n     No cameras detected.  Using default indices.")
        config = {role: {"index": DEFAULT_INDICES.get(role, 0),
                         "name": "Unknown", "uid": f"index:{DEFAULT_INDICES.get(role,0)}",
                         "path": ""}
                  for role in roles}
        config["DEV_MODE"] = dev_mode
        return config

    print(f"\n  Detected {len(devices)} camera(s):\n")
    for dev in devices:
        print(f"    [{dev['index']}]  {dev['name']}")
        if dev.get("uid") and not dev["uid"].startswith("index:"):
            print(f"         UID : {dev['uid'][:72]}")
        if dev.get("path"):
            print(f"         Path: {dev['path']}")
    print()

    # Offer visual preview
    try:
        resp = input("  Show live preview windows so you can identify cameras? [Y/n] ").strip().lower()
        if resp in ("", "y", "yes"):
            _show_camera_previews(devices)
    except (KeyboardInterrupt, EOFError):
        pass

    valid_indices = {d["index"] for d in devices}
    assigned_indices: Dict[str, int] = {}
    config: Dict = {}

    for role, description in roles.items():
        print(f"\n  ─── {description}")
        while True:
            try:
                raw = input(f"  Enter camera index for [{role}]: ").strip()
                if raw == "":
                    # Default
                    idx = DEFAULT_INDICES.get(role, 0)
                    print(f"  Using default: {idx}")
                else:
                    idx = int(raw)
            except (ValueError, EOFError):
                print("  Invalid — please enter a number.")
                continue

            if idx not in valid_indices:
                print(f"     Index {idx} not in detected list {sorted(valid_indices)}. "
                      f"Accept anyway? [y/N] ", end="")
                try:
                    if input().strip().lower() not in ("y", "yes"):
                        continue
                except EOFError:
                    pass

            if not dev_mode and idx in assigned_indices.values():
                taken_role = next(r for r, i in assigned_indices.items() if i == idx)
                print(f"     Index {idx} is already assigned to [{taken_role}].")
                print(f"     In production, each role must use a distinct physical camera.")
                print(f"     Use dev_mode=True to allow sharing.  Re-enter or skip [s]: ", end="")
                try:
                    resp2 = input().strip().lower()
                    if resp2 == "s":
                        idx = DEFAULT_INDICES.get(role, 0)
                    elif resp2.isdigit():
                        idx = int(resp2)
                    # else: keep idx (operator confirmed)
                except EOFError:
                    pass

            # Find device metadata
            dev = next((d for d in devices if d["index"] == idx), None)
            entry = {
                "index": idx,
                "name":  dev["name"]  if dev else f"Camera {idx}",
                "uid":   dev["uid"]   if dev else f"index:{idx}",
                "path":  dev.get("path", ""),
            }
            config[role] = entry
            assigned_indices[role] = idx
            print(f"  ✓  [{role}] to index {idx}  ({entry['name']})")
            break

    config["DEV_MODE"] = dev_mode

    print("\n" + "=" * 70)
    print("  Configuration summary:")
    for role in roles:
        e = config[role]
        print(f"    {role:15s} to index {e['index']}  ({e['name']})")
    if dev_mode:
        print("    DEV_MODE: True  (same camera may be shared across roles)")
    print("=" * 70 + "\n")

    return config


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME RESOLUTION
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_index(entry: Dict, devices: List[Dict]) -> int:
    """
    Resolve a saved config entry to a current OpenCV index.

    Fallback chain:
        1. UID exact match (stable across reboots)
        2. Name substring match (tolerates minor driver renames)
        3. last known index from config
        4. 0 (always starts)
    """
    saved_uid  = entry.get("uid", "")
    saved_name = entry.get("name", "").lower()
    saved_idx  = entry.get("index", 0)

    # 1. UID match
    if saved_uid and not saved_uid.startswith("index:"):
        for dev in devices:
            if dev.get("uid") == saved_uid:
                if dev["index"] != saved_idx:
                    logger.info(
                        f"[CamMgr] UID matched but index shifted "
                        f"{saved_idx} to {dev['index']}  ({dev['name']})"
                    )
                return dev["index"]

    # 2. Name substring match
    if saved_name:
        for dev in devices:
            if saved_name in dev.get("name", "").lower():
                logger.debug(f"[CamMgr] Name-matched '{saved_name}' , index {dev['index']}")
                return dev["index"]

    # 3. Last known index (check it actually works)
    probed = {d["index"] for d in devices}
    if saved_idx in probed:
        logger.debug(f"[CamMgr] Using last known index {saved_idx}")
        return saved_idx

    # 4. Hard fallback
    if devices:
        fallback = devices[0]["index"]
        logger.warning(
            f"[CamMgr] Could not resolve camera (uid={saved_uid!r}, "
            f"name={saved_name!r}, last={saved_idx}) — "
            f"falling back to index {fallback}"
        )
        return fallback

    logger.error("[CamMgr] No cameras available — returning 0")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# CAMERA MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class CameraManager:
    """
    Singleton-friendly camera manager.

    Typical startup sequence
    ────────────────────────
        cam_mgr = CameraManager()
        cam_mgr.setup_if_needed()       # runs interactive UI if no config file
        cam_mgr.resolve()               # resolves all roles to current indices

        idx = cam_mgr.index("top_cam")  # fast O(1) dict lookup
    """

    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        roles:       Optional[Dict[str, str]] = None,
        dev_mode:    bool = False,
        force_setup: bool = False,
    ):
        self._config_path = config_path
        self._roles       = roles or ALL_ROLES
        self._dev_mode    = dev_mode
        self._force_setup = force_setup
        self._config:    Dict = {}          # raw loaded/created config
        self._resolved:  Dict[str, int] = {}  # role → current index
        self._devices:   List[Dict] = []    # last enumerated device list

    # ── Setup ─────────────────────────────────────────────────────────────────

    def setup_if_needed(self) -> None:
        if self._force_setup or not self._config_path.exists():
            # No config — must enumerate to show setup UI
            logger.info("[CamMgr] No config found — starting interactive setup")
            self._config = interactive_setup(
                roles=self._roles,
                dev_mode=self._dev_mode,
            )
            self.save()
            self.resolve(force_enumerate=False)  # indices just came from setup
        else:
            self.load()
            if self._config.get("DEV_MODE"):
                self._dev_mode = True
            self.resolve(force_enumerate=False)  # ← fast path, no probing

    def load(self) -> None:
        """Load config from disk."""
        try:
            with open(self._config_path) as f:
                self._config = json.load(f)
            logger.info(f"[CamMgr] Config loaded from {self._config_path}")
        except Exception as exc:
            logger.warning(f"[CamMgr] Failed to load config ({exc}) — using defaults")
            self._config = self._default_config()

    def save(self) -> None:
        """Persist current config to disk."""
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_path, "w") as f:
                json.dump(self._config, f, indent=2)
            logger.info(f"[CamMgr] Config saved to {self._config_path}")
        except Exception as exc:
            logger.error(f"[CamMgr] Failed to save config: {exc}")

    # camera_manager.py

    def resolve(self, force_enumerate: bool = False) -> None:
        """
        Resolve all roles to current OpenCV indices.

        If a valid config is loaded and force_enumerate=False, uses saved
        indices directly without enumerating hardware (fast path).
        Only enumerates if UIDs need re-matching or force_enumerate=True.
        """
        # ── Fast path: config is loaded, just use saved indices ──────────────
        if not force_enumerate and self._config:
            all_have_index = all(
                role in self._config and isinstance(self._config[role], dict)
                and "index" in self._config[role]
                for role in self._roles
            )
            if all_have_index:
                for role in self._roles:
                    self._resolved[role] = self._config[role]["index"]
                logger.info(
                    "[CamMgr] Fast resolve (no enumeration): " +
                    " | ".join(f"{r}={i}" for r, i in self._resolved.items())
                )
                self._log_dev_warnings()
                return

        # ── Slow path: enumerate hardware for UID matching ───────────────────
        logger.info("[CamMgr] Enumerating cameras for UID resolution...")
        self._devices = enumerate_cameras()
        for role in self._roles:
            if role in self._config and isinstance(self._config[role], dict):
                idx = _resolve_index(self._config[role], self._devices)
            else:
                idx = DEFAULT_INDICES.get(role, 0)
            self._resolved[role] = idx

        self._log_dev_warnings()
        logger.info("[CamMgr] Resolved camera indices: " +
                    " | ".join(f"{r}→{i}" for r, i in self._resolved.items()))

    def _log_dev_warnings(self) -> None:
        """Extracted so both resolve paths log consistently."""
        if self._dev_mode:
            shared: Dict[int, List[str]] = {}
            for role, idx in self._resolved.items():
                shared.setdefault(idx, []).append(role)
            for idx, role_list in shared.items():
                if len(role_list) > 1:
                    logger.warning(
                        f"[CamMgr] DEV MODE: index {idx} shared by "
                        f"{role_list} — NOT safe for production"
                    )
        else:
            seen: Dict[int, str] = {}
            for role, idx in self._resolved.items():
                if idx in seen:
                    logger.error(
                        f"[CamMgr] PRODUCTION WARNING: index {idx} assigned "
                        f"to both [{seen[idx]}] and [{role}]. "
                        f"Run camera setup to fix this."
                    )
                seen[idx] = role

    # ── Public accessors ──────────────────────────────────────────────────────

    def index(self, role: str) -> int:
        """
        Return the current OpenCV index for a role.

        Raises KeyError if role is unknown (catches config typos early).
        """
        if role not in self._resolved:
            if role in self._roles:
                logger.warning(
                    f"[CamMgr] Role '{role}' not resolved yet — calling resolve()"
                )
                self.resolve()
            else:
                raise KeyError(
                    f"[CamMgr] Unknown camera role '{role}'. "
                    f"Valid roles: {list(self._roles)}"
                )
        return self._resolved[role]

    def all_indices(self) -> Dict[str, int]:
        """Return a snapshot of all current role → index mappings."""
        return dict(self._resolved)

    def available_cameras(self) -> List[Dict]:
        """Return the last-enumerated device list."""
        if not self._devices:
            self._devices = enumerate_cameras()
        return self._devices

    def is_dev_mode(self) -> bool:
        return self._dev_mode

    def reconfigure(self) -> None:
        """Force interactive reconfiguration and save."""
        self._config = interactive_setup(
            roles    = self._roles,
            dev_mode = self._dev_mode,
        )
        self.save()
        self.resolve()

    def update_role(self, role: str, index: int) -> None:
        """
        Programmatically re-assign a single role and persist.
        Useful for the SocketIO-based UI path (frontend camera selector).
        """
        if role not in self._roles:
            raise KeyError(f"Unknown role '{role}'")
        dev = next((d for d in self._devices if d["index"] == index), None)
        self._config[role] = {
            "index": index,
            "name":  dev["name"]      if dev else f"Camera {index}",
            "uid":   dev["uid"]       if dev else f"index:{index}",
            "path":  dev.get("path","") if dev else "",
        }
        self._resolved[role] = index
        self.save()
        logger.info(f"[CamMgr] Role '{role}' re-assigned to index {index}")

    def status_dict(self) -> Dict:
        """Return a JSON-serialisable status dict (for SocketIO or health endpoints)."""
        return {
            "dev_mode":  self._dev_mode,
            "config_path": str(self._config_path),
            "roles":     {r: {"index": self._resolved.get(r), "name": self._config.get(r, {}).get("name")}
                          for r in self._roles},
            "available": [{"index": d["index"], "name": d["name"]} for d in self._devices],
        }

    # ── Private ───────────────────────────────────────────────────────────────

    def _default_config(self) -> Dict:
        config: Dict = {}
        for role, idx in DEFAULT_INDICES.items():
            config[role] = {"index": idx, "name": f"Camera {idx}",
                            "uid": f"index:{idx}", "path": ""}
        config["DEV_MODE"] = self._dev_mode
        return config


# ══════════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON
# ══════════════════════════════════════════════════════════════════════════════

cam_mgr = CameraManager()
"""
Module-level singleton.  Import and use everywhere:

    from back_end.camera_manager import cam_mgr
    top_idx = cam_mgr.index("top_cam")
"""


# ══════════════════════════════════════════════════════════════════════════════
# STANDALONE CLI — run directly to set up or inspect camera mapping
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Camera Manager — enumerate, configure, and verify camera mappings"
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Force interactive camera assignment (overwrites existing config)",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Enable dev mode: allow one physical camera for multiple roles",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List detected cameras and current assignments, then exit",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help=f"Path to camera_config.json (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    mgr = CameraManager(
        config_path = Path(args.config),
        dev_mode    = args.dev,
        force_setup = args.setup,
    )

    if args.list:
        print("\nDetected cameras:")
        for dev in enumerate_cameras():
            print(f"  [{dev['index']}]  {dev['name']}")
            if dev.get("uid") and not dev["uid"].startswith("index:"):
                print(f"       UID : {dev['uid']}")
            if dev.get("path"):
                print(f"       Path: {dev['path']}")
        if Path(args.config).exists():
            print(f"\nConfig ({args.config}):")
            with open(args.config) as f:
                print(json.dumps(json.load(f), indent=2))
        sys.exit(0)

    mgr.setup_if_needed()

    print("\n  Final camera assignments:")
    for role, idx in mgr.all_indices().items():
        desc = ALL_ROLES.get(role, "")
        print(f"    {role:15s} to index {idx}   {desc}")
    if mgr.is_dev_mode():
        print("\n     DEV MODE active — not safe for production deployment")