# ============================================================
# Opt #26 — Redis student embed cache
# FILE: back_end/scanner_worker_cache.py  (add alongside scanner_worker.py)
# ============================================================
"""
Redis-backed cache for student embed lookups in scanner_worker.py.

Every barcode scan calls fetch_student_by_sid() which hits the REST
API → PostgreSQL.  At 30 fps this means potentially dozens of DB
roundtrips per second when a student holds their badge steady.

This module wraps fetch_student_by_sid() with a 60-second Redis TTL.
The embed vector (~1-2 KB) is serialised as msgpack, negligible memory.

Install:
    pip install redis msgpack

Redis server (local, no auth):
    sudo apt install redis-server
    redis-server --daemonize yes

For password-protected Redis, set PHONEBOX_REDIS_URL:
    export PHONEBOX_REDIS_URL="redis://:password@localhost:6379/0"
"""

import json
import logging
import os
import time
from typing import Optional, Dict, Any

import numpy as np

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60   # seconds a student record lives in cache


# ── Redis client (optional) ───────────────────────────────────────────────────

_redis_client = None
_REDIS_AVAILABLE = False

def _get_redis():
    global _redis_client, _REDIS_AVAILABLE
    if _redis_client is not None:
        return _redis_client
    try:
        import redis
        url = os.environ.get("PHONEBOX_REDIS_URL", "redis://localhost:6379/0")
        _redis_client = redis.from_url(url, decode_responses=False, socket_connect_timeout=1)
        _redis_client.ping()
        _REDIS_AVAILABLE = True
        logger.info(f"[StudentCache] Redis connected: {url}")
        return _redis_client
    except Exception as exc:
        _REDIS_AVAILABLE = False
        logger.info(f"[StudentCache] Redis unavailable ({exc}) — cache disabled")
        return None


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _serialise(student: Dict) -> bytes:
    """Serialise student dict to JSON bytes. Converts numpy array to list."""
    payload = dict(student)
    emb = payload.get("embed")
    if emb is not None and hasattr(emb, "tolist"):
        payload["embed"] = emb.tolist()
    return json.dumps(payload).encode()


def _deserialise(data: bytes, parse_pg_array_fn) -> Optional[Dict]:
    """Deserialise student dict from JSON bytes, restoring embed as numpy array."""
    try:
        student = json.loads(data.decode())
        if student.get("embed") is not None:
            student["embed"] = parse_pg_array_fn(student["embed"])
        return student
    except Exception as exc:
        logger.debug(f"[StudentCache] Deserialise error: {exc}")
        return None


# ── Cached lookup ─────────────────────────────────────────────────────────────

def fetch_student_cached(sid: str, fetch_fn, parse_pg_array_fn) -> Optional[Dict]:
    """
    Fetch a student by SID with a Redis-backed TTL cache.

    Args:
        sid:               Student ID string.
        fetch_fn:          Original fetch_student_by_sid function.
        parse_pg_array_fn: parse_pg_array from scanner_worker.

    Returns:
        Student dict with 'embed' as numpy array, or None if not found.
    """
    key = f"phonebox:student:{sid}"
    r = _get_redis()

    # ── Cache hit ──────────────────────────────────────
    if r is not None:
        try:
            cached = r.get(key)
            if cached is not None:
                student = _deserialise(cached, parse_pg_array_fn)
                if student is not None:
                    return student
        except Exception as exc:
            logger.debug(f"[StudentCache] Redis get error: {exc}")

    # ── Cache miss — hit the API ───────────────────────
    student = fetch_fn(sid)
    if student is None:
        return None

    # ── Write to cache ─────────────────────────────────
    if r is not None:
        try:
            r.setex(key, _CACHE_TTL_S, _serialise(student))
        except Exception as exc:
            logger.debug(f"[StudentCache] Redis set error: {exc}")

    return student


def invalidate(sid: str) -> None:
    """
    Evict a student from the cache.

    Call this after updating a student's embed so the next scan picks
    up the new value instead of serving a stale cached record.
    """
    r = _get_redis()
    if r is None:
        return
    key = f"phonebox:student:{sid}"
    try:
        r.delete(key)
        logger.debug(f"[StudentCache] Evicted: {sid}")
    except Exception as exc:
        logger.debug(f"[StudentCache] Evict error: {exc}")