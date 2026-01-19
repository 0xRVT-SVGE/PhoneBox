# ============================================================
# FILE: back_end/Database/API/slot_operations.py
# ============================================================

import logging
import time
from typing import Dict, Optional
import numpy as np
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)


class SlotOperations:
    """Handle all slot monitoring operations including phone deposit/withdrawal"""

    def __init__(self, monitor=None):
        """
        Args:
            monitor: SlotMonitor instance for camera access (can be None initially)
        """
        self.monitor = monitor
        logger.info("SlotOperations initialized")

    def set_monitor(self, monitor):
        """Set the monitor reference after initialization"""
        self.monitor = monitor
        logger.info("SlotMonitor reference attached to SlotOperations")

    # ============================================================
    # PHONE DEPOSIT/WITHDRAWAL OPERATIONS
    # ============================================================

    def deposit_phone(self, pid: str, lid: int, wait_for_stable: float = 3.0) -> Dict:
        """
        Deposit a phone into a specific storage location.

        Args:
            pid: Phone UUID
            lid: Location ID where phone will be stored
            wait_for_stable: Seconds to wait after placing phone

        Returns:
            dict with status, message, and data
        """
        if not self.monitor:
            return {"status": "error", "message": "Slot monitor not available"}

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Verify phone exists and is not already stored
                cur.execute("""
                            SELECT p.sid, ps.pid
                            FROM phones p
                                     LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL
                            WHERE p.pid = %s
                            """, (pid,))
                result = cur.fetchone()

                if not result:
                    return {"status": "error", "message": "Phone not found"}

                sid, existing_storage = result

                if existing_storage:
                    return {"status": "error", "message": "Phone already in storage"}

                # Verify location exists and is empty
                cur.execute("""
                            SELECT ps.pid
                            FROM phone_storage ps
                            WHERE ps.lid = %s
                              AND ps.retrieved_at IS NULL
                            """, (lid,))

                if cur.fetchone():
                    return {"status": "error", "message": f"Location {lid} already occupied"}

                logger.info(f"Starting deposit: phone {pid} at location {lid}")

                # Pause monitoring for this slot
                self.monitor.pause_slot(lid)

                # Wait for operator to place phone and remove hand
                logger.info(f"Waiting {wait_for_stable}s for slot to stabilize...")
                time.sleep(wait_for_stable)

                # Capture new baseline
                baseline_emb = self._capture_slot_baseline(lid)
                if baseline_emb is None:
                    self.monitor.resume_slot(lid)
                    return {"status": "error", "message": "Failed to capture baseline"}

                # Create storage record (triggers DB state update via trigger)
                cur.execute("""
                            INSERT INTO phone_storage (pid, lid, stored_at)
                            VALUES (%s, %s, NOW())
                            RETURNING id;
                            """, (pid, lid))

                storage_id = cur.fetchone()[0]
                conn.commit()

                # Resume monitoring with new baseline
                self.monitor.resume_slot(lid, baseline_emb)

                logger.info(f"Phone {pid} deposited successfully at location {lid}")
                return {
                    "status": "success",
                    "message": "Phone deposited successfully",
                    "pid": pid,
                    "lid": lid,
                    "student": sid,
                    "storage_id": storage_id
                }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to deposit phone {pid}: {e}")
            if 'lid' in locals():
                self.monitor.resume_slot(lid)
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def withdraw_phone(self, pid: str, wait_for_removal: float = 3.0) -> Dict:
        """
        Withdraw a phone from storage.

        Args:
            pid: Phone UUID
            wait_for_removal: Seconds to wait after removing phone

        Returns:
            dict with status, message, and data
        """
        if not self.monitor:
            return {"status": "error", "message": "Slot monitor not available"}

        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Get active storage record
                cur.execute("""
                            SELECT ps.id, ps.lid, p.sid
                            FROM phone_storage ps
                                     JOIN phones p ON ps.pid = p.pid
                            WHERE ps.pid = %s
                              AND ps.retrieved_at IS NULL
                            """, (pid,))
                result = cur.fetchone()

                if not result:
                    return {"status": "error", "message": "Phone not in storage"}

                storage_id, lid, sid = result

                logger.info(f"Starting withdrawal: phone {pid} from location {lid}")

                # Pause monitoring for this slot
                self.monitor.pause_slot(lid)

                # Wait for operator to scan QR and remove hand
                logger.info(f"Waiting {wait_for_removal}s for slot to stabilize...")
                time.sleep(wait_for_removal)

                # Capture new baseline (empty slot)
                baseline_emb = self._capture_slot_baseline(lid)
                if baseline_emb is None:
                    self.monitor.resume_slot(lid)
                    return {"status": "error", "message": "Failed to capture baseline"}

                # Update storage record (triggers DB state update via trigger)
                cur.execute("""
                            UPDATE phone_storage
                            SET retrieved_at = NOW()
                            WHERE id = %s;
                            """, (storage_id,))

                conn.commit()

                # Resume monitoring with new baseline
                self.monitor.resume_slot(lid, baseline_emb)

                logger.info(f"Phone {pid} withdrawn successfully from location {lid}")
                return {
                    "status": "success",
                    "message": "Phone withdrawn successfully",
                    "pid": pid,
                    "lid": lid,
                    "student": sid,
                    "storage_id": storage_id
                }

        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to withdraw phone {pid}: {e}")
            if 'lid' in locals():
                self.monitor.resume_slot(lid)
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def _capture_slot_baseline(self, lid: int) -> Optional[np.ndarray]:
        """Capture baseline embedding for a slot"""
        from .slot_embed import compute_embedding

        try:
            # Capture multiple frames and average for stability
            embeddings = []
            for _ in range(3):
                frame = self.monitor.camera.read()
                if frame is None:
                    logger.error("Failed to capture frame for baseline")
                    return None

                rois = self.monitor.camera.extract_rois(frame)
                if lid not in rois:
                    logger.error(f"Slot {lid} not found in ROIs")
                    return None

                roi = rois[lid]
                emb = compute_embedding(roi)
                embeddings.append(emb)
                time.sleep(0.1)

            # Average the embeddings
            avg_emb = np.mean(embeddings, axis=0)
            norm = np.linalg.norm(avg_emb)
            if norm > 1e-8:
                avg_emb = avg_emb / norm

            return avg_emb.astype(np.float32)

        except Exception as e:
            logger.error(f"Failed to capture baseline for slot {lid}: {e}")
            return None

    # ============================================================
    # SLOT STATUS QUERIES
    # ============================================================

    def get_slot_status(self, lid: int) -> Dict:
        """Get current status of a specific slot"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT s.binary_state,
                                   s.tx_state,
                                   s.last_distance,
                                   s.last_change_ts,
                                   ps.pid,
                                   p.imei
                            FROM slot_current_state s
                                     LEFT JOIN phone_storage ps ON s.lid = ps.lid AND ps.retrieved_at IS NULL
                                     LEFT JOIN phones p ON ps.pid = p.pid
                            WHERE s.lid = %s;
                            """, (lid,))
                row = cur.fetchone()

                if not row:
                    return {"status": "error", "message": "Slot not found"}

                return {
                    "status": "success",
                    "lid": lid,
                    "binary_state": row[0],
                    "tx_state": row[1],
                    "distance": float(row[2]),
                    "last_change": row[3].isoformat(),
                    "phone_pid": row[4],
                    "phone_imei": row[5]
                }
        except Exception as e:
            logger.error(f"Error fetching slot {lid} status: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def get_all_slots(self) -> Dict:
        """Get status of all slots"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                            SELECT l.lid,
                                   l.x,
                                   l.y,
                                   s.binary_state,
                                   s.tx_state,
                                   s.last_distance,
                                   s.last_change_ts,
                                   ps.pid,
                                   p.imei,
                                   p.model,
                                   p.sid
                            FROM locations l
                                     LEFT JOIN slot_current_state s ON l.lid = s.lid
                                     LEFT JOIN phone_storage ps ON l.lid = ps.lid AND ps.retrieved_at IS NULL
                                     LEFT JOIN phones p ON ps.pid = p.pid
                            ORDER BY l.lid;
                            """)
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]

                slots = []
                for row in rows:
                    slot_data = dict(zip(columns, row))
                    if slot_data.get('last_change_ts'):
                        slot_data['last_change_ts'] = slot_data['last_change_ts'].isoformat()
                    slots.append(slot_data)

                return {
                    "status": "success",
                    "slots": slots,
                    "count": len(slots)
                }
        except Exception as e:
            logger.error(f"Error fetching all slots: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def get_problem_slots(self) -> Dict:
        """Get all slots with monitoring issues"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM v_problem_slots;")
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]

                problems = []
                for row in rows:
                    problem = dict(zip(columns, row))
                    if problem.get('last_change_ts'):
                        problem['last_change_ts'] = problem['last_change_ts'].isoformat()
                    problems.append(problem)

                return {
                    "status": "success",
                    "problems": problems,
                    "count": len(problems)
                }
        except Exception as e:
            logger.error(f"Error fetching problem slots: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def get_anomalies(self, hours: int = 24, severity: Optional[str] = None) -> Dict:
        """Get recent slot anomalies"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                if severity:
                    cur.execute("""
                                SELECT id, lid, anomaly_type, distance, severity, description, timestamp
                                FROM slot_anomalies
                                WHERE timestamp > NOW() - INTERVAL '%s hours'
                                  AND severity = %s
                                ORDER BY timestamp DESC
                                LIMIT 100;
                                """, (hours, severity))
                else:
                    cur.execute("""
                                SELECT id, lid, anomaly_type, distance, severity, description, timestamp
                                FROM slot_anomalies
                                WHERE timestamp > NOW() - INTERVAL '%s hours'
                                ORDER BY timestamp DESC
                                LIMIT 100;
                                """, (hours,))

                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]

                anomalies = []
                for row in rows:
                    anomaly = dict(zip(columns, row))
                    if anomaly.get('timestamp'):
                        anomaly['timestamp'] = anomaly['timestamp'].isoformat()
                    anomalies.append(anomaly)

                return {
                    "status": "success",
                    "anomalies": anomalies,
                    "count": len(anomalies)
                }
        except Exception as e:
            logger.error(f"Error fetching anomalies: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def get_system_health(self) -> Dict:
        """Get current system health metrics"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM v_system_health_current;")
                row = cur.fetchone()

                if row:
                    columns = [desc[0] for desc in cur.description]
                    health = dict(zip(columns, row))
                else:
                    health = {
                        "total_slots": 0,
                        "occupied_slots": 0,
                        "empty_slots": 0,
                        "unknown_slots": 0,
                        "altered_slots": 0,
                        "avg_distance": 0.0,
                        "max_distance": 0.0,
                        "anomalies_last_hour": 0,
                        "errors_last_hour": 0
                    }

                return {
                    "status": "success",
                    "health": health
                }
        except Exception as e:
            logger.error(f"Error fetching system health: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    def get_empty_locations(self, limit: int = 10) -> Dict:
        """Get available empty locations"""
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM get_available_locations(%s);", (limit,))
                rows = cur.fetchall()
                columns = [desc[0] for desc in cur.description]

                locations = [dict(zip(columns, row)) for row in rows]

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