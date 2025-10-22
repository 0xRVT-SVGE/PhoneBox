import hashlib
import json
import os

import numpy as np
from deepface import DeepFace


# -------------------------
# Helpers
# -------------------------
def l2_normalize(vec: np.ndarray) -> list:
    """L2 normalize a vector and return as list."""
    norm = np.linalg.norm(vec)
    return (vec / norm if norm > 0 else vec).tolist()

def hash_file(path: str) -> str:
    """Return SHA1 hash of a file (for cache invalidation)."""
    BUF_SIZE = 65536
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(BUF_SIZE):
            sha1.update(chunk)
    return sha1.hexdigest()

def process_image(img_path: str) -> list | None:
    """Return normalized embedding from image, or None on failure."""
    try:
        reps = DeepFace.represent(
            img_path=img_path,
            model_name="SFace",
            detector_backend="opencv",
            enforce_detection=False
        )
        if reps:
            embedding = np.array(reps[0]["embedding"], dtype=np.float32)
            return l2_normalize(embedding)
    except Exception as e:
        print(f"[!] Error processing {img_path}: {e}")
    return None

# -------------------------
# Paths
# -------------------------
KNOWN_FACES_DIR = "known_faces"
CACHE_JSON = "cache.json"
DATABASE_JSON = "database.json"

# -------------------------
# Load existing files
# -------------------------
cache = {}
if os.path.exists(CACHE_JSON):
    with open(CACHE_JSON, "r") as f:
        cache = json.load(f)

database = {}
if os.path.exists(DATABASE_JSON):
    with open(DATABASE_JSON, "r") as f:
        database = json.load(f)

# -------------------------
# Main loop
# -------------------------
for entry in os.listdir(KNOWN_FACES_DIR):
    if entry.startswith("."):  # skip hidden
        continue

    entry_path = os.path.join(KNOWN_FACES_DIR, entry)
    person_name = os.path.splitext(entry)[0] if os.path.isfile(entry_path) else entry
    embeddings = []

    # Collect image paths (single file or folder)
    img_paths = []
    if os.path.isdir(entry_path):
        img_paths = [
            os.path.join(entry_path, f)
            for f in os.listdir(entry_path)
            if not f.startswith(".") and os.path.isfile(os.path.join(entry_path, f))
        ]
    elif os.path.isfile(entry_path):
        img_paths = [entry_path]

    for img_path in img_paths:
        file_hash = hash_file(img_path)
        cache_key = f"{person_name}/{os.path.basename(img_path)}"

        if cache_key in cache and cache[cache_key]["hash"] == file_hash:
            embedding = cache[cache_key]["embedding"]
            print(f"Using cached embedding for {cache_key}")
        else:
            embedding = process_image(img_path)
            if embedding:
                cache[cache_key] = {"hash": file_hash, "embedding": embedding}
                print(f"[+] Processed {cache_key}")
            else:
                continue  # skip failed

        embeddings.append(np.array(embedding, dtype=np.float32))

    # Save mean embedding if valid
    if embeddings:
        mean_embedding = np.mean(embeddings, axis=0)
        if person_name not in database:
            database[person_name] = {}
        database[person_name]["embedding"] = l2_normalize(mean_embedding)
        print(f"[✓] Updated embedding for {person_name} ({len(embeddings)} images)")
    else:
        print(f"[!] No valid embeddings for {person_name}")

# -------------------------
# Save cache + database
# -------------------------
with open(CACHE_JSON, "w") as f:
    json.dump(cache, f, indent=2)

with open(DATABASE_JSON, "w") as f:
    json.dump(database, f, indent=2)

print(f"\n✅ Cache saved to {CACHE_JSON}")
print(f"✅ Database updated in {DATABASE_JSON}")
