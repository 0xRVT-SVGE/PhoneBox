#!/usr/bin/env python3
# ============================================================
# FILE: back_end/slot_monitor/tools/download_tracker_models.py
# ============================================================
"""
One-time download of TrackerNano ONNX models from opencv_zoo.

Run ONCE on a machine with internet access, then copy the
back_end/models/ directory to the intranet server.

Models downloaded (~1 MB total):
  nanotrack_backbone_sim.onnx  (~800 KB)
  nanotrack_head_sim.onnx      (~200 KB)

Source: https://github.com/opencv/opencv_zoo (Apache 2.0 license)

Usage:
    python back_end/slot_monitor/tools/download_tracker_models.py
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

# ── Output directory ──────────────────────────────────────
MODELS_DIR = Path(__file__).parent.parent.parent / "models"

# ── Model URLs and expected SHA-256 checksums ─────────────
# From opencv_zoo commit pinned for reproducibility.
BASE = (
    "https://raw.githubusercontent.com/opencv/opencv_zoo/"
    "9bba7beb5e97dad3d6bfb428bf8dc1bc73d0b73c/"
    "models/nanotrack"
)

MODELS = [
    {
        "url":      f"{BASE}/nanotrack_backbone_sim.onnx",
        "filename": "nanotrack_backbone_sim.onnx",
        "sha256":   None,   # set to None to skip check; fill in after first download
    },
    {
        "url":      f"{BASE}/nanotrack_head_sim.onnx",
        "filename": "nanotrack_head_sim.onnx",
        "sha256":   None,
    },
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_models() -> bool:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    all_ok = True

    for m in MODELS:
        dest = MODELS_DIR / m["filename"]

        if dest.exists():
            print(f"  SKIP  {m['filename']}  (already exists at {dest})")
            continue

        print(f"  GET   {m['filename']}  …", end="", flush=True)
        try:
            urllib.request.urlretrieve(m["url"], dest)
            size_kb = dest.stat().st_size // 1024
            print(f"  {size_kb} KB")

            if m["sha256"]:
                actual = _sha256(dest)
                if actual != m["sha256"]:
                    print(f"  WARN  Checksum mismatch for {m['filename']}")
                    print(f"        expected: {m['sha256']}")
                    print(f"        got:      {actual}")
                    all_ok = False
                else:
                    print(f"  OK    Checksum verified")
        except Exception as e:
            print(f"\n  ERROR downloading {m['filename']}: {e}")
            all_ok = False

    return all_ok


def verify_models() -> bool:
    """Check both model files exist and are non-empty."""
    ok = True
    for m in MODELS:
        dest = MODELS_DIR / m["filename"]
        if not dest.exists() or dest.stat().st_size < 10_000:
            print(f"  MISSING  {dest}")
            ok = False
        else:
            print(f"  OK       {dest}  ({dest.stat().st_size // 1024} KB)")
    return ok


def verify_opencv_nano() -> bool:
    """Check that the installed OpenCV build has TrackerNano."""
    try:
        import cv2
        has_nano = (
            hasattr(cv2, "TrackerNano") or
            hasattr(cv2, "TrackerNano_create") or
            (hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerNano"))
        )
        ver = cv2.__version__
        if has_nano:
            print(f"  OK  cv2.TrackerNano available  (OpenCV {ver})")
        else:
            print(f"  WARN  cv2.TrackerNano NOT found in OpenCV {ver}")
            print("        Install opencv-contrib-python >= 4.7:")
            print("          pip install opencv-contrib-python")
            print("        Server will fall back to CSRT automatically.")
        return has_nano
    except ImportError:
        print("  ERROR  cv2 not importable")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("TrackerNano model downloader")
    print("=" * 60)
    print(f"\nTarget directory: {MODELS_DIR}\n")

    print("Checking OpenCV build …")
    has_nano = verify_opencv_nano()
    print()

    print("Downloading models …")
    ok = download_models()
    print()

    print("Verifying …")
    ok = verify_models() and ok
    print()

    if ok:
        print("All models ready.")
        if not has_nano:
            print("NOTE: Install opencv-contrib-python to use TrackerNano.")
            print("      Server will use CSRT until then.")
    else:
        print("Some models failed — check errors above.")
        sys.exit(1)