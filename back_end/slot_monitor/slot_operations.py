# ============================================================
# FILE: back_end/slot_monitor/slot_operations.py
# ============================================================
"""
Slot operations — DATABASE MUTATIONS + BOTTOM-CAMERA PLACEMENT VERIFICATION.

Changes from previous version
──────────────────────────────
deposit_phone_db
    Single atomic conditional INSERT with pg_try_advisory_xact_lock(lid).
    Eliminates 3 round-trip pre-checks and the TOCTOU race window.
    Diagnostic SELECT only runs on the rare INSERT-miss path.

make_placement_verifier
    Returns a callable querying the BOTTOM camera embedding for PhoneTracker.
    A phone held over but not placed in the slot will not change the
    bottom-camera view enough to cross PLACEMENT_DETECTION_THRESHOLD.

_invalidate_async_cache
    After every deposit/withdraw the async DB's per-lid PID cache is dropped
    so monitoring workers don't fire stale alarms.
"""

import logging
import time
from typing import Callable, Dict, Optional
import numpy as np
from back_end.Database.db import get_conn, put_conn

logger = logging.getLogger(__name__)

# Minimum bottom-cam embedding distance from empty-slot baseline that
# indicates a phone is physically present. Calibrate for your sensor.
# Typical values: empty=0.01-0.04, phone=0.12-0.25, hand=0.06-0.15.
PLACEMENT_DETECTION_THRESHOLD = 0.10


class SlotOperations:

    def __init__(self, monitor=None):
        self.monitor = monitor
        logger.info("SlotOperations initialized")

    def set_monitor(self, monitor):
        self.monitor = monitor
        logger.info("Monitor attached to SlotOperations")

    # ── Bottom-camera placement verifier ──────────────────────────────────────

    def make_placement_verifier(self, lid: int) -> Callable[[], bool]:
        """
        Return a zero-argument callable that checks whether a phone is
        physically present in slot `lid` via the bottom camera.

        Designed to be passed to PhoneTracker as verify_fn.
        Returns True (assume placed) on any error so operations can still
        succeed when the bottom camera is unavailable.
        """
        monitor_ref = self.monitor

        def _verify() -> bool:
            if monitor_ref is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no monitor")
                return True
            fb = getattr(monitor_ref, "frame_buffer", None)
            if fb is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no frame_buffer")
                return True
            wp = getattr(monitor_ref, "worker_pool", None)
            if wp is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no worker_pool")
                return True
            worker = wp._get_worker(lid)
            if worker is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no worker")
                return True
            slot = worker._slot_map.get(lid)
            if slot is None:
                logger.debug(f"[SlotOps] verify LID={lid}: slot not in map")
                return True
            frame = fb.get_frame_sync()
            if frame is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no frame")
                return True
            try:
                dist = slot.compute_distance(frame)
                placed = dist > PLACEMENT_DETECTION_THRESHOLD
                logger.info(
                    f"[SlotOps] Placement verify LID={lid}: dist={dist:.4f} "
                    f"threshold={PLACEMENT_DETECTION_THRESHOLD} "
                    f"-> {'PLACED' if placed else 'NOT PLACED'}"
                )
                return placed
            except Exception as exc:
                logger.warning(f"[SlotOps] verify LID={lid}: {exc} — assuming placed")
                return True

        return _verify

    # ── Async cache invalidation ───────────────────────────────────────────────

    def _invalidate_async_cache(self, lid: int) -> None:
        """
        Drop lid from the async DB PID cache after deposit/withdraw.
        dict.pop() is GIL-atomic; safe to call from any thread.
        """
        try:
            db = getattr(self.monitor, "db", None)
            if db is not None and hasattr(db, "invalidate_pid_cache_sync"):
                db.invalidate_pid_cache_sync(lid)
        except Exception as exc:
            logger.warning(f"[SlotOps] Cache invalidation failed LID={lid}: {exc}")

    # ── Deposit ───────────────────────────────────────────────────────────────

    def deposit_phone_db(self, pid: str, lid: int) -> Dict:
        """
        Atomically validate and record a deposit.

        Uses pg_try_advisory_xact_lock(lid) to serialise concurrent deposits
        to the same slot, then a single conditional INSERT that embeds all
        pre-condition checks. Diagnostic SELECT only runs on INSERT miss.
        """
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                # Acquire per-slot advisory lock (released on transaction end)
                cur.execute("SELECT pg_try_advisory_xact_lock(%s);", (lid,))
                if not cur.fetchone()[0]:
                    return {"status": "error",
                            "message": f"Location {lid} temporarily locked — retry"}

                # Single conditional INSERT: all checks + write in one round-trip
                cur.execute("""
                    INSERT INTO phone_storage (pid, lid, stored_at)
                    SELECT %s, %s, NOW()
                    WHERE
                        EXISTS     (SELECT 1 FROM phones WHERE pid = %s)
                        AND NOT EXISTS (SELECT 1 FROM phone_storage
                                        WHERE pid = %s AND retrieved_at IS NULL)
                        AND NOT EXISTS (SELECT 1 FROM phone_storage
                                        WHERE lid = %s AND retrieved_at IS NULL)
                    RETURNING id;
                """, (pid, lid, pid, pid, lid))
                row = cur.fetchone()

                if row is None:
                    # Diagnostic only on failure
                    cur.execute("""
                        SELECT
                            EXISTS(SELECT 1 FROM phones WHERE pid = %s),
                            EXISTS(SELECT 1 FROM phone_storage
                                   WHERE pid = %s AND retrieved_at IS NULL),
                            EXISTS(SELECT 1 FROM phone_storage
                                   WHERE lid = %s AND retrieved_at IS NULL);
                    """, (pid, pid, lid))
                    phone_ok, already, occupied = cur.fetchone()
                    if not phone_ok:
                        return {"status": "error", "message": "Phone not found in database"}
                    if already:
                        return {"status": "error", "message": "Phone already in storage"}
                    return {"status": "error", "message": f"Location {lid} already occupied"}

                storage_id = row[0]
                conn.commit()

            logger.info(f"[SlotOps] Deposit: PID={pid} LID={lid} id={storage_id}")
            self._invalidate_async_cache(lid)
            return {"status": "success", "message": "Deposit recorded",
                    "pid": pid, "lid": lid, "storage_id": storage_id}

        except Exception as exc:
            conn.rollback()
            logger.error(f"[SlotOps] deposit_phone_db PID={pid}: {exc}")
            return {"status": "error", "message": str(exc)}
        finally:
            put_conn(conn)

    # ── Withdraw ──────────────────────────────────────────────────────────────

    def withdraw_phone_db(self, pid: str) -> Dict:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ps.id, ps.lid FROM phone_storage ps
                    WHERE ps.pid = %s AND ps.retrieved_at IS NULL;
                """, (pid,))
                result = cur.fetchone()
                if not result:
                    return {"status": "error", "message": "Phone not in storage"}
                storage_id, lid = result
                cur.execute(
                    "UPDATE phone_storage SET retrieved_at=NOW() WHERE id=%s;",
                    (storage_id,)
                )
                conn.commit()

            logger.info(f"[SlotOps] Withdraw: PID={pid} LID={lid} id={storage_id}")
            self._invalidate_async_cache(lid)
            return {"status": "success", "message": "Withdrawal recorded",
                    "pid": pid, "lid": lid, "storage_id": storage_id}

        except Exception as exc:
            conn.rollback()
            logger.error(f"[SlotOps] withdraw_phone_db PID={pid}: {exc}")
            return {"status": "error", "message": str(exc)}
        finally:
            put_conn(conn)

    # ── Baseline ──────────────────────────────────────────────────────────────

    def capture_and_save_baseline(self, lid: int, is_occupied: bool,
                                   wait_for_stable: float = 3.0) -> Dict:
        if not self.monitor:
            return {"status": "error", "message": "Monitor not available"}
        worker = self.monitor.worker_pool._get_worker(lid)
        if worker is None:
            return {"status": "error", "message": f"No worker for slot {lid}"}
        slot = worker._slot_map.get(lid)
        if slot is None:
            return {"status": "error", "message": f"Slot {lid} not in worker map"}
        try:
            logger.info(f"[SlotOps] Baseline LID={lid} occ={is_occupied} wait={wait_for_stable}s")
            time.sleep(wait_for_stable)
            emb = self._capture_baseline(slot)
            if emb is None:
                return {"status": "error", "message": "Failed to capture baseline"}
            from back_end.slot_monitor.db_interface import SlotMonitorDB
            SlotMonitorDB.save_baseline(lid, emb)
            slot.reset_baseline(emb)
            slot.is_occupied = is_occupied
            logger.info(f"[SlotOps] Baseline saved LID={lid}")
            return {"status": "success", "message": "Baseline captured", "baseline": emb}
        except Exception as exc:
            logger.error(f"[SlotOps] baseline LID={lid}: {exc}")
            return {"status": "error", "message": str(exc)}

    def _capture_baseline(self, slot) -> Optional[np.ndarray]:
        try:
            frame = self.monitor.frame_buffer.get_frame_sync()
            if frame is None: return None
            emb = slot.compute_embedding(frame)
            norm = np.linalg.norm(emb)
            if norm > 1e-8: emb = emb / norm
            return emb.astype(np.float32)
        except Exception as exc:
            logger.error(f"[SlotOps] _capture_baseline slot {slot.lid}: {exc}")
            return None

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_slot_status(self, lid: int) -> Dict:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT l.lid, ps.pid, p.imei, p.model, ps.stored_at, ps.retrieved_at
                    FROM locations l
                    LEFT JOIN phone_storage ps ON l.lid=ps.lid AND ps.retrieved_at IS NULL
                    LEFT JOIN phones p ON ps.pid=p.pid
                    WHERE l.lid=%s;
                """, (lid,))
                row = cur.fetchone()
            if not row: return {"status": "error", "message": "Location not found"}
            lid_r,pid,imei,model,stored_at,_ = row
            ms = None
            if self.monitor and self.monitor.worker_pool:
                w = self.monitor.worker_pool._get_worker(lid)
                if w:
                    s = w._slot_map.get(lid)
                    if s: ms = {"last_distance":s.last_dist,"mismatch":s.mismatch,"is_occupied":s.is_occupied}
            return {"status":"success","lid":lid_r,"occupied":pid is not None,
                    "phone":{"pid":pid,"imei":imei,"model":model,
                             "stored_at":stored_at.isoformat() if stored_at else None} if pid else None,
                    "monitoring":ms}
        except Exception as exc:
            logger.error(f"[SlotOps] get_slot_status: {exc}")
            return {"status":"error","message":str(exc)}
        finally:
            put_conn(conn)

    def get_empty_locations(self, limit: int = 10) -> Dict:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT l.lid,l.x,l.y FROM locations l
                    LEFT JOIN phone_storage ps ON l.lid=ps.lid AND ps.retrieved_at IS NULL
                    WHERE ps.pid IS NULL ORDER BY l.lid LIMIT %s;
                """, (limit,))
                rows = cur.fetchall()
            return {"status":"success","locations":[{"lid":r[0],"x":r[1],"y":r[2]} for r in rows],"count":len(rows)}
        except Exception as exc:
            logger.error(f"[SlotOps] get_empty_locations: {exc}")
            return {"status":"error","message":str(exc)}
        finally:
            put_conn(conn)