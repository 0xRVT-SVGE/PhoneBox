import logging
import numpy as np
from typing import List, Tuple, Optional
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)


class SlotMonitorDB:
    """
    Pure DB interface for slot monitoring.
    No business logic - just CRUD operations.
    """

    # ------------------------------------------------------------
    # BASELINE MANAGEMENT
    # ------------------------------------------------------------

    @staticmethod
    def fetch_occupied_slots() -> List[Tuple[int, int]]:
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
    def fetch_all_baselines() -> dict[int, np.ndarray]:
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
                        baselines[lid] = _embedding_from_bytes(emb_bytes)

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
            emb_bytes = _embedding_to_bytes(embedding)

            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_baselines (lid, embedding)
                            VALUES (%s, %s)
                            ON CONFLICT (lid) 
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
    def get_pid_for_lid(lid: int) -> Optional[int]:
        """Get phone ID for a given location."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT pid
                            FROM phone_storage
                            WHERE lid = %s
                              AND retrieved_at IS NULL
                            LIMIT 1;
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

    # ------------------------------------------------------------
    # ANOMALY LOGGING
    # ------------------------------------------------------------


# ------------------------------------------------------------
# EMBEDDING SERIALIZATION HELPERS
# ------------------------------------------------------------

def _embedding_to_bytes(emb: np.ndarray) -> bytes:
    """Convert numpy embedding to bytes for DB storage."""
    return emb.astype(np.float32).tobytes()


def _embedding_from_bytes(data: bytes) -> np.ndarray:
    """Convert bytes from DB to numpy embedding."""
    return np.frombuffer(data, dtype=np.float32)