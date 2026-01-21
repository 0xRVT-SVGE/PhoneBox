import logging
import time
from typing import Dict, Optional
import numpy as np
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)


class SlotOperations:
    """
    Handle deposit/withdrawal operations.
    Coordinates between DB, monitor, and embedder.
    """

    def __init__(self, monitor=None, embedder=None):
        """
        Args:
            monitor: SlotMonitor instance
            embedder: Embedder instance for computing baselines
        """
        self.monitor = monitor
        self.embedder = embedder
        logger.info("SlotOperations initialized")

    def set_monitor(self, monitor):
        """Set monitor reference after initialization."""
        self.monitor = monitor
        logger.info("Monitor attached to SlotOperations")

    def set_embedder(self, embedder):
        """Set embedder reference after initialization."""
        self.embedder = embedder
        logger.info("Embedder attached to SlotOperations")

    # ------------------------------------------------------------
    # DEPOSIT OPERATION
    # ------------------------------------------------------------

    def deposit_phone(
            self,
            pid: int,
            lid: int,
            wait_for_stable: float = 3.0
    ) -> Dict:
        """
        Deposit a phone into a storage location.

        Args:
            pid: Phone ID
            lid: Location ID
            wait_for_stable: Seconds to wait after placing phone

        Returns:
            {"status": "success" | "error", "message": str, ...}
        """
        if not self.monitor or not self.embedder:
            return {
                "status": "error",
                "message": "Monitor or embedder not available"
            }

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Verify phone exists and is not stored
                cur.execute("""
                            SELECT EXISTS(SELECT 1
                                          FROM phones
                                          WHERE pid = %s),
                                   EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE pid = %s
                                            AND retrieved_at IS NULL);
                            """, (pid, pid))

                phone_exists, already_stored = cur.fetchone()

                if not phone_exists:
                    return {"status": "error", "message": "Phone not found"}

                if already_stored:
                    return {"status": "error", "message": "Phone already in storage"}

                # Verify location is empty
                cur.execute("""
                            SELECT EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE lid = %s
                                            AND retrieved_at IS NULL);
                            """, (lid,))

                if cur.fetchone()[0]:
                    return {
                        "status": "error",
                        "message": f"Location {lid} already occupied"
                    }

                logger.info(f"Starting deposit: PID={pid} at LID={lid}")

                # Pause monitoring
                self.monitor.pause_slot(lid)

                # Wait for operator to place phone and remove hand
                logger.info(f"Waiting {wait_for_stable}s for stabilization...")
                time.sleep(wait_for_stable)

                # Capture new baseline
                baseline_emb = self._capture_baseline(lid)
                if baseline_emb is None:
                    self.monitor.resume_slot(lid, np.zeros(512), False)  # Resume empty
                    return {
                        "status": "error",
                        "message": "Failed to capture baseline"
                    }

                # Create storage record
                cur.execute("""
                            INSERT INTO phone_storage (pid, lid, stored_at)
                            VALUES (%s, %s, NOW())
                            RETURNING id;
                            """, (pid, lid))

                storage_id = cur.fetchone()[0]
                conn.commit()

                # Resume monitoring with new baseline (occupied)
                self.monitor.resume_slot(lid, baseline_emb, is_occupied=True)

                logger.info(f"Phone {pid} deposited successfully at location {lid}")
                return {
                    "status": "success",
                    "message": "Phone deposited successfully",
                    "pid": pid,
                    "lid": lid,
                    "storage_id": storage_id
                }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to deposit phone {pid}: {e}")
            if 'lid' in locals():
                # Resume monitoring in error case
                self.monitor.resume_slot(lid, np.zeros(512), False)
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    # ------------------------------------------------------------
    # WITHDRAWAL OPERATION
    # ------------------------------------------------------------

    def withdraw_phone(
            self,
            pid: int,
            wait_for_removal: float = 3.0
    ) -> Dict:
        """
        Withdraw a phone from storage.

        Args:
            pid: Phone ID
            wait_for_removal: Seconds to wait after removing phone

        Returns:
            {"status": "success" | "error", "message": str, ...}
        """
        if not self.monitor or not self.embedder:
            return {
                "status": "error",
                "message": "Monitor or embedder not available"
            }

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Get active storage record
                cur.execute("""
                            SELECT ps.id, ps.lid
                            FROM phone_storage ps
                            WHERE ps.pid = %s
                              AND ps.retrieved_at IS NULL;
                            """, (pid,))

                result = cur.fetchone()

                if not result:
                    return {
                        "status": "error",
                        "message": "Phone not in storage"
                    }

                storage_id, lid = result

                logger.info(f"Starting withdrawal: PID={pid} from LID={lid}")

                # Pause monitoring
                self.monitor.pause_slot(lid)

                # Wait for operator to remove phone and hand
                logger.info(f"Waiting {wait_for_removal}s for removal...")
                time.sleep(wait_for_removal)

                # Capture new baseline (empty slot)
                baseline_emb = self._capture_baseline(lid)
                if baseline_emb is None:
                    self.monitor.resume_slot(lid, np.zeros(512), True)  # Resume as occupied
                    return {
                        "status": "error",
                        "message": "Failed to capture baseline"
                    }

                # Update storage record
                cur.execute("""
                            UPDATE phone_storage
                            SET retrieved_at = NOW()
                            WHERE id = %s;
                            """, (storage_id,))

                conn.commit()

                # Remove from monitoring (slot now empty)
                self.monitor.remove_slot(lid)

                logger.info(f"Phone {pid} withdrawn successfully from location {lid}")
                return {
                    "status": "success",
                    "message": "Phone withdrawn successfully",
                    "pid": pid,
                    "lid": lid,
                    "storage_id": storage_id
                }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to withdraw phone {pid}: {e}")
            if 'lid' in locals():
                # Resume monitoring in error case
                self.monitor.resume_slot(lid, np.zeros(512), True)
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    # ------------------------------------------------------------
    # HELPER METHODS
    # ------------------------------------------------------------

    def _capture_baseline(self, lid: int) -> Optional[np.ndarray]:
        """
        Capture stable baseline embedding for a slot.

        Takes multiple samples and averages them.
        """
        try:
            embeddings = []

            for i in range(3):
                emb = self.embedder.compute(lid)
                embeddings.append(emb)

                if i < 2:  # Don't sleep after last capture
                    time.sleep(0.2)

            # Average and normalize
            avg_emb = np.mean(embeddings, axis=0)
            norm = np.linalg.norm(avg_emb)

            if norm > 1e-8:
                avg_emb = avg_emb / norm

            logger.debug(f"Captured baseline for slot {lid}")
            return avg_emb.astype(np.float32)

        except Exception as e:
            logger.error(f"Failed to capture baseline for slot {lid}: {e}")
            return None

    # ------------------------------------------------------------
    # STATUS QUERIES
    # ------------------------------------------------------------

    def get_slot_status(self, lid: int) -> Dict:
        """Get current status of a slot."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT l.lid,
                                   ps.pid,
                                   p.imei,
                                   p.model,
                                   ps.stored_at,
                                   ps.retrieved_at
                            FROM locations l
                                     LEFT JOIN phone_storage ps ON l.lid = ps.lid
                                AND ps.retrieved_at IS NULL
                                     LEFT JOIN phones p ON ps.pid = p.pid
                            WHERE l.lid = %s;
                            """, (lid,))

                row = cur.fetchone()

                if not row:
                    return {"status": "error", "message": "Location not found"}

                lid, pid, imei, model, stored_at, retrieved_at = row

                # Get monitoring state if slot is being monitored
                monitor_state = None
                if self.monitor and lid in self.monitor.slots:
                    slot = self.monitor.slots[lid]
                    monitor_state = {
                        "last_distance": slot.last_dist,
                        "mismatch": slot.mismatch,
                        "is_occupied": slot.is_occupied
                    }

                return {
                    "status": "success",
                    "lid": lid,
                    "occupied": pid is not None,
                    "phone": {
                        "pid": pid,
                        "imei": imei,
                        "model": model,
                        "stored_at": stored_at.isoformat() if stored_at else None
                    } if pid else None,
                    "monitoring": monitor_state
                }

        except Exception as e:
            logger.error(f"Error fetching slot {lid} status: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def get_empty_locations(self, limit: int = 10) -> Dict:
        """Get available empty locations."""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT l.lid, l.x, l.y
                            FROM locations l
                                     LEFT JOIN phone_storage ps ON l.lid = ps.lid
                                AND ps.retrieved_at IS NULL
                            WHERE ps.pid IS NULL
                            ORDER BY l.lid
                            LIMIT %s;
                            """, (limit,))

                rows = cur.fetchall()

                locations = [
                    {"lid": lid, "x": x, "y": y}
                    for lid, x, y in rows
                ]

                return {
                    "status": "success",
                    "locations": locations,
                    "count": len(locations)
                }

        except Exception as e:
            logger.error(f"Error fetching empty locations: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)