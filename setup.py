"""
PhoneBox — Cython build script
==============================
Compiles the hot-path .pyx modules into native C extensions.

Usage
─────
  # One-time build (run from repo root):
  python setup.py build_ext --inplace

  # Clean rebuild:
  python setup.py clean --all && python setup.py build_ext --inplace

  # Install for the project (alternative):
  pip install -e . --no-build-isolation

What gets compiled
──────────────────
  back_end/slot_monitor/slot_embed_cy.pyx   →  slot_embed_cy.so/.pyd
  back_end/slot_monitor/slots_cy.pyx        →  slots_cy.so/.pyd

Each .py original is kept untouched.  The compiled module is imported
with a fallback to the pure-Python version if the .so is missing or
the build was not run (see each module's __init__ for the fallback).

Requirements
────────────
  pip install cython numpy
  Linux:   gcc (standard)
  Windows: MSVC (Visual Studio Build Tools) or MinGW-w64
"""

from setuptools import setup, Extension
import numpy as np

try:
    from Cython.Build import cythonize
    from Cython.Compiler import Options as _CyOptions
    # Emit C-level line directives so tracebacks still point to .pyx lines
    _CyOptions.emit_code_comments = True
    HAS_CYTHON = True
except ImportError:
    HAS_CYTHON = False
    print("[setup.py] Cython not found — run: pip install cython")

# ── Common compiler flags ─────────────────────────────────────────────────────
import sys
import platform

_COMPILE_ARGS = []
_LINK_ARGS    = []

if platform.system() == "Linux":
    _COMPILE_ARGS = [
        "-O3",           # full optimisation
        "-march=native", # use all CPU features available on this machine
        "-ffast-math",   # float math shortcuts (safe for embeddings)
        "-fno-wrapv",    # assume no signed-int overflow (matches Python)
    ]
elif platform.system() == "Windows":
    _COMPILE_ARGS = [
        "/O2",   # MSVC optimise
        "/fp:fast",
    ]
elif platform.system() == "Darwin":
    _COMPILE_ARGS = ["-O3", "-ffast-math"]

# ── Numpy include path ────────────────────────────────────────────────────────
NP_INCLUDE = np.get_include()

# ── Extension definitions ─────────────────────────────────────────────────────
# Add new .pyx modules here as you create them.
EXTENSIONS = [
    Extension(
        name               = "back_end.slot_monitor.slot_embed_cy",
        sources            = ["back_end/slot_monitor/slot_embed_cy.pyx"],
        include_dirs       = [NP_INCLUDE],
        extra_compile_args = _COMPILE_ARGS,
        extra_link_args    = _LINK_ARGS,
    ),
    Extension(
        name               = "back_end.slot_monitor.slots_cy",
        sources            = ["back_end/slot_monitor/slots_cy.pyx"],
        include_dirs       = [NP_INCLUDE],
        extra_compile_args = _COMPILE_ARGS,
        extra_link_args    = _LINK_ARGS,
    ),
]

if not HAS_CYTHON:
    print("[setup.py] Cannot build without Cython.  Install it and retry.")
    raise SystemExit(1)

setup(
    name    = "phonebox_cy",
    version = "1.0.0",
    ext_modules = cythonize(
        EXTENSIONS,
        compiler_directives = {
            # ── Safety ────────────────────────────────────────────
            # boundscheck=False: skip index bounds checks in numpy loops.
            # Safe because every array access in our code is explicitly
            # sized before the loop.
            "boundscheck"  : False,

            # wraparound=False: skip negative-index handling.
            # We never use negative indices.
            "wraparound"   : False,

            # nonecheck=False: skip None checks on typed extension types.
            "nonecheck"    : False,

            # cdivision=True: use C division (no ZeroDivisionError check).
            # We guard divisions manually where needed.
            "cdivision"    : True,

            # ── Performance ────────────────────────────────────────
            "language_level": "3",   # Python 3 string semantics
            "profile"       : False, # set True temporarily to profile with cProfile
        },
        # Show progress during compilation
        quiet = False,
        # Always recompile even if .pyx hasn't changed (safe default)
        force = False,
    ),
    zip_safe = False,
)
