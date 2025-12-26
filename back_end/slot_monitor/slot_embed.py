# ============================================================
# FILE: server/slot_monitor/slot_embed.py
# ============================================================

import cv2
import numpy as np

IMG_SIZE = 64
HIST_BINS = 32
DCT_SIZE = 8  # 8x8 = 64 coeffs


def compute_embedding(roi_bgr: np.ndarray) -> np.ndarray:
    """
    Compute normalized embedding vector from ROI.
    Returns 96-dimensional vector (32 histogram + 64 DCT) as float32.
    """
    if roi_bgr is None or roi_bgr.size == 0:
        raise ValueError("Invalid ROI: empty or None")

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (IMG_SIZE, IMG_SIZE), interpolation=cv2.INTER_AREA)

    # Histogram (lighting-tolerant)
    hist = cv2.calcHist([gray], [0], None, [HIST_BINS], [0, 256])
    hist = cv2.normalize(hist, hist).flatten()

    # DCT (structure-sensitive)
    gray_f = np.float32(gray) / 255.0
    dct = cv2.dct(gray_f)
    dct_low = dct[:DCT_SIZE, :DCT_SIZE].flatten()

    emb = np.concatenate([hist, dct_low])
    norm = np.linalg.norm(emb)
    if norm > 1e-8:
        emb = emb / norm

    return emb.astype(np.float32)


def embedding_distance(e1: np.ndarray, e2: np.ndarray) -> float:
    """Compute cosine distance between embeddings"""
    return float(1.0 - np.dot(e1, e2))


def embedding_to_bytes(emb: np.ndarray) -> bytes:
    """Convert numpy float32 array to bytes for database storage (BYTEA)"""
    return emb.astype(np.float32).tobytes()


def embedding_from_bytes(data: bytes) -> np.ndarray:
    """Convert bytes from database to numpy float32 array"""
    return np.frombuffer(data, dtype=np.float32)
