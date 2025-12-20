# server/slot_monitor/slot_embed.py

import cv2
import numpy as np

IMG_SIZE = 64
HIST_BINS = 32
DCT_SIZE = 8  # 8x8 = 64 coeffs


def compute_embedding(roi_bgr: np.ndarray) -> np.ndarray:
    """
    Returns a normalized embedding vector.
    """
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
    emb = emb / (np.linalg.norm(emb) + 1e-8)

    return emb


def embedding_distance(e1: np.ndarray, e2: np.ndarray) -> float:
    # cosine distance
    return 1.0 - float(np.dot(e1, e2))
