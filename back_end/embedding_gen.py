# embedding_gen.py
import asyncio
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import _deepface_represent, l2_normalize
from back_end.config import ScannerConfig as _SC, EmbeddingConfig as _EC

_embedding_executor = ThreadPoolExecutor(max_workers=_EC.MAX_WORKERS)

# Cached scale factor — same pattern as scanner_worker._resize_scale.
# Computed once on first call; camera resolution is fixed for process lifetime.
_embed_resize_scale: float | None = None


async def generate_embedding():
    global _embed_resize_scale

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, scanner_state.photo_taken_event.wait)

    frame = scanner_state.get_rframe()
    if frame is None:
        return None

    try:
        if _embed_resize_scale is None:
            _embed_resize_scale = _SC.SCALED_WIDTH / frame.shape[1]

        resized = cv2.resize(
            frame,
            (_SC.SCALED_WIDTH, int(frame.shape[0] * _embed_resize_scale)),
        )

        results = await loop.run_in_executor(
            _embedding_executor, _deepface_represent, resized
        )
        if not results:
            return None

        largest = max(results, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"])
        return l2_normalize(np.array(largest["embedding"], dtype=np.float32))

    finally:
        scanner_state.photo_taken_event.clear()