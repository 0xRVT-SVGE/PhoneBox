# ============================================================
# FILE: server/slot_monitor/db_interface.py
# ============================================================
"""
Unified database interface for slot monitoring.
Supports both sync (for legacy/calibration) and async (for monitoring) operations.

USAGE:
- Sync methods: For calibration, one-time operations, legacy code
- Async methods: For real-time monitoring (high performance)
"""

import asyncio
import logging
import numpy as np
from typing import Dict, List, Tuple, Optional
import asyncpg
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)


# ============================================================
# EMBEDDING SERIALIZATION (used by both sync and async)
# ============================================================

def embedding_to_bytes(emb: np.ndarray) -> bytes:
    """Convert numpy embedding to bytes for DB storage."""
    return emb.astype(np.float32).tobytes()


def embedding_from_bytes(data: bytes) -> np.ndarray:
    """Convert bytes from DB to numpy embedding."""
    return np.frombuffer(data, dtype=np.float32)


# ============================================================
# SYNC DATABASE INTERFACE (Legacy/Calibration)
# ============================================================

class SlotMonitorDB:
    """
    Synchronous DB interface for slot monitoring.

    USE FOR:
    - Calibration scripts (calibrate_all_slots.py)
    - One-time operations (setup, testing)
    - Legacy code that can't use async

    PERFORMANCE: Blocking I/O (fine for non-realtime operations)
    """

    # ------------------------------------------------------------
    # BASELINE MANAGEMENT
    # ------------------------------------------------------------

    @staticmethod
    def fetch_occupied_slots() -> List[Tuple[int, str]]:
        """
        Fetch all currently occupied slots.

        Returns:
            List of (lid, pid) tuples
        """
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
        Fetch ALL slot baselines (occupied and empty slots).

        Returns:
            Dict mapping lid -> baseline_embedding
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
        """Save or update baseline embedding for a slot."""
        conn = get_conn()
        try:
            emb_bytes = embedding_to_bytes(embedding)

            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_baselines (lid, embedding)
                            VALUES (%s, %s) ON CONFLICT (lid)
                            DO UPDATE SET
                                embedding = EXCLUDED.embedding,
                                calibrated_at = NOW()
                            """, (lid, emb_bytes))
                conn.commit()
                logger.debug(f"Saved baseline for slot {lid}")

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to save baseline for slot {lid}: {e}")
        finally:
            put_conn(conn)

    # ------------------------------------------------------------
    # SLOT STATE QUERIES
    # ------------------------------------------------------------

    @staticmethod
    def get_pid_for_lid(lid: int) -> Optional[str]:
        """Get phone ID for a given location."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT pid
                            FROM phone_storage
                            WHERE lid = %s
                              AND retrieved_at IS NULL LIMIT 1;
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
        """Check if a slot is currently occupied."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE lid = %s
                                            AND retrieved_at IS NULL);
                            """, (lid,))
                return cur.fetchone()[0]

        except Exception as e:
            logger.error(f"Failed to check occupancy for slot {lid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def count_stored_phones() -> Optional[int]:
        """
        Return the number of phones currently in storage (retrieved_at IS NULL).
        Used by AdminSessionContext for the phone count invariant check.
        Returns None on DB error.
        """
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
    def pid_exists(pid: int) -> bool:
        """
        Check if a phone PID exists in the database.

        Args:
            pid: Phone ID

        Returns:
            True if phone exists, False otherwise
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM phones WHERE pid = %s);",
                    (pid,)
                )
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to check PID existence for {pid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def is_phone_stored(pid: int) -> bool:
        """
        Check if a phone is currently in storage (not yet retrieved).

        Args:
            pid: Phone ID

        Returns:
            True if the phone has an active storage record
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE pid = %s
                                            AND retrieved_at IS NULL);
                            """, (pid,))
                return cur.fetchone()[0]
        except Exception as e:
            logger.error(f"Failed to check storage status for PID {pid}: {e}")
            return False
        finally:
            put_conn(conn)

    @staticmethod
    def get_next_free_lid() -> Optional[int]:
        """
        Return the next available (free) LID for phone storage.

        Returns:
            lid if a free slot exists, None if storage is full
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT l.lid
                            FROM locations l
                                     LEFT JOIN phone_storage ps
                                               ON l.lid = ps.lid
                                                   AND ps.retrieved_at IS NULL
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
    def get_lid_for_pid(pid: int) -> Optional[int]:
        """
        Get the current storage location for a phone.

        Args:
            pid: Phone ID

        Returns:
            lid if phone is currently stored, None otherwise
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT lid
                            FROM phone_storage
                            WHERE pid = %s
                              AND retrieved_at IS NULL
                            LIMIT 1;
                            """, (pid,))
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get LID for PID {pid}: {e}")
            return None
        finally:
            put_conn(conn)

    @staticmethod
    def update_storage_lid(pid: int, new_lid: int) -> bool:
        """
        Update the storage location for a phone (used during verification).

        Args:
            pid: Phone ID
            new_lid: New location ID

        Returns:
            True on success, False on failure
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            UPDATE phone_storage
                            SET lid = %s
                            WHERE pid = %s
                              AND retrieved_at IS NULL;
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


# ============================================================
# ASYNC DATABASE INTERFACE (Real-time Monitoring)
# ============================================================

class AsyncSlotMonitorDB:
    """
    Async database interface for slot monitoring.

    USE FOR:
    - Real-time monitoring (camera_test_async.py)
    - High-performance operations (500+ slots)
    - Event-driven workers

    PERFORMANCE:
    - 3-5x faster than sync (non-blocking I/O)
    - Connection pooling (5-20 persistent connections)
    - Prepared statements (automatic optimization)
    - Batch operations (10x faster for bulk updates)
    """

    def __init__(
            self,
            host: str = "localhost",
            port: int = 5432,
            database: str = "phone_monitor",
            user: str = "postgres",
            password: str = "postgres",
            min_pool_size: int = 5,
            max_pool_size: int = 20,
    ):
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size

        self._pool: Optional[asyncpg.Pool] = None

        # Internal cache - not exposed to callers
        self._pid_cache: Dict[int, str] = {}
        self._cache_lock = asyncio.Lock()

    async def connect(self):
        """
        Create connection pool.
        MUST be called before using any async DB methods.
        """
        if self._pool is not None:
            logger.warning("Connection pool already exists")
            return

        self._pool = await asyncpg.create_pool(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user,
            password=self.password,
            min_size=self.min_pool_size,
            max_size=self.max_pool_size,
            command_timeout=10.0,
        )

        logger.info(
            f"AsyncPG pool created: {self.min_pool_size}-{self.max_pool_size} "
            f"connections to {self.host}:{self.port}/{self.database}"
        )

    async def close(self):
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("AsyncPG pool closed")

    def _require_pool(self):
        if self._pool is None:
            raise RuntimeError("Database not connected. Call connect() first.")

    # ------------------------------------------------------------
    # BASELINE OPERATIONS
    # ------------------------------------------------------------

    async def save_baseline(self, lid: int, embedding: np.ndarray):
        """Save baseline embedding for a slot."""
        self._require_pool()
        emb_bytes = embedding_to_bytes(embedding)
        await self._pool.execute(
            """
            INSERT INTO slot_baselines (lid, embedding, calibrated_at)
            VALUES ($1, $2, NOW()) ON CONFLICT (lid) DO UPDATE
                SET embedding     = EXCLUDED.embedding,
                    calibrated_at = EXCLUDED.calibrated_at
            """,
            lid, emb_bytes
        )

    async def fetch_baseline(self, lid: int) -> Optional[np.ndarray]:
        """Fetch baseline for a single slot."""
        self._require_pool()
        row = await self._pool.fetchrow(
            "SELECT embedding FROM slot_baselines WHERE lid = $1",
            lid
        )
        return embedding_from_bytes(row["embedding"]) if row else None

    async def fetch_all_baselines(self) -> Dict[int, np.ndarray]:
        """Fetch all baselines (used during initialization)."""
        self._require_pool()
        rows = await self._pool.fetch(
            """
            SELECT lid, embedding
            FROM slot_baselines
            WHERE embedding IS NOT NULL
              AND embedding != '\\x00'::bytea
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
        """Delete baseline for a slot."""
        self._require_pool()
        await self._pool.execute(
            "DELETE FROM slot_baselines WHERE lid = $1",
            lid
        )

    async def save_baselines_batch(self, baselines: Dict[int, np.ndarray]):
        """
        Save multiple baselines in a single transaction.
        10x faster than individual saves for bulk operations.
        """
        self._require_pool()
        if not baselines:
            return

        data = [
            (lid, embedding_to_bytes(emb))
            for lid, emb in baselines.items()
        ]

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    """
                    INSERT INTO slot_baselines (lid, embedding, calibrated_at)
                    VALUES ($1, $2, NOW()) ON CONFLICT (lid) DO UPDATE
                        SET embedding     = EXCLUDED.embedding,
                            calibrated_at = EXCLUDED.calibrated_at
                    """,
                    data
                )

        logger.info(f"Saved {len(baselines)} baselines in batch (async)")

    # ------------------------------------------------------------
    # OCCUPANCY QUERIES
    # ------------------------------------------------------------

    async def get_num_lid(self) -> int:
        """Return the number of available lids (max lid + 1)."""
        self._require_pool()
        num_lid = await self._pool.fetchval(
            "SELECT COALESCE(MAX(lid), 0) + 1 FROM locations;"
        )
        return num_lid or 1

    async def get_next_free_lid(self) -> Optional[int]:
        """Return the next available (free) LID for phone storage."""
        self._require_pool()
        return await self._pool.fetchval(
            """
            SELECT l.lid
            FROM locations l
            WHERE NOT EXISTS (
                SELECT 1 FROM phone_storage ps
                WHERE ps.lid = l.lid
                  AND ps.retrieved_at IS NULL
            )
            ORDER BY l.lid
            FOR UPDATE SKIP LOCKED
            LIMIT 1;
            """
        )

    async def fetch_occupied_slots(self) -> List[Tuple[int, str]]:
        """Fetch all currently occupied slots."""
        self._require_pool()
        rows = await self._pool.fetch(
            """
            SELECT lid, pid
            FROM phone_storage
            WHERE retrieved_at IS NULL
            ORDER BY lid
            """
        )
        result = [(row["lid"], row["pid"]) for row in rows]
        logger.info(f"Fetched {len(result)} occupied slots from DB (async)")
        return result

    async def get_pid_for_lid(self, lid: int) -> Optional[str]:
        """
        Get phone ID for a location (with internal caching).
        Cache is invalidated automatically after mutating operations.
        """
        async with self._cache_lock:
            if lid in self._pid_cache:
                return self._pid_cache[lid]

        self._require_pool()
        row = await self._pool.fetchrow(
            """
            SELECT pid
            FROM phone_storage
            WHERE lid = $1
              AND retrieved_at IS NULL
            """,
            lid
        )

        async with self._cache_lock:
            value = row["pid"] if row else None
            self._pid_cache[lid] = value
            return value

    async def get_lid_for_pid(self, pid: str) -> Optional[int]:
        """Get location ID for a stored phone."""
        self._require_pool()
        row = await self._pool.fetchrow(
            """
            SELECT lid
            FROM phone_storage
            WHERE pid = $1
              AND retrieved_at IS NULL
            LIMIT 1
            """,
            pid
        )
        return row["lid"] if row else None

    async def is_slot_occupied(self, lid: int) -> bool:
        """Check if a slot is currently occupied."""
        self._require_pool()
        return await self._pool.fetchval(
            """
            SELECT EXISTS(SELECT 1
                          FROM phone_storage
                          WHERE lid = $1
                            AND retrieved_at IS NULL)
            """,
            lid
        )

    async def update_storage_lid(self, pid: str, new_lid: int) -> bool:
        """
        Update the storage location for a phone (used during verification).

        Returns:
            True on success, False on failure
        """
        self._require_pool()
        try:
            await self._pool.execute(
                """
                UPDATE phone_storage
                SET lid = $1
                WHERE pid = $2
                  AND retrieved_at IS NULL
                """,
                new_lid, pid
            )
            await self._invalidate_cache(new_lid)
            return True
        except Exception as e:
            logger.error(f"Failed to update storage LID for PID={pid}: {e}")
            return False

    # ------------------------------------------------------------
    # INTERNAL CACHE MANAGEMENT
    # ------------------------------------------------------------

    async def _invalidate_cache(self, lid: Optional[int] = None):
        """
        Invalidate PID cache after mutating operations.
        Called internally — callers do not need to manage this.
        """
        async with self._cache_lock:
            if lid is not None:
                self._pid_cache.pop(lid, None)
            else:
                self._pid_cache.clear()

    # ------------------------------------------------------------
    # HEALTH CHECK
    # ------------------------------------------------------------

    async def is_healthy(self) -> bool:
        """
        Test database connection.

        Returns:
            True if connection is healthy
        """
        if self._pool is None:
            return False
        try:
            await self._pool.fetchval("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"Database connection test failed: {e}")
            return False


# ============================================================
# ASYNC CONTEXT MANAGER (Convenience)
# ============================================================

class AsyncDBContext:
    """
    Async context manager for database lifecycle.

    Usage:
        async with AsyncDBContext() as db:
            baseline = await db.fetch_baseline(lid)
            await db.save_baseline(lid, new_baseline)
    """

    def __init__(self, **kwargs):
        self.db = AsyncSlotMonitorDB(**kwargs)

    async def __aenter__(self):
        await self.db.connect()
        return self.db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.db.close()


# ============================================================
# USAGE EXAMPLES
# ============================================================

"""
SYNC USAGE (Calibration, legacy code):

    from db_interface import SlotMonitorDB

    baselines = SlotMonitorDB.fetch_all_baselines()
    SlotMonitorDB.save_baseline(lid=0, embedding=emb)
    occupied  = SlotMonitorDB.fetch_occupied_slots()
    lid       = SlotMonitorDB.get_next_free_lid()
    stored    = SlotMonitorDB.is_phone_stored(pid)
    lid       = SlotMonitorDB.get_lid_for_pid(pid)
    ok        = SlotMonitorDB.update_storage_lid(pid, new_lid)


ASYNC USAGE (Real-time monitoring):

    from db_interface import AsyncSlotMonitorDB

    db = AsyncSlotMonitorDB(host="localhost", database="phone_monitor")
    await db.connect()

    baselines = await db.fetch_all_baselines()
    await db.save_baseline(lid=0, embedding=emb)
    occupied  = await db.fetch_occupied_slots()
    healthy   = await db.is_healthy()

    await db.close()


ASYNC WITH CONTEXT MANAGER (Recommended):

    from db_interface import AsyncDBContext

    async with AsyncDBContext() as db:
        baselines = await db.fetch_all_baselines()
        await db.save_baseline(lid=0, embedding=emb)
    # Auto-closes connection
"""