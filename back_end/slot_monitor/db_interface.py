# ============================================================
# FILE: server/slot_monitor/db_interface.py
# ============================================================

import logging
from typing import Dict, Optional, List
import numpy as np
from back_end.Database.db import get_conn, put_conn
from .slot_embed import embedding_to_bytes, embedding_from_bytes

logger = logging.getLogger(__name__)


class SlotMonitorDB:
    """Database interface for slot monitoring system"""

    @staticmethod
    def load_baselines() -> Dict[int, np.ndarray]:
        """Load all baselines from database"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT lid, embedding
                            FROM slot_baselines
                            WHERE embedding IS NOT NULL;
                            """)
                rows = cur.fetchall()

                baselines = {}
                for lid, emb_bytes in rows:
                    if emb_bytes:
                        baselines[lid] = embedding_from_bytes(emb_bytes)

                logger.info(f"Loaded {len(baselines)} baselines from database")
                return baselines
        finally:
            put_conn(conn)

    @staticmethod
    def save_baseline(lid: int, embedding: np.ndarray, reason: str = 'auto_adapt'):
        """Save or update baseline for a slot"""
        conn = get_conn()
        try:
            emb_bytes = embedding_to_bytes(embedding)

            with conn.cursor() as cur:
                # Update baseline
                cur.execute("""
                            INSERT INTO slot_baselines (lid, embedding, needs_recalculation, updated_at)
                            VALUES (%s, %s, FALSE, NOW()) ON CONFLICT (lid)
                    DO
                            UPDATE SET
                                embedding = EXCLUDED.embedding,
                                needs_recalculation = FALSE,
                                updated_at = NOW();
                            """, (lid, emb_bytes))

                # Log to history
                cur.execute("""
                            INSERT INTO slot_baseline_history (lid, embedding, reason, created_at)
                            VALUES (%s, %s, %s, NOW());
                            """, (lid, emb_bytes, reason))

                conn.commit()
                logger.info(f"Saved baseline for slot {lid} (reason: {reason})")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to save baseline for slot {lid}: {e}")
        finally:
            put_conn(conn)

    @staticmethod
    def update_slot_state(lid: int, binary_state: str, tx_state: str, distance: float):
        """Update current slot state"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_current_state
                            (lid, binary_state, tx_state, last_distance, last_change_ts, updated_at)
                            VALUES (%s, %s, %s, %s, NOW(), NOW()) ON CONFLICT (lid)
                    DO
                            UPDATE SET
                                binary_state = EXCLUDED.binary_state,
                                tx_state = EXCLUDED.tx_state,
                                last_distance = EXCLUDED.last_distance,
                                last_change_ts = CASE
                                WHEN slot_current_state.binary_state != EXCLUDED.binary_state
                                THEN NOW()
                                ELSE slot_current_state.last_change_ts
                            END
                            ,
                        updated_at = NOW();
                            """, (lid, binary_state, tx_state, distance))
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to update slot state for {lid}: {e}")
        finally:
            put_conn(conn)

    @staticmethod
    def log_slot_event(lid: int, binary_state: str, tx_state: str, distance: float):
        """Log slot state change event"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_events (lid, binary_state, tx_state, distance, timestamp)
                            VALUES (%s, %s, %s, %s, NOW());
                            """, (lid, binary_state, tx_state, distance))
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to log event for slot {lid}: {e}")
        finally:
            put_conn(conn)

    @staticmethod
    def log_anomaly(lid: int, anomaly_type: str, distance: float,
                    severity: str = 'medium', description: str = None):
        """Log detected anomaly"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_anomalies
                                (lid, anomaly_type, distance, severity, description, timestamp)
                            VALUES (%s, %s, %s, %s, %s, NOW());
                            """, (lid, anomaly_type, distance, severity, description))
                conn.commit()
                logger.warning(f"Anomaly logged: slot {lid}, type {anomaly_type}, severity {severity}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to log anomaly for slot {lid}: {e}")
        finally:
            put_conn(conn)

    @staticmethod
    def log_system_error(error_type: str, description: str, severity: str = 'error'):
        """Log system-level error"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            INSERT INTO slot_system_errors
                                (error_type, error_count, severity, description, timestamp)
                            VALUES (%s, 1, %s, %s, NOW());
                            """, (error_type, severity, description))
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to log system error: {e}")
        finally:
            put_conn(conn)