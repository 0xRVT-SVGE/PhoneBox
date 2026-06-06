# back_end/slot_monitor/slot_embed.py
# ============================================================
"""
Embedding computation and utilities for slot monitoring.

This file provides the pure-Python fallback.  At import time it tries
to load the compiled Cython module (slot_embed_cy) which is 4-5× faster.
If the .so/.pyd is not built yet, it falls back to this pure-Python
implementation transparently — no errors, no configuration needed.

v2 changes (embedding algorithm)
──────────────────────────────────
Histogram : grayscale 32-bin → HSV 3-channel (16 H + 8 S + 8 V = 32 bins)
  • Hue catches same-brightness colour-bypass attempts and distinguishes
    skin tone from phone back and empty-slot material.
DCT input : raw grayscale → CLAHE-equalised grayscale
  • CLAHE normalises local contrast so a hand shadow / ambient light
    shift does not spike the DC/low-frequency DCT coefficients.
Dimension (96) and serialisation format are UNCHANGED.
After deploying run embed_calibration.py → option 1 to rebuild baselines.

To build the Cython module:
    python setup.py build_ext --inplace
"""

import cv2
import numpy as np

# ── Embedding parameters (mirror config.py SlotEmbedConfig) ──────────────────
IMG_SIZE      = 64
HIST_BINS     = 32   # 16 H + 8 S + 8 V — still 32 total
DCT_SIZE      = 8
EMBEDDING_DIM = HIST_BINS + (DCT_SIZE * DCT_SIZE)  # 96 — unchanged

# ── Try to load the compiled Cython module ────────────────────────────────────
# If slot_embed_cy.so / .pyd exists (built via setup.py build_ext --inplace),
# we use its implementations.  Otherwise we fall through to the pure-Python
# functions below.  Callers throughout the project import from this file and
# are completely unaware of which implementation they're getting.

_CY_AVAILABLE = False
try:
    from back_end.slot_monitor.slot_embed_cy import (
        compute_embedding,
        embedding_distance,
        embedding_to_bytes,
        embedding_from_bytes,
    )
    _CY_AVAILABLE = True
    import logging as _logging
    _logging.getLogger(__name__).info(
        "[SlotEmbed] Cython module loaded — using compiled hot path"
    )
except ImportError:
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "[SlotEmbed] Cython module not found — using pure Python. "
        "Run 'python setup.py build_ext --inplace' to compile."
    )


# ── Pure-Python implementations (used when Cython is not built) ──────────────
# These are only defined if the Cython import above failed.

if not _CY_AVAILABLE:

    def compute_embedding(roi_bgr: np.ndarray) -> np.ndarray:
        """
        Compute normalized 96-D embedding vector from a BGR ROI.

        96-D = 32-bin HSV color histogram + 64 DCT structural features.

        Changes from v1
        ───────────────
        Histogram: grayscale → HSV (H 16 bins, S 8 bins, V 8 bins)
          • Hue catches same-brightness colour-bypass attempts and
            distinguishes skin tone from phone back / empty-slot material.
        DCT input: raw grayscale → CLAHE-equalised grayscale
          • CLAHE normalises local contrast so a hand shadow or brief
            ambient light shift does not spike the DC/low-freq coefficients.

        Dimension and serialisation format are identical to v1.
        Run embed_calibration.py → option 1 after deploying.

        Args:
            roi_bgr: BGR image (ROI from camera frame)

        Returns:
            96-dimensional normalized float32 vector (32 histogram + 64 DCT)

        Raises:
            ValueError: If ROI is empty or None
        """
        if roi_bgr is None or roi_bgr.size == 0:
            raise ValueError("Invalid ROI: empty or None")

        resized = cv2.resize(roi_bgr, (IMG_SIZE, IMG_SIZE),
                             interpolation=cv2.INTER_AREA)

        # ── 1. Color histogram in HSV (32 bins total) ─────────────────────
        hsv    = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        h_hist = cv2.calcHist([hsv], [0], None, [16], [0, 180])   # Hue
        s_hist = cv2.calcHist([hsv], [1], None, [8],  [0, 256])   # Saturation
        v_hist = cv2.calcHist([hsv], [2], None, [8],  [0, 256])   # Value
        cv2.normalize(h_hist, h_hist)
        cv2.normalize(s_hist, s_hist)
        cv2.normalize(v_hist, v_hist)
        color_hist = np.concatenate([h_hist.ravel(),
                                     s_hist.ravel(),
                                     v_hist.ravel()])              # shape (32,)

        # ── 2. DCT features on CLAHE-equalised grayscale (64 dims) ────────
        # createCLAHE() per call: construction is ~1 µs and is the only safe
        # pattern for concurrent ThreadPoolExecutor usage — CLAHE.apply() is
        # not guaranteed thread-safe on a shared instance across all OpenCV
        # builds.
        gray    = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        gray_eq = cv2.createCLAHE(clipLimit=2.0,
                                   tileGridSize=(4, 4)).apply(gray)
        gray_f  = gray_eq.astype(np.float32) * (1.0 / 255.0)
        dct_low = cv2.dct(gray_f)[:DCT_SIZE, :DCT_SIZE].ravel()   # shape (64,)

        # ── 3. Concatenate and L2-normalise → 96-D unit vector ────────────
        emb = np.empty(EMBEDDING_DIM, dtype=np.float32)
        emb[:HIST_BINS] = color_hist
        emb[HIST_BINS:] = dct_low

        norm = np.linalg.norm(emb)
        if norm > 1e-8:
            emb /= norm  # in-place divide

        return emb

    def embedding_distance(e1: np.ndarray, e2: np.ndarray) -> float:
        """
        Compute cosine distance between embeddings.

        Args:
            e1: First embedding vector
            e2: Second embedding vector

        Returns:
            Distance in [0, 2] where 0 = identical, 2 = opposite
        """
        return float(1.0 - np.dot(e1, e2))

    def embedding_to_bytes(emb: np.ndarray) -> bytes:
        """
        Convert numpy float32 array to bytes for database storage (BYTEA).

        Args:
            emb: Embedding vector (float32)

        Returns:
            Raw bytes suitable for PostgreSQL BYTEA column
        """
        return emb.astype(np.float32).tobytes()

    def embedding_from_bytes(data: bytes) -> np.ndarray:
        """
        Convert bytes from database to numpy float32 array.

        Args:
            data: Raw bytes from PostgreSQL BYTEA column

        Returns:
            Embedding vector as float32 numpy array
        """
        return np.frombuffer(data, dtype=np.float32)