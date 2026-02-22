# ============================================================
# FILE: back_end/slot_monitor/slot_operations.py
# ============================================================
"""
Slot operations - DATABASE MUTATIONS ONLY.

Methods:
- deposit_phone_db():          Create storage record
- withdraw_phone_db():         Mark phone as retrieved
- capture_and_save_baseline(): Capture frame, compute embedding, persist to DB,
                                update in-memory slot state.
                                Callers own pause/resume — this method does NOT
                                touch the worker's paused_slots set.
"""

import logging
import time
from typing import Dict, Optional
import numpy as np
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)


class SlotOperations:
    """
    Handle deposit/withdrawal DATABASE operations.
    Coordinates between DB, monitor frame buffer, and slot map.

    IMPORTANT: This class does NOT handle:
    - QR scanning (see qr_pid_reader.py)
    - PID validation (see qr_pid_reader.py)
    - WebSocket events (see ops_handler.py)
    - Slot pause/resume lifecycle (see ops_handler.py)
    """

    def __init__(self, monitor=None):
        """
        Args:
            monitor: HeadlessSlotMonitor instance (provides worker_pool + frame_buffer)
        """
        self.monitor = monitor
        logger.info("SlotOperations initialized")

    def set_monitor(self, monitor):
        """Set monitor reference after initialization."""
        self.monitor = monitor
        logger.info("Monitor attached to SlotOperations")

    # ------------------------------------------------------------
    # DEPOSIT OPERATION (DB ONLY)
    # ------------------------------------------------------------

    def deposit_phone_db(self, pid: str, lid: int) -> Dict:
        """
        Create database record for phone deposit.

        ASSUMES:
        - PID is already validated
        - Slot is already verified empty
        - Phone is already physically placed
        - Baseline will be captured separately via capture_and_save_baseline()

        Args:
            pid: Phone ID (already validated, str)
            lid: Location ID (already verified empty)

        Returns:
            {"status": "success" | "error", "message": str, "storage_id": int}
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT EXISTS(SELECT 1 FROM phones WHERE pid = %s),
                                   EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE pid = %s
                                            AND retrieved_at IS NULL),
                                   EXISTS(SELECT 1
                                          FROM phone_storage
                                          WHERE lid = %s
                                            AND retrieved_at IS NULL);
                            """, (pid, pid, lid))

                phone_exists, already_stored, slot_occupied = cur.fetchone()

                if not phone_exists:
                    return {"status": "error", "message": "Phone not found in database"}

                if already_stored:
                    return {"status": "error", "message": "Phone already in storage"}

                if slot_occupied:
                    return {"status": "error", "message": f"Location {lid} already occupied"}

                cur.execute("""
                            INSERT INTO phone_storage (pid, lid, stored_at)
                            VALUES (%s, %s, NOW()) RETURNING id;
                            """, (pid, lid))

                storage_id = cur.fetchone()[0]
                conn.commit()

                logger.info(f"Deposit DB record created: PID={pid}, LID={lid}, storage_id={storage_id}")
                return {
                    "status": "success",
                    "message": "Deposit recorded successfully",
                    "pid": pid,
                    "lid": lid,
                    "storage_id": storage_id
                }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to create deposit record for PID {pid}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    # ------------------------------------------------------------
    # WITHDRAWAL OPERATION (DB ONLY)
    # ------------------------------------------------------------

    def withdraw_phone_db(self, pid: str) -> Dict:
        """
        Mark phone as retrieved in database.

        ASSUMES:
        - PID is already validated
        - Phone is already physically removed
        - Baseline will be recaptured separately via capture_and_save_baseline()

        Args:
            pid: Phone ID (already validated)

        Returns:
            {"status": "success" | "error", "message": str, "lid": int}
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
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

                # Update storage record
                cur.execute("""
                            UPDATE phone_storage
                            SET retrieved_at = NOW()
                            WHERE id = %s;
                            """, (storage_id,))

                conn.commit()

                logger.info(f"Withdrawal DB record updated: PID={pid}, LID={lid}, storage_id={storage_id}")
                return {
                    "status": "success",
                    "message": "Withdrawal recorded successfully",
                    "pid": pid,
                    "lid": lid,
                    "storage_id": storage_id
                }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to update withdrawal record for PID {pid}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    # ------------------------------------------------------------
    # BASELINE MANAGEMENT
    # ------------------------------------------------------------

    def capture_and_save_baseline(
            self,
            lid: int,
            is_occupied: bool,
            wait_for_stable: float = 3.0
    ) -> Dict:
        """
        Capture a new baseline for a slot, persist it to DB, and update the
        in-memory slot state (baseline embedding + is_occupied).

        IMPORTANT: This method does NOT pause or resume the slot.
        The caller (ops_handler) owns the pause/resume lifecycle and must
        ensure the slot is already paused before calling this.

        Args:
            lid:            Location ID
            is_occupied:    True if phone was just deposited, False if just removed
            wait_for_stable: Seconds to wait before capturing (lets vibrations settle)

        Returns:
            {"status": "success" | "error", "message": str, "baseline": np.ndarray | None}
        """
        if not self.monitor:
            return {"status": "error", "message": "Monitor not available"}

        worker = self.monitor.worker_pool._get_worker(lid)
        if worker is None:
            return {"status": "error", "message": f"No worker found for slot {lid}"}

        slot = worker._slot_map.get(lid)
        if slot is None:
            return {"status": "error", "message": f"Slot {lid} not in worker map"}

        try:
            logger.info(f"Capturing baseline for LID={lid} (is_occupied={is_occupied}, wait={wait_for_stable}s)")
            time.sleep(wait_for_stable)

            baseline_emb = self._capture_baseline(slot)
            if baseline_emb is None:
                return {"status": "error", "message": "Failed to capture baseline"}

            # Persist to DB
            from back_end.slot_monitor.db_interface import SlotMonitorDB
            SlotMonitorDB.save_baseline(lid, baseline_emb)

            # Update in-memory slot state — reset_baseline clears history/mismatch/grace
            slot.reset_baseline(baseline_emb)
            slot.is_occupied = is_occupied

            logger.info(f"Baseline captured and saved for LID={lid}")
            return {
                "status": "success",
                "message": "Baseline captured successfully",
                "baseline": baseline_emb,
            }

        except Exception as e:
            logger.error(f"Failed to capture baseline for LID {lid}: {e}")
            return {"status": "error", "message": str(e)}

    def _capture_baseline(self, slot) -> Optional[np.ndarray]:
        """
        Capture a stable baseline embedding for a slot.

        Takes 3 samples from the live frame buffer using the slot's own
        ROI extractor and averages them.

        Args:
            slot: Slot instance (provides compute_embedding)

        Returns:
            Normalised float32 embedding, or None on failure
        """
        try:
            embeddings = []
            for i in range(3):
                frame = self.monitor.frame_buffer.get_frame_sync()
                if frame is None:
                    logger.warning("Frame buffer returned None during baseline capture")
                    continue
                embeddings.append(slot.compute_embedding(frame))
                if i < 2:
                    time.sleep(0.2)

            if not embeddings:
                logger.error("No frames captured for baseline")
                return None

            avg_emb = np.mean(embeddings, axis=0)
            norm = np.linalg.norm(avg_emb)
            if norm > 1e-8:
                avg_emb = avg_emb / norm

            return avg_emb.astype(np.float32)

        except Exception as e:
            logger.error(f"Failed to capture baseline for slot {slot.lid}: {e}")
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

                monitor_state = None
                if self.monitor and self.monitor.worker_pool:
                    worker = self.monitor.worker_pool._get_worker(lid)
                    if worker:
                        slot = worker._slot_map.get(lid)
                        if slot:
                            monitor_state = {
                                "last_distance": slot.last_dist,
                                "mismatch": slot.mismatch,
                                "is_occupied": slot.is_occupied,
                            }

                return {
                    "status": "success",
                    "lid": lid,
                    "occupied": pid is not None,
                    "phone": {
                        "pid": pid,
                        "imei": imei,
                        "model": model,
                        "stored_at": stored_at.isoformat() if stored_at else None,
                    } if pid else None,
                    "monitoring": monitor_state,
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