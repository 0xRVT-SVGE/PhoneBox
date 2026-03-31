# ============================================================
# FILE: back_end/slot_monitor/slot_operations.py
# ============================================================
"""
Slot operations - DATABASE MUTATIONS ONLY.

Optimization vs previous version
──────────────────────────────────
_capture_baseline previously took 3 samples with time.sleep(0.2) between
each, adding 0.4 s of blocking wait on top of wait_for_stable.
It now takes a single sample — wait_for_stable already lets vibrations
settle, so averaging 3 close frames added latency without meaningfully
improving accuracy.  Total baseline capture time is now:

    wait_for_stable  (caller-controlled, default 1.5–2.0 s)
  + one frame read   (~0 ms, frame is in memory)

Methods:
- deposit_phone_db():          Create storage record
- withdraw_phone_db():         Mark phone as retrieved
- capture_and_save_baseline(): Capture frame, compute embedding, persist to DB,
                                update in-memory slot state.
                                Callers own pause/resume.
"""

import logging
import time
from typing import Dict, Optional
import numpy as np
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)


class SlotOperations:

    def __init__(self, monitor=None):
        self.monitor = monitor
        logger.info("SlotOperations initialized")

    def set_monitor(self, monitor):
        self.monitor = monitor
        logger.info("Monitor attached to SlotOperations")

    # ── Deposit ───────────────────────────────────────────────────────────────

    def deposit_phone_db(self, pid: str, lid: int) -> Dict:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        EXISTS(SELECT 1 FROM phones WHERE pid = %s),
                        EXISTS(SELECT 1 FROM phone_storage
                               WHERE pid = %s AND retrieved_at IS NULL),
                        EXISTS(SELECT 1 FROM phone_storage
                               WHERE lid = %s AND retrieved_at IS NULL);
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

            logger.info(f"Deposit DB record: PID={pid} LID={lid} storage_id={storage_id}")
            return {
                "status":     "success",
                "message":    "Deposit recorded successfully",
                "pid":        pid,
                "lid":        lid,
                "storage_id": storage_id,
            }
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to create deposit record for PID {pid}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    # ── Withdraw ──────────────────────────────────────────────────────────────

    def withdraw_phone_db(self, pid: str) -> Dict:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ps.id, ps.lid
                    FROM phone_storage ps
                    WHERE ps.pid = %s AND ps.retrieved_at IS NULL;
                    """, (pid,))
                result = cur.fetchone()

                if not result:
                    return {"status": "error", "message": "Phone not in storage"}

                storage_id, lid = result
                cur.execute("""
                    UPDATE phone_storage SET retrieved_at = NOW()
                    WHERE id = %s;
                    """, (storage_id,))
                conn.commit()

            logger.info(f"Withdrawal DB record: PID={pid} LID={lid} storage_id={storage_id}")
            return {
                "status":     "success",
                "message":    "Withdrawal recorded successfully",
                "pid":        pid,
                "lid":        lid,
                "storage_id": storage_id,
            }
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to update withdrawal record for PID {pid}: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)

    # ── Baseline ──────────────────────────────────────────────────────────────

    def capture_and_save_baseline(
        self,
        lid:             int,
        is_occupied:     bool,
        wait_for_stable: float = 3.0,
    ) -> Dict:
        """
        Capture a new baseline embedding for a slot.

        wait_for_stable seconds of sleep lets vibrations settle, then
        a single frame is read and embedded.  Averaging 3 frames (old
        behaviour) added 0.4 s of extra blocking time without measurably
        improving accuracy for this use case.

        Does NOT pause/resume the slot — the caller owns that lifecycle.
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
            logger.info(
                f"Capturing baseline LID={lid} is_occupied={is_occupied} "
                f"wait={wait_for_stable}s"
            )
            time.sleep(wait_for_stable)

            baseline_emb = self._capture_baseline(slot)
            if baseline_emb is None:
                return {"status": "error", "message": "Failed to capture baseline"}

            from back_end.slot_monitor.db_interface import SlotMonitorDB
            SlotMonitorDB.save_baseline(lid, baseline_emb)

            slot.reset_baseline(baseline_emb)
            slot.is_occupied = is_occupied

            logger.info(f"Baseline saved for LID={lid}")
            return {"status": "success", "message": "Baseline captured", "baseline": baseline_emb}

        except Exception as e:
            logger.error(f"Failed to capture baseline for LID {lid}: {e}")
            return {"status": "error", "message": str(e)}

    def _capture_baseline(self, slot) -> Optional[np.ndarray]:
        """
        Read one frame from the slot monitor's frame buffer and compute
        an embedding for the given slot ROI.

        Previously took 3 samples with 200 ms sleeps between them (total
        +0.4 s blocking).  A single sample is sufficient because
        capture_and_save_baseline() already waits wait_for_stable seconds
        before calling here, so the scene is settled by the time we read.
        """
        try:
            frame = self.monitor.frame_buffer.get_frame_sync()
            if frame is None:
                logger.warning("[SlotOps] Frame buffer returned None during baseline capture")
                return None

            emb  = slot.compute_embedding(frame)
            norm = np.linalg.norm(emb)
            if norm > 1e-8:
                emb = emb / norm
            return emb.astype(np.float32)

        except Exception as e:
            logger.error(f"[SlotOps] _capture_baseline failed for slot {slot.lid}: {e}")
            return None

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_slot_status(self, lid: int) -> Dict:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT l.lid, ps.pid, p.imei, p.model,
                           ps.stored_at, ps.retrieved_at
                    FROM locations l
                    LEFT JOIN phone_storage ps
                        ON l.lid = ps.lid AND ps.retrieved_at IS NULL
                    LEFT JOIN phones p ON ps.pid = p.pid
                    WHERE l.lid = %s;
                    """, (lid,))
                row = cur.fetchone()

            if not row:
                return {"status": "error", "message": "Location not found"}

            lid_r, pid, imei, model, stored_at, _ = row

            monitor_state = None
            if self.monitor and self.monitor.worker_pool:
                w = self.monitor.worker_pool._get_worker(lid)
                if w:
                    s = w._slot_map.get(lid)
                    if s:
                        monitor_state = {
                            "last_distance": s.last_dist,
                            "mismatch":      s.mismatch,
                            "is_occupied":   s.is_occupied,
                        }

            return {
                "status":    "success",
                "lid":       lid_r,
                "occupied":  pid is not None,
                "phone": {
                    "pid":       pid,
                    "imei":      imei,
                    "model":     model,
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
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT l.lid, l.x, l.y
                    FROM locations l
                    LEFT JOIN phone_storage ps
                        ON l.lid = ps.lid AND ps.retrieved_at IS NULL
                    WHERE ps.pid IS NULL
                    ORDER BY l.lid LIMIT %s;
                    """, (limit,))
                rows = cur.fetchall()

            return {
                "status":    "success",
                "locations": [{"lid": r[0], "x": r[1], "y": r[2]} for r in rows],
                "count":     len(rows),
            }
        except Exception as e:
            logger.error(f"Error fetching empty locations: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            put_conn(conn)