# ============================================================
# FILE: back_end/slot_monitor/db_interface.py
# ============================================================
"""
Unified database interface for slot monitoring.

Opt #25 — LISTEN/NOTIFY
------------------------
AsyncSlotMonitorDB holds one dedicated asyncpg connection that LISTENs
on 'phonebox_storage_change'.  When PostgreSQL fires the trigger
(notify_storage_change in DBQuery.sql), _on_storage_notify() invalidates
the in-process pid→lid cache immediately — no polling.

Graceful fallback: if the LISTEN connection fails (trigger not yet in DB,
network issue) the pool operates normally and manual invalidation still works.

Opt #36 — pgvector dual-write
------------------------------
save_baseline() writes BOTH the legacy `embedding` BYTEA column and
`embedding_vec vector(96)`.  Reads always use BYTEA (unchanged runtime).

Backward compat: if the vector column does not exist (migration not run),
a try/except falls back to BYTEA-only writes automatically.
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


def _vec_str(emb: np.ndarray) -> str:
    """Format numpy array as pgvector literal: '[0.1,0.2,...]'"""
    return '[' + ','.join(f'{float(v):.8g}' for v in emb) + ']'


# ============================================================
# SYNC DATABASE INTERFACE  (psycopg2, used by calibration tools
#                           and SlotOperations)
# ============================================================

# Opt #36: lazy one-time check for embedding_vec column
_PGVECTOR_COL_EXISTS: Optional[bool] = None


def _check_pgvector_column_sync() -> bool:
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
                    SELECT lid, pid FROM phone_storage
                    WHERE retrieved_at IS NULL ORDER BY lid;
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
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT lid, embedding FROM slot_baselines
                    WHERE embedding IS NOT NULL
                      AND embedding != '\\x00'::bytea
                    ORDER BY lid;
                """)
                rows = cur.fetchall()
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
        Opt #36: writes to both BYTEA and vector(96) when migration applied.
        Falls back to BYTEA-only silently.
        """
        conn = get_conn()
        try:
            emb_bytes = embedding_to_bytes(embedding)
            use_vector = _check_pgvector_column_sync()

            with conn.cursor() as cur:
                if use_vector:
                    try:
                        cur.execute("""
                            INSERT INTO slot_baselines
                                (lid, embedding, embedding_vec, calibrated_at)
                            VALUES (%s, %s, %s::vector(96), NOW())
                            ON CONFLICT (lid) DO UPDATE SET
                                embedding     = EXCLUDED.embedding,
                                embedding_vec = EXCLUDED.embedding_vec,
                                calibrated_at = NOW()
                        """, (lid, emb_bytes, _vec_str(embedding)))
                    except Exception:
                        conn.rollback()
                        # Column exists in schema but type cast failed — fall back
                        cur.execute("""
                            INSERT INTO slot_baselines (lid, embedding, calibrated_at)
                            VALUES (%s, %s, NOW())
                            ON CONFLICT (lid) DO UPDATE SET
                                embedding     = EXCLUDED.embedding,
                                calibrated_at = NOW()
                        """, (lid, emb_bytes))
                else:
                    cur.execute("""
                        INSERT INTO slot_baselines (lid, embedding, calibrated_at)
                        VALUES (%s, %s, NOW())
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
            logger.error(f"Failed to check PID existence: {e}")
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
                    SELECT l.lid FROM locations l
                    LEFT JOIN phone_storage ps
                           ON l.lid = ps.lid AND ps.retrieved_at IS NULL
                    WHERE ps.pid IS NULL
                    ORDER BY l.lid LIMIT 1;
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
# ASYNC DATABASE INTERFACE  (asyncpg, used by HeadlessSlotMonitor)
# ============================================================

_CACHE_MISS = object()


class AsyncSlotMonitorDB:
    """
    Async database interface for slot monitoring.

    Opt #25: dedicated asyncpg LISTEN/NOTIFY connection invalidates the
             pid→lid cache the moment PostgreSQL fires the trigger.
    Opt #36: save_baseline writes both BYTEA and vector(96).
    """

    def __init__(
        self,
        host:          str = _DC.ASYNC_HOST,
        port:          int = _DC.ASYNC_PORT,
        database:      str = _DC.ASYNC_DATABASE,
        user:          str = Secrets.DB_USER,
        password:      str = Secrets.DB_PASSWORD,
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

        # Opt #25: dedicated connection for LISTEN/NOTIFY
        self._listen_conn: Optional[asyncpg.Connection] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

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

        # Opt #36: detect vector column once
        self._pgvector_ready = await self._detect_pgvector()
        logger.info(
            f"[AsyncDB] pgvector embedding_vec: "
            f"{'available' if self._pgvector_ready else 'not migrated — bytea-only writes'}"
        )

        # Opt #25: start LISTEN/NOTIFY listener
        await self._setup_notify_listener()

    async def close(self):
        # Opt #25: close listener before pool
        if self._listen_conn:
            try:
                await self._listen_conn.close()
                logger.info("[AsyncDB] LISTEN/NOTIFY connection closed")
            except Exception:
                pass
            self._listen_conn = None

        if self._pool:
            await self._pool.close()
            logger.info("AsyncPG pool closed")

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

    # ── Opt #25: LISTEN/NOTIFY ────────────────────────────────────────────────

    async def _setup_notify_listener(self) -> None:
        """
        Open a dedicated asyncpg connection and register a LISTEN callback.

        A separate connection is required because asyncpg pool connections
        cannot hold persistent channel registrations.

        Graceful: if setup fails the pool continues normally.
        """
        try:
            self._listen_conn = await asyncpg.connect(
                host=self.host, port=self.port,
                database=self.database,
                user=self.user, password=self.password,
            )
            await self._listen_conn.add_listener(
                'phonebox_storage_change',
                self._on_storage_notify,
            )
            logger.info(
                "[AsyncDB] Opt #25: LISTEN active on 'phonebox_storage_change' — "
                "pid cache auto-invalidates on DB changes"
            )
        except Exception as exc:
            logger.warning(
                f"[AsyncDB] Opt #25: LISTEN setup failed ({exc}) "
                "— manual invalidation fallback active"
            )
            self._listen_conn = None

    def _on_storage_notify(self, conn, pid, channel, payload: str) -> None:
        """
        Callback from asyncpg when PostgreSQL sends NOTIFY.
        Runs in the asyncpg event-loop thread; dict.pop is GIL-safe.
        """
        try:
            lid = int(payload)
            self.invalidate_pid_cache_sync(lid)
            logger.debug(f"[AsyncDB] Opt #25: cache invalidated via NOTIFY LID={lid}")
        except (ValueError, TypeError):
            self.invalidate_pid_cache_sync()   # full clear on bad payload
            logger.warning(
                f"[AsyncDB] Opt #25: unexpected NOTIFY payload {payload!r} — "
                "full cache cleared"
            )

    # ── Opt #36: pgvector detection ───────────────────────────────────────────

    async def _detect_pgvector(self) -> bool:
        try:
            row = await self._pool.fetchrow("""
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'slot_baselines'
                  AND column_name = 'embedding_vec'
                LIMIT 1
            """)
            return row is not None
        except Exception as exc:
            logger.warning(f"[AsyncDB] pgvector detection failed: {exc}")
            return False

    # ── Baseline operations ───────────────────────────────────────────────────

    async def save_baseline(self, lid: int, embedding: np.ndarray):
        """Opt #36: write BYTEA + vector(96) with graceful BYTEA fallback."""
        self._require_pool()
        emb_bytes = embedding_to_bytes(embedding)

        if self._pgvector_ready:
            try:
                await self._pool.execute(
                    """
                    INSERT INTO slot_baselines
                        (lid, embedding, embedding_vec, calibrated_at)
                    VALUES ($1, $2, $3::vector(96), NOW())
                    ON CONFLICT (lid) DO UPDATE SET
                        embedding     = EXCLUDED.embedding,
                        embedding_vec = EXCLUDED.embedding_vec,
                        calibrated_at = EXCLUDED.calibrated_at
                    """,
                    lid, emb_bytes, _vec_str(embedding),
                )
                return
            except Exception:
                pass   # fall through to BYTEA-only

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
        """Opt #36: batch write BYTEA + vector(96) with graceful fallback."""
        self._require_pool()
        if not baselines:
            return

        data_vec = [
            (lid, embedding_to_bytes(emb), _vec_str(emb))
            for lid, emb in baselines.items()
        ]
        data_bytes = [(lid, b) for lid, b, _ in data_vec]

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                if self._pgvector_ready:
                    try:
                        await conn.executemany(
                            """
                            INSERT INTO slot_baselines
                                (lid, embedding, embedding_vec, calibrated_at)
                            VALUES ($1, $2, $3::vector(96), NOW())
                            ON CONFLICT (lid) DO UPDATE SET
                                embedding     = EXCLUDED.embedding,
                                embedding_vec = EXCLUDED.embedding_vec,
                                calibrated_at = EXCLUDED.calibrated_at
                            """,
                            data_vec,
                        )
                        logger.info(
                            f"Saved {len(baselines)} baselines batch "
                            f"(async, vector=True)"
                        )
                        return
                    except Exception:
                        pass   # fall through

                await conn.executemany(
                    """
                    INSERT INTO slot_baselines (lid, embedding, calibrated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (lid) DO UPDATE SET
                        embedding     = EXCLUDED.embedding,
                        calibrated_at = EXCLUDED.calibrated_at
                    """,
                    data_bytes,
                )
        logger.info(f"Saved {len(baselines)} baselines batch (async, vector=False)")

    # ── Occupancy queries ─────────────────────────────────────────────────────

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
            "SELECT lid, pid FROM phone_storage WHERE retrieved_at IS NULL ORDER BY lid"
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
                "SELECT pid FROM phone_storage WHERE lid=$1 AND retrieved_at IS NULL",
                lid,
            )
            value = row["pid"] if row else None
            self._pid_cache[lid] = value
            return value

    async def get_lid_for_pid(self, pid: str) -> Optional[int]:
        self._require_pool()
        row = await self._pool.fetchrow(
            "SELECT lid FROM phone_storage WHERE pid=$1 AND retrieved_at IS NULL LIMIT 1",
            pid,
        )
        return row["lid"] if row else None

    async def is_slot_occupied(self, lid: int) -> bool:
        self._require_pool()
        return await self._pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM phone_storage WHERE lid=$1 AND retrieved_at IS NULL)",
            lid,
        )

    async def update_storage_lid(self, pid: str, new_lid: int) -> bool:
        self._require_pool()
        try:
            await self._pool.execute(
                "UPDATE phone_storage SET lid=$1 WHERE pid=$2 AND retrieved_at IS NULL",
                new_lid, pid,
            )
            await self._invalidate_cache(new_lid)
            return True
        except Exception as e:
            logger.error(f"Failed to update storage LID for PID={pid}: {e}")
            return False

    # ── Cache management ──────────────────────────────────────────────────────

    def invalidate_pid_cache_sync(self, lid: Optional[int] = None):
        """Thread-safe (CPython GIL). Called from _on_storage_notify callback."""
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

    # ── Health check ──────────────────────────────────────────────────────────

    async def is_healthy(self) -> bool:
        if self._pool is None:
            return False
        try:
            await self._pool.fetchval("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
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