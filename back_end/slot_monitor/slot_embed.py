# back_end/slot_monitor/slot_embed.py
# ============================================================
"""
Embedding computation and utilities for slot monitoring.

This file provides the pure-Python fallback.  At import time it tries
to load the compiled Cython module (slot_embed_cy) which is 4-5× faster.
If the .so/.pyd is not built yet, it falls back to this pure-Python
implementation transparently — no errors, no configuration needed.

To build the Cython module:
    python setup.py build_ext --inplace
"""

import cv2
import numpy as np

# ── Embedding parameters (mirror config.py SlotEmbedConfig) ──────────────────
IMG_SIZE      = 64
HIST_BINS     = 32
DCT_SIZE      = 8
EMBEDDING_DIM = HIST_BINS + (DCT_SIZE * DCT_SIZE)  # 96

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
        Compute normalized embedding vector from ROI.

        Combines histogram (lighting-tolerant) and DCT (structure-sensitive).

        Args:
            roi_bgr: BGR image (ROI from camera frame)

        Returns:
            96-dimensional normalized float32 vector (32 histogram + 64 DCT)

        Raises:
            ValueError: If ROI is empty or None
        """
        if roi_bgr is None or roi_bgr.size == 0:
            raise ValueError("Invalid ROI: empty or None")

        # Convert to grayscale and resize
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)

        # 1. Histogram features — normalize in-place into a pre-sized buffer
        hist = cv2.calcHist([gray], [0], None, [HIST_BINS], [0, 256])
        cv2.normalize(hist, hist)
        hist_flat = hist.ravel()  # view, no copy

        # 2. DCT features — convert uint8→float32 in one step
        gray_f  = gray.astype(np.float32) * (1.0 / 255.0)
        dct_low = cv2.dct(gray_f)[:DCT_SIZE, :DCT_SIZE].ravel()  # view, no copy

        # 3. Combine into pre-allocated output
        emb = np.empty(EMBEDDING_DIM, dtype=np.float32)
        emb[:HIST_BINS] = hist_flat
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