# ============================================================
# FILE: back_end/slot_monitor/db_interface.py
# ============================================================
"""
Unified database interface for slot monitoring.

Opt #36 — pgvector integration
--------------------------------
save_baseline() now writes to BOTH the legacy `embedding` BYTEA column
and the new `embedding_vec` VECTOR(96) column when the migration has
been applied.  Reading (fetch_all_baselines) stays on bytea — the slot
monitor's runtime behaviour is completely unchanged.

Backward compatibility: if the `embedding_vec` column does not yet
exist (migration not run), the code detects this once at startup and
silently falls back to bytea-only writes.  Nothing breaks either way.
"""

import asyncio
import logging
import numpy as np
from typing import Dict, List, Tuple, Optional
import asyncpg
from back_end.Database.db import get_conn, put_conn
from back_end.secrets import Secrets
from back_end.config import DatabaseConfig as _DC

logger = logging.getLogger(__name__)


# ============================================================
# EMBEDDING SERIALIZATION
# ============================================================

def embedding_to_bytes(emb: np.ndarray) -> bytes:
    return emb.astype(np.float32).tobytes()


def embedding_from_bytes(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def _embedding_to_pg_vector_str(emb: np.ndarray) -> str:
    """
    Format embedding as a pgvector literal string: '[0.1,0.2,...]'
    Works with both psycopg2 (%s::vector cast) and asyncpg ($n::vector cast).
    """
    return '[' + ','.join(f'{float(x):.8g}' for x in emb) + ']'


# ============================================================
# SYNC DATABASE INTERFACE (Legacy/Calibration)
# ============================================================

# Opt #36: lazy check — set to True/False after first call, None = unchecked.
_PGVECTOR_COL_EXISTS: Optional[bool] = None


def _check_pgvector_column_sync() -> bool:
    """Return True if embedding_vec column exists in slot_baselines."""
    global _PGVECTOR_COL_EXISTS
    if _PGVECTOR_COL_EXISTS is not None:
        return _PGVECTOR_COL_EXISTS
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE  table_name  = 'slot_baselines'
                    AND    column_name = 'embedding_vec'
                    LIMIT  1
                """)
                _PGVECTOR_COL_EXISTS = cur.fetchone() is not None
        finally:
            put_conn(conn)
    except Exception as exc:
        logger.warning(f"[DB] pgvector column check failed: {exc}")
        _PGVECTOR_COL_EXISTS = False

    logger.info(
        f"[DB] pgvector embedding_vec: "
        f"{'available' if _PGVECTOR_COL_EXISTS else 'not migrated — bytea-only writes'}"
    )
    return _PGVECTOR_COL_EXISTS


class SlotMonitorDB:

    @staticmethod
    def fetch_occupied_slots() -> List[Tuple[int, str]]:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT lid, pid
                    FROM phone_storage
                    WHERE retrieved_at IS NULL
                    ORDER BY lid;
                """)
                rows = cur.fetchall()
                logger.info(f"Fetched {len(rows)} occupied slots from DB")
                return rows
        except Exception as e:
            logger.error(f"Failed to fetch occupied slots: {e}")
            return []
        finally:
            put_conn(conn)

    @staticmethod
    def fetch_all_baselines() -> Dict[int, np.ndarray]:
        """
        Read baselines from the legacy bytea column.
        Reading is intentionally unchanged — slot monitor runtime is unaffected.
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT lid, embedding
                    FROM slot_baselines
                    WHERE embedding IS NOT NULL
                      AND embedding != '\\x00'::bytea
                    ORDER BY lid;
                """)
                rows      = cur.fetchall()
                baselines = {}
                for lid, emb_bytes in rows:
                    if emb_bytes:
                        baselines[lid] = embedding_from_bytes(emb_bytes)
                logger.info(f"Fetched {len(baselines)} baselines from DB")
                return baselines
        except Exception as e:
            logger.error(f"Failed to fetch baselines: {e}")
            return {}
        finally:
            put_conn(conn)

    @staticmethod
    def save_baseline(lid: int, embedding: np.ndarray):
        """
        Persist a slot baseline.

        Opt #36: writes to both `embedding` (bytea) and `embedding_vec`
        (vector) when the migration has been applied.  Falls back silently
        to bytea-only if the column does not exist.
        """
        conn = get_conn()
        try:
            emb_bytes  = embedding_to_bytes(embedding)
            use_vector = _check_pgvector_column_sync()

            with conn.cursor() as cur:
                if use_vector:
                    vec_str = _embedding_to_pg_vector_str(embedding)
                    cur.execute("""
                        INSERT INTO slot_baselines (lid, embedding, embedding_vec, calibrated_at)
                        VALUES (%s, %s, %s::vector, NOW())
                        ON CONFLICT (lid) DO UPDATE SET
                            embedding     = EXCLUDED.embedding,
                            embedding_vec = EXCLUDED.embedding_vec,
                            calibrated_at = NOW()
                    """, (lid, emb_bytes, vec_str))
                else:
                    cur.execute("""
                        INSERT INTO slot_baselines (lid, embedding)
                        VALUES (%s, %s)
                        ON CONFLICT (lid) DO UPDATE SET
                            embedding     = EXCLUDED.embedding,
                            calibrated_at = NOW()
                    """, (lid, emb_bytes))
                conn.commit()
                logger.debug(f"Saved baseline for slot {lid} (vector={use_vector})")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to save baseline for slot {lid}: {e}")
        finally:
            put_conn(conn)

    @staticmethod
    def get_pid_for_lid(lid: int) -> Optional[str]:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT pid FROM phone_storage
                    WHERE lid = %s AND retrieved_at IS NULL LIMIT 1;
                """, (lid,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get PID for LID {lid}: {e}")
            return None
        finally:
            put_conn(conn)

    @staticmethod
    def is_slot_occupied(lid: int) -> bool:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS(SELECT 1 FROM phone_storage
                                  WHERE lid = %s AND retrieved_at IS NULL);
                """, (lid,))
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to check occupancy for slot {lid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def count_stored_phones() -> Optional[int]:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM phone_storage WHERE retrieved_at IS NULL;"
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"count_stored_phones failed: {e}")
            return None
        finally:
            put_conn(conn)

    @staticmethod
    def pid_exists(pid: str) -> bool:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM phones WHERE pid = %s);", (pid,)
                )
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to check PID existence for {pid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def is_phone_stored(pid: str) -> bool:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT EXISTS(SELECT 1 FROM phone_storage
                                  WHERE pid = %s AND retrieved_at IS NULL);
                """, (pid,))
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to check storage status for PID {pid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def get_next_free_lid() -> Optional[int]:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT l.lid
                    FROM locations l
                    LEFT JOIN phone_storage ps
                           ON l.lid = ps.lid AND ps.retrieved_at IS NULL
                    WHERE ps.pid IS NULL
                    ORDER BY l.lid
                    LIMIT 1;
                """)
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get next free LID: {e}")
            return None
        finally:
            put_conn(conn)

    @staticmethod
    def get_lid_for_pid(pid: str) -> Optional[int]:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT lid FROM phone_storage
                    WHERE pid = %s AND retrieved_at IS NULL LIMIT 1;
                """, (pid,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get LID for PID {pid}: {e}")
            return None
        finally:
            put_conn(conn)

    @staticmethod
    def update_storage_lid(pid: str, new_lid: int) -> bool:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE phone_storage SET lid = %s
                    WHERE pid = %s AND retrieved_at IS NULL;
                """, (new_lid, pid))
                conn.commit()
                logger.debug(f"Updated storage LID for PID={pid} to LID={new_lid}")
                return True
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to update storage LID for PID={pid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def get_sid_for_pid(pid: str):
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT sid FROM phones WHERE pid = %s;", (pid,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"get_sid_for_pid PID={pid}: {e}")
            return None
        finally:
            put_conn(conn)


# ============================================================
# ASYNC DATABASE INTERFACE (Real-time Monitoring)
# ============================================================

_CACHE_MISS = object()


class AsyncSlotMonitorDB:
    """
    Async database interface for slot monitoring.
    Uses asyncpg connection pool for high-performance real-time operations.
    """

    def __init__(
            self,
            host:         str = _DC.ASYNC_HOST,
            port:         int = _DC.ASYNC_PORT,
            database:     str = _DC.ASYNC_DATABASE,
            user:         str = Secrets.DB_USER,
            password:     str = Secrets.DB_PASSWORD,
            min_pool_size: int = _DC.ASYNC_POOL_MIN,
            max_pool_size: int = _DC.ASYNC_POOL_MAX,
    ):
        self.host          = host
        self.port          = port
        self.database      = database
        self.user          = user
        self.password      = password
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size

        self._pool: Optional[asyncpg.Pool] = None
        self._pid_cache: Dict[int, str]    = {}
        self._cache_lock = asyncio.Lock()

        # Opt #36: detected once in connect()
        self._pgvector_ready: bool = False

    async def connect(self):
        if self._pool is not None:
            logger.warning("Connection pool already exists")
            return

        self._pool = await asyncpg.create_pool(
            host            = self.host,
            port            = self.port,
            database        = self.database,
            user            = self.user,
            password        = self.password,
            min_size        = self.min_pool_size,
            max_size        = self.max_pool_size,
            command_timeout = _DC.ASYNC_CMD_TIMEOUT,
        )

        logger.info(
            f"AsyncPG pool created: {self.min_pool_size}-{self.max_pool_size} "
            f"connections to {self.host}:{self.port}/{self.database}"
        )

        # Opt #36: detect vector column once, log result
        self._pgvector_ready = await self._detect_pgvector()
        logger.info(
            f"[AsyncDB] pgvector embedding_vec: "
            f"{'available' if self._pgvector_ready else 'not migrated — bytea-only writes'}"
        )

    async def _detect_pgvector(self) -> bool:
        """Return True if the embedding_vec column exists in slot_baselines."""
        try:
            row = await self._pool.fetchrow("""
                SELECT 1 FROM information_schema.columns
                WHERE  table_name  = 'slot_baselines'
                AND    column_name = 'embedding_vec'
                LIMIT  1
            """)
            return row is not None
        except Exception as exc:
            logger.warning(f"[AsyncDB] pgvector detection failed: {exc}")
            return False

    async def close(self):
        if self._pool:
            await self._pool.close()
            logger.info("AsyncPG pool closed")

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

    # ── Baseline operations ───────────────────────────────

    async def save_baseline(self, lid: int, embedding: np.ndarray):
        """
        Persist a slot baseline.

        Opt #36: writes to both `embedding` (bytea) and `embedding_vec`
        (vector) when the migration has been applied.
        """
        self._require_pool()
        emb_bytes = embedding_to_bytes(embedding)

        if self._pgvector_ready:
            vec_str = _embedding_to_pg_vector_str(embedding)
            await self._pool.execute(
                """
                INSERT INTO slot_baselines (lid, embedding, embedding_vec, calibrated_at)
                VALUES ($1, $2, $3::vector, NOW())
                ON CONFLICT (lid) DO UPDATE SET
                    embedding     = EXCLUDED.embedding,
                    embedding_vec = EXCLUDED.embedding_vec,
                    calibrated_at = EXCLUDED.calibrated_at
                """,
                lid, emb_bytes, vec_str,
            )
        else:
            await self._pool.execute(
                """
                INSERT INTO slot_baselines (lid, embedding, calibrated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (lid) DO UPDATE SET
                    embedding     = EXCLUDED.embedding,
                    calibrated_at = EXCLUDED.calibrated_at
                """,
                lid, emb_bytes,
            )

    async def fetch_baseline(self, lid: int) -> Optional[np.ndarray]:
        self._require_pool()
        row = await self._pool.fetchrow(
            "SELECT embedding FROM slot_baselines WHERE lid = $1", lid
        )
        return embedding_from_bytes(row["embedding"]) if row else None

    async def fetch_all_baselines(self) -> Dict[int, np.ndarray]:
        """
        Read baselines from the legacy bytea column.
        Reading is intentionally unchanged — slot monitor runtime is unaffected.
        """
        self._require_pool()
        rows = await self._pool.fetch(
            """
            SELECT lid, embedding FROM slot_baselines
            WHERE embedding IS NOT NULL AND embedding != '\\x00'::bytea
            ORDER BY lid
            """
        )
        baselines = {
            row["lid"]: embedding_from_bytes(row["embedding"])
            for row in rows
        }
        logger.info(f"Fetched {len(baselines)} baselines from DB (async)")
        return baselines

    async def delete_baseline(self, lid: int):
        self._require_pool()
        await self._pool.execute(
            "DELETE FROM slot_baselines WHERE lid = $1", lid
        )

    async def save_baselines_batch(self, baselines: Dict[int, np.ndarray]):
        """
        Batch-save multiple baselines in a single transaction.

        Opt #36: includes embedding_vec when the column is available.
        """
        self._require_pool()
        if not baselines:
            return

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if self._pgvector_ready:
                    data = [
                        (lid, embedding_to_bytes(emb),
                         _embedding_to_pg_vector_str(emb))
                        for lid, emb in baselines.items()
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO slot_baselines
                            (lid, embedding, embedding_vec, calibrated_at)
                        VALUES ($1, $2, $3::vector, NOW())
                        ON CONFLICT (lid) DO UPDATE SET
                            embedding     = EXCLUDED.embedding,
                            embedding_vec = EXCLUDED.embedding_vec,
                            calibrated_at = EXCLUDED.calibrated_at
                        """,
                        data,
                    )
                else:
                    data = [
                        (lid, embedding_to_bytes(emb))
                        for lid, emb in baselines.items()
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO slot_baselines (lid, embedding, calibrated_at)
                        VALUES ($1, $2, NOW())
                        ON CONFLICT (lid) DO UPDATE SET
                            embedding     = EXCLUDED.embedding,
                            calibrated_at = EXCLUDED.calibrated_at
                        """,
                        data,
                    )
        logger.info(f"Saved {len(baselines)} baselines in batch (async, vector={self._pgvector_ready})")

    # ── Occupancy queries ─────────────────────────────────

    async def get_num_lid(self) -> int:
        self._require_pool()
        num_lid = await self._pool.fetchval(
            "SELECT COALESCE(MAX(lid), 0) + 1 FROM locations;"
        )
        return num_lid or 1

    async def get_next_free_lid(self) -> Optional[int]:
        self._require_pool()
        return await self._pool.fetchval(
            """
            SELECT l.lid FROM locations l
            WHERE NOT EXISTS (
                SELECT 1 FROM phone_storage ps
                WHERE ps.lid = l.lid AND ps.retrieved_at IS NULL
            )
            ORDER BY l.lid
            FOR UPDATE SKIP LOCKED
            LIMIT 1;
            """
        )

    async def fetch_occupied_slots(self) -> List[Tuple[int, str]]:
        self._require_pool()
        rows = await self._pool.fetch(
            """
            SELECT lid, pid FROM phone_storage
            WHERE retrieved_at IS NULL ORDER BY lid
            """
        )
        result = [(row["lid"], row["pid"]) for row in rows]
        logger.info(f"Fetched {len(result)} occupied slots from DB (async)")
        return result

    async def get_pid_for_lid(self, lid: int) -> Optional[str]:
        cached = self._pid_cache.get(lid, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached

        self._require_pool()
        async with self._cache_lock:
            cached = self._pid_cache.get(lid, _CACHE_MISS)
            if cached is not _CACHE_MISS:
                return cached
            row = await self._pool.fetchrow(
                """
                SELECT pid FROM phone_storage
                WHERE lid = $1 AND retrieved_at IS NULL
                """,
                lid,
            )
            value = row["pid"] if row else None
            self._pid_cache[lid] = value
            return value

    async def get_lid_for_pid(self, pid: str) -> Optional[int]:
        self._require_pool()
        row = await self._pool.fetchrow(
            """
            SELECT lid FROM phone_storage
            WHERE pid = $1 AND retrieved_at IS NULL LIMIT 1
            """,
            pid,
        )
        return row["lid"] if row else None

    async def is_slot_occupied(self, lid: int) -> bool:
        self._require_pool()
        return await self._pool.fetchval(
            """
            SELECT EXISTS(SELECT 1 FROM phone_storage
                          WHERE lid = $1 AND retrieved_at IS NULL)
            """,
            lid,
        )

    async def update_storage_lid(self, pid: str, new_lid: int) -> bool:
        self._require_pool()
        try:
            await self._pool.execute(
                """
                UPDATE phone_storage SET lid = $1
                WHERE pid = $2 AND retrieved_at IS NULL
                """,
                new_lid, pid,
            )
            await self._invalidate_cache(new_lid)
            return True
        except Exception as e:
            logger.error(f"Failed to update storage LID for PID={pid}: {e}")
            return False

    # ── Cache management ──────────────────────────────────

    def invalidate_pid_cache_sync(self, lid: Optional[int] = None):
        if lid is not None:
            self._pid_cache.pop(lid, None)
        else:
            self._pid_cache.clear()

    async def _invalidate_cache(self, lid: Optional[int] = None):
        async with self._cache_lock:
            if lid is not None:
                self._pid_cache.pop(lid, None)
            else:
                self._pid_cache.clear()

    # ── Health check ──────────────────────────────────────

    async def is_healthy(self) -> bool:
        if self._pool is None:
            return False
        try:
            await self._pool.fetchval("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False


# ============================================================
# ASYNC CONTEXT MANAGER
# ============================================================

class AsyncDBContext:
    def __init__(self, **kwargs):
        self.db = AsyncSlotMonitorDB(**kwargs)

    async def __aenter__(self):
        await self.db.connect()
        return self.db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.db.close()