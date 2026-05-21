# ============================================================
# FILE: back_end/slot_monitor/slot_operations.py
# ============================================================
"""
Slot operations — DATABASE MUTATIONS + BOTTOM-CAMERA SLOT-CHANGE VERIFICATION.

Slot-change verifier  (make_placement_verifier)
───────────────────────────────────────────────
Returns a zero-argument callable that verifies the slot physically changed
during an operation by comparing two bottom-camera embeddings:

  before_emb  — snapshot taken when make_placement_verifier() is CALLED
  current_emb — snapshot taken when the returned callable is CALLED

  result: cosine_distance(before_emb, current_emb) > SLOT_CHANGE_THRESHOLD

Works identically for deposit (before=empty → after=occupied) and
withdraw (before=occupied → after=empty).

Caller responsibility:
  deposit  → created inside create_tracker_for_operation() after QR scan
             (phone in hand, slot is empty at snapshot time)
  withdraw → created in handle_withdraw() before QR scan starts
             (phone still in slot at snapshot time)

Fail-safe: any error returns True so operations can complete when the
bottom camera is unavailable.
"""

import logging
import time
from typing import Callable, Dict, Optional
import numpy as np
from Backup.back_end.Database.db import get_conn, put_conn
from Backup.back_end.config import AlarmConfig as _AC

logger = logging.getLogger(__name__)

SLOT_CHANGE_THRESHOLD = _AC.SLOT_CHANGE_THRESHOLD


class SlotOperations:

    def __init__(self, monitor=None):
        self.monitor = monitor
        logger.info("SlotOperations initialized")

    def set_monitor(self, monitor):
        self.monitor = monitor
        logger.info("Monitor attached to SlotOperations")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_slot(self, lid: int):
        """Resolve lid → (slot, frame_buffer) or (None, None) on any failure."""
        fb     = getattr(self.monitor, "frame_buffer", None) if self.monitor else None
        wp     = getattr(self.monitor, "worker_pool",  None) if self.monitor else None
        worker = wp._get_worker(lid) if wp else None
        slot   = worker._slot_map.get(lid) if worker else None
        return slot, fb

    def _current_embedding(self, slot, fb) -> Optional[np.ndarray]:
        """Capture and L2-normalise the current embedding for *slot*."""
        try:
            frame = fb.get_frame_sync()
            if frame is None:
                return None
            emb  = slot.compute_embedding(frame)
            norm = np.linalg.norm(emb)
            if norm > 1e-8:
                emb = emb / norm
            return emb.astype(np.float32)
        except Exception as exc:
            logger.warning(f"[SlotOps] embedding snapshot slot {slot.lid}: {exc}")
            return None

    # ── Slot-change verifier ──────────────────────────────────────────────────

    def make_placement_verifier(self, lid: int) -> Callable[[], bool]:
        """
        Return a zero-argument callable that checks whether slot *lid*
        physically changed since this method was called.

        Call immediately BEFORE the physical action to capture the "before"
        snapshot, then call the returned callable AFTER.
        """
        slot, fb = self._get_slot(lid)

        # Capture "before" snapshot now.
        before_emb: Optional[np.ndarray] = None
        if slot is not None and fb is not None:
            before_emb = self._current_embedding(slot, fb)
            if before_emb is not None:
                logger.debug(f"[SlotOps] Before-snapshot captured LID={lid}")
            else:
                logger.warning(
                    f"[SlotOps] Before-snapshot failed LID={lid} — "
                    "slot-change check will be skipped (fail-open)"
                )

        def _verify() -> bool:
            # Guard: infrastructure must be available
            if slot is None or fb is None:
                logger.debug(f"[SlotOps] verify LID={lid}: infrastructure unavailable")
                return True
            if before_emb is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no before-snapshot, skipping")
                return True

            after_emb = _current_embedding_local()
            if after_emb is None:
                logger.debug(f"[SlotOps] verify LID={lid}: no after frame")
                return True

            try:
                dist    = float(1.0 - np.dot(before_emb, after_emb))
                changed = dist > SLOT_CHANGE_THRESHOLD
                logger.info(
                    f"[SlotOps] Slot-change verify LID={lid}: "
                    f"dist={dist:.4f} threshold={SLOT_CHANGE_THRESHOLD} "
                    f"-> {'CHANGED' if changed else 'NO CHANGE'}"
                )
                return changed
            except Exception as exc:
                logger.warning(f"[SlotOps] verify LID={lid}: {exc} — assuming changed")
                return True

        def _current_embedding_local() -> Optional[np.ndarray]:
            try:
                frame = fb.get_frame_sync()
                if frame is None:
                    return None
                emb  = slot.compute_embedding(frame)
                norm = np.linalg.norm(emb)
                if norm > 1e-8:
                    emb = emb / norm
                return emb.astype(np.float32)
            except Exception as exc:
                logger.warning(f"[SlotOps] after-snapshot LID={lid}: {exc}")
                return None

        return _verify

    # ── Async cache invalidation ───────────────────────────────────────────────

    def _invalidate_async_cache(self, lid: int) -> None:
        try:
            db = getattr(self.monitor, "db", None)
            if db is not None and hasattr(db, "invalidate_pid_cache_sync"):
                db.invalidate_pid_cache_sync(lid)
        except Exception as exc:
            logger.warning(f"[SlotOps] Cache invalidation failed LID={lid}: {exc}")

    # ── Deposit ───────────────────────────────────────────────────────────────

    def deposit_phone_db(self, pid: str, lid: int) -> Dict:
        from Backup.back_end.slot_monitor.db_interface import _BOX_ID
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s);", (lid,))
                if not cur.fetchone()[0]:
                    return {"status": "error",
                            "message": f"Location {lid} temporarily locked — retry"}

                cur.execute("""
                    INSERT INTO phone_storage (pid, lid, box_id, stored_at)
                    SELECT %s, %s, %s, NOW()
                    WHERE
                        EXISTS     (SELECT 1 FROM phones WHERE pid = %s)
                        AND NOT EXISTS (SELECT 1 FROM phone_storage
                                        WHERE pid = %s AND retrieved_at IS NULL)
                        AND NOT EXISTS (SELECT 1 FROM phone_storage
                                        WHERE lid = %s AND retrieved_at IS NULL)
                    RETURNING id;
                """, (pid, lid, _BOX_ID, pid, pid, lid))
                row = cur.fetchone()

                if row is None:
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
                    return {"status": "error",
                            "message": f"Location {lid} already occupied"}

                storage_id = row[0]
                conn.commit()

            logger.info(f"[SlotOps] Deposit: PID={pid} LID={lid} box_id={_BOX_ID} id={storage_id}")
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
        slot, fb = self._get_slot(lid)
        if slot is None:
            return {"status": "error", "message": f"No slot object for LID={lid}"}
        try:
            logger.info(
                f"[SlotOps] Baseline LID={lid} occ={is_occupied} "
                f"wait={wait_for_stable}s"
            )
            time.sleep(wait_for_stable)
            emb = self._current_embedding(slot, fb)
            if emb is None:
                return {"status": "error", "message": "Failed to capture baseline"}
            from Backup.back_end.slot_monitor.db_interface import SlotMonitorDB
            SlotMonitorDB.save_baseline(lid, emb)
            slot.reset_baseline(emb)
            slot.is_occupied = is_occupied
            logger.info(f"[SlotOps] Baseline saved LID={lid}")
            return {"status": "success", "message": "Baseline captured", "baseline": emb}
        except Exception as exc:
            logger.error(f"[SlotOps] baseline LID={lid}: {exc}")
            return {"status": "error", "message": str(exc)}

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
            if not row:
                return {"status": "error", "message": "Location not found"}
            lid_r, pid, imei, model, stored_at, _ = row
            slot, _ = self._get_slot(lid)
            ms = {
                "last_distance": slot.last_dist,
                "mismatch":      slot.mismatch,
                "is_occupied":   slot.is_occupied,
            } if slot else None
            return {
                "status":   "success",
                "lid":      lid_r,
                "occupied": pid is not None,
                "phone": {
                    "pid":       pid,
                    "imei":      imei,
                    "model":     model,
                    "stored_at": stored_at.isoformat() if stored_at else None,
                } if pid else None,
                "monitoring": ms,
            }
        except Exception as exc:
            logger.error(f"[SlotOps] get_slot_status: {exc}")
            return {"status": "error", "message": str(exc)}
        finally:
            put_conn(conn)

    def get_empty_locations(self, limit: int = 10) -> Dict:
        """Return free slots in THIS box only."""
        from Backup.back_end.slot_monitor.db_interface import _BOX_ID
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT l.lid, l.x, l.y FROM locations l
                    LEFT JOIN phone_storage ps ON l.lid=ps.lid AND ps.retrieved_at IS NULL
                    WHERE ps.pid IS NULL
                      AND l.box_id = %s
                    ORDER BY l.lid LIMIT %s;
                """, (_BOX_ID, limit))
                rows = cur.fetchall()
            return {
                "status":    "success",
                "locations": [{"lid": r[0], "x": r[1], "y": r[2]} for r in rows],
                "count":     len(rows),
            }
        except Exception as exc:
            logger.error(f"[SlotOps] get_empty_locations: {exc}")
            return {"status": "error", "message": str(exc)}
        finally:
            put_conn(conn)