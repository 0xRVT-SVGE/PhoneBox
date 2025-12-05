# embedding_gen.py
import asyncio
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from back_end.scanner_state import scanner_state
from back_end.scanner_worker import _deepface_represent, l2_normalize

# Create a dedicated thread pool for embeddings
_embedding_executor = ThreadPoolExecutor(max_workers=2)

async def generate_embedding():
    """
    Waits for photo_taken_event to be set,
    then reads latest_rframe, computes face embedding, and returns it.
    """
    # Wait until a photo is taken
    loop = asyncio.get_running_loop()
    # Wait for the threading.Event to be set without blocking the event loop
    await loop.run_in_executor(None, scanner_state.photo_taken_event.wait)

    frame = scanner_state.get_rframe()
    if frame is None:
        return None

    try:
        # Resize for DeepFace consistency (reuse your scanner_worker logic)
        height, width = frame.shape[:2]
        scale = 720 / width
        resized = cv2.resize(frame, (720, int(height * scale)))

        # Compute embedding in thread pool (non-blocking for asyncio)
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(_embedding_executor, _deepface_represent, resized)

        if not results:
            return None

        # Pick the largest detected face
        largest = max(results, key=lambda f: f["facial_area"]["w"] * f["facial_area"]["h"])
        embed = l2_normalize(np.array(largest["embedding"], dtype=np.float32))
        return embed

    finally:
        # Reset the event so it can be reused
        scanner_state.photo_taken_event.clear()
