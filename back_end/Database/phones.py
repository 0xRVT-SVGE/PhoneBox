# back_end/Database/phones.py
from datetime import datetime, timezone as _tz
from Backup.back_end.Database.db import get_conn, put_conn
import logging

logger = logging.getLogger(__name__)


# ── Datetime helper ───────────────────────────────────────────────────────────

def _to_naive_utc(dt):
    """
    Strip timezone info from a datetime, converting to UTC first when needed.

    PostgreSQL TIMESTAMPTZ columns return timezone-aware datetimes in psycopg2,
    while input strings from Flutter (ISO 8601 without tz offset) parse as
    naive.  Mixing the two in a comparison raises:
        TypeError: can't compare offset-naive and offset-aware datetimes
    This helper normalises everything to naive UTC before any comparison.
    """
    if dt is None:
        return None
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        return dt.astimezone(_tz.utc).replace(tzinfo=None)
    return dt


# ------------------ CRUD ------------------
def create_phone(data):
    """Create a new phone (no location assignment)"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        INSERT INTO phones (sid, model, imei, cond, admin_note, stud_note,
                                            phone_size, allowed_box_slugs)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING pid;
                        """, (
                            data["sid"],
                            data["model"],
                            data["imei"],
                            data.get("cond"),
                            data.get("admin_note"),
                            data.get("stud_note"),
                            data.get("phone_size", "standard"),
                            data.get("allowed_box_slugs"),   # None = no restriction
                        ))
            pid = cur.fetchone()[0]
            conn.commit()

            logger.info(f"Created phone {pid} for student {data['sid']}, IMEI: {data['imei']}")
            return {"status": "success", "data": {"pid": pid}}, 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create phone: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


def get_phones(sid):
    """Get all phones for a student with current storage location if stored"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT p.*,
                               ps.lid,
                               ps.stored_at,
                               l.x,
                               l.y,
                               (ps.pid IS NOT NULL) AS is_stored
                        FROM phones p
                        LEFT JOIN phone_storage ps
                               ON ps.pid = p.pid AND ps.retrieved_at IS NULL
                        LEFT JOIN locations l ON l.lid = ps.lid
                        WHERE p.sid = %s;
                        """, (sid,))
            rows = cur.fetchall()

            if not rows:
                logger.warning(f"No phones found for student {sid}")
                return {"status": "error", "message": "No phones found"}, 404

            columns = [desc[0] for desc in cur.description]
            data = [dict(zip(columns, r)) for r in rows]

            for item in data:
                if item.get('stored_at'):
                    item['stored_at'] = item['stored_at'].isoformat()
                if item.get('created_at'):
                    item['created_at'] = item['created_at'].isoformat()
                if item.get('modified_at'):
                    item['modified_at'] = item['modified_at'].isoformat()

            logger.debug(f"Retrieved {len(data)} phone(s) for student {sid}")
            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def list_phones():
    """List all phones with current storage status"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT p.*,
                               ps.lid,
                               ps.stored_at,
                               l.x,
                               l.y,
                               (ps.pid IS NOT NULL) AS is_stored
                        FROM phones p
                        LEFT JOIN phone_storage ps
                               ON ps.pid = p.pid AND ps.retrieved_at IS NULL
                        LEFT JOIN locations l ON l.lid = ps.lid
                        ORDER BY p.sid;
                        """)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

            data = [dict(zip(columns, r)) for r in rows]
            for item in data:
                if item.get('stored_at'):
                    item['stored_at'] = item['stored_at'].isoformat()
                if item.get('created_at'):
                    item['created_at'] = item['created_at'].isoformat()
                if item.get('modified_at'):
                    item['modified_at'] = item['modified_at'].isoformat()

            logger.debug(f"Listed {len(rows)} phones")
            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def update_phone(pid, data):
    """Update phone details (NOT storage status)"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        UPDATE phones
                        SET model              = COALESCE(%s, model),
                            imei               = COALESCE(%s, imei),
                            cond               = COALESCE(%s, cond),
                            admin_note         = COALESCE(%s, admin_note),
                            stud_note          = COALESCE(%s, stud_note),
                            phone_size         = COALESCE(%s, phone_size),
                            allowed_box_slugs  = CASE
                                WHEN %s IS NULL THEN allowed_box_slugs
                                WHEN %s = '{}' THEN NULL
                                ELSE %s
                            END
                        WHERE pid = %s;
                        """, (
                            data.get("model"),
                            data.get("imei"),
                            data.get("cond"),
                            data.get("admin_note"),
                            data.get("stud_note"),
                            data.get("phone_size"),
                            # allowed_box_slugs: pass list to set, [] to clear to NULL, omit to leave unchanged
                            data.get("allowed_box_slugs"),
                            data.get("allowed_box_slugs"),
                            data.get("allowed_box_slugs"),
                            pid
                        ))

            if cur.rowcount == 0:
                logger.warning(f"Phone {pid} not found for update")
                return {"status": "error", "message": "Phone not found"}, 404

            conn.commit()
            logger.info(f"Updated phone {pid}")
            return {"status": "success", "data": {"pid": pid}}, 200
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update phone {pid}: {str(e)}")
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


def delete_phone(pid):
    """Delete a phone (will also cascade delete storage records)"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT 1
                        FROM phone_storage
                        WHERE pid = %s
                          AND retrieved_at IS NULL
                        LIMIT 1;
                        """, (pid,))

            if cur.fetchone():
                return {
                    "status": "error",
                    "message": "Cannot delete phone while it's in storage. Please withdraw it first."
                }, 400

            cur.execute("SELECT imei FROM phones WHERE pid = %s", (pid,))
            result = cur.fetchone()
            if not result:
                return {"status": "error", "message": "Phone not found"}, 404

            imei = result[0]

            cur.execute("DELETE FROM phones WHERE pid = %s RETURNING pid;", (pid,))
            conn.commit()

            logger.info(f"Deleted phone {pid} (IMEI: {imei})")
            return {"status": "success", "data": {"pid": pid}}, 200
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to delete phone {pid}: {str(e)}")
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


# ------------------ Advanced ------------------
def phones_not_stored():
    """Get all phones not currently in storage"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT p.*
                        FROM phones p
                        WHERE NOT EXISTS (
                            SELECT 1
                            FROM phone_storage ps
                            WHERE ps.pid = p.pid
                              AND ps.retrieved_at IS NULL
                        )
                        ORDER BY p.sid;
                        """)
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

            data = [dict(zip(columns, r)) for r in rows]
            for item in data:
                if item.get('created_at'):
                    item['created_at'] = item['created_at'].isoformat()
                if item.get('modified_at'):
                    item['modified_at'] = item['modified_at'].isoformat()

            logger.debug(f"Retrieved {len(rows)} phones not in storage")
            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def phones_by_condition(cond):
    """Get phones by condition"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT p.*,
                               ps.lid,
                               ps.stored_at,
                               l.x,
                               l.y,
                               (ps.pid IS NOT NULL) AS is_stored
                        FROM phones p
                        LEFT JOIN phone_storage ps
                               ON ps.pid = p.pid AND ps.retrieved_at IS NULL
                        LEFT JOIN locations l ON l.lid = ps.lid
                        WHERE p.cond = %s
                        ORDER BY p.sid;
                        """, (cond,))
            rows = cur.fetchall()

            if not rows:
                logger.debug(f"No phones found with condition '{cond}'")
                return {
                    "status": "success",
                    "data": [],
                    "message": f"No phones with condition '{cond}'"
                }, 200

            columns = [desc[0] for desc in cur.description]
            data = [dict(zip(columns, r)) for r in rows]
            for item in data:
                if item.get('stored_at'):
                    item['stored_at'] = item['stored_at'].isoformat()
                if item.get('created_at'):
                    item['created_at'] = item['created_at'].isoformat()
                if item.get('modified_at'):
                    item['modified_at'] = item['modified_at'].isoformat()

            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def phone_stats():
    """Get phone statistics"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT COUNT(*) AS total,
                               COUNT(*) FILTER (
                                   WHERE EXISTS (
                                       SELECT 1
                                       FROM phone_storage ps
                                       WHERE ps.pid = p.pid
                                         AND ps.retrieved_at IS NULL
                                   )
                               ) AS stored,
                               COUNT(*) FILTER (
                                   WHERE NOT EXISTS (
                                       SELECT 1
                                       FROM phone_storage ps
                                       WHERE ps.pid = p.pid
                                         AND ps.retrieved_at IS NULL
                                   )
                               ) AS not_stored,
                               COUNT(*) FILTER (WHERE p.cond = 'Damaged') AS damaged,
                               COUNT(*) FILTER (WHERE p.cond = 'Broken') AS broken
                        FROM phones p;
                        """)
            result = cur.fetchone()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": dict(zip(columns, result))}, 200
    finally:
        put_conn(conn)


def reassign_phone(pid, new_sid):
    """Reassign a phone to a different student"""
    if not pid or not new_sid:
        return {"status": "error", "message": "pid and new_sid are required"}, 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT sid FROM phones WHERE pid = %s", (pid,))
            result = cur.fetchone()
            if not result:
                return {"status": "error", "message": "Phone not found for the given pid"}, 404
            old_sid = result[0]

            cur.execute("SELECT sid FROM students WHERE sid = %s", (new_sid,))
            if not cur.fetchone():
                return {"status": "error", "message": "New student not found"}, 404

            cur.execute(
                "UPDATE phones SET sid = %s WHERE pid = %s RETURNING pid;",
                (new_sid, pid),
            )
            conn.commit()

            logger.info(f"Reassigned phone {pid} from {old_sid} to {new_sid}")
            return {
                "status": "success",
                "data": {"pid": pid, "old_owner": old_sid, "new_owner": new_sid},
            }, 200
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to reassign phone {pid}: {str(e)}")
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


def get_phone_storage_history(pid):
    """Get storage history for a phone"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM get_phone_storage_history(%s)", (pid,))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

            data = [dict(zip(columns, r)) for r in rows]
            for item in data:
                if item.get('stored_at'):
                    item['stored_at'] = item['stored_at'].isoformat()
                if item.get('retrieved_at'):
                    item['retrieved_at'] = item['retrieved_at'].isoformat()
                if item.get('duration'):
                    item['duration'] = str(item['duration'])

            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def get_phone_operation_history(pid, limit=50):
    """Get operation history for a phone from audit log"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT operation,
                               timestamp,
                               old_data,
                               new_data
                        FROM phone_operations
                        WHERE pid = %s
                        ORDER BY timestamp DESC
                        LIMIT %s;
                        """, (pid, limit))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

            data = [dict(zip(columns, r)) for r in rows]
            for item in data:
                if item.get('timestamp'):
                    item['timestamp'] = item['timestamp'].isoformat()

            logger.debug(f"Retrieved {len(rows)} operations for phone {pid}")
            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def get_activity_report(from_dt_str: str, to_dt_str: str) -> tuple:
    """
    Return a structured phone-movement report for the given interval.

    Three categories:
      deposited_and_withdrawn  -- both stored_at and retrieved_at inside [from, to]
      withdrawn_only           -- retrieved_at inside interval, stored_at outside
      deposited_only           -- stored_at inside interval, retrieved_at outside or NULL

    Timezone handling
    -----------------
    PostgreSQL TIMESTAMPTZ columns return timezone-aware datetimes via psycopg2.
    Input strings from Flutter are naive ISO 8601 (no offset).  Comparing the
    two directly raises TypeError.  All values are normalised to naive UTC via
    _to_naive_utc() before any comparison.
    """
    try:
        from_dt = datetime.fromisoformat(from_dt_str)
        to_dt   = datetime.fromisoformat(to_dt_str)
    except (ValueError, TypeError) as e:
        return {"status": "error", "message": f"Invalid datetime format: {e}"}, 400

    # Normalise input to naive UTC
    from_dt = _to_naive_utc(from_dt)
    to_dt   = _to_naive_utc(to_dt)

    if from_dt >= to_dt:
        return {"status": "error", "message": "'from' must be before 'to'"}, 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    ps.pid,
                    p.model,
                    ps.stored_at,
                    ps.retrieved_at,
                    p.sid,
                    s.first_name,
                    s.last_name
                FROM phone_storage ps
                JOIN phones  p ON ps.pid  = p.pid
                JOIN students s ON p.sid  = s.sid
                WHERE
                    (ps.stored_at     >= %(f)s AND ps.stored_at     <= %(t)s)
                    OR
                    (ps.retrieved_at IS NOT NULL
                     AND ps.retrieved_at >= %(f)s
                     AND ps.retrieved_at <= %(t)s)
                ORDER BY p.sid, COALESCE(ps.stored_at, ps.retrieved_at)
            """, {"f": from_dt, "t": to_dt})

            rows    = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            records = [dict(zip(columns, row)) for row in rows]

        deposited_and_withdrawn = []
        withdrawn_only          = []
        deposited_only          = []

        for r in records:
            # Normalise DB datetimes before comparison
            sa = _to_naive_utc(r["stored_at"])
            ra = _to_naive_utc(r["retrieved_at"])

            stored_in    = sa is not None and from_dt <= sa <= to_dt
            withdrawn_in = ra is not None and from_dt <= ra <= to_dt

            entry = {
                "pid":          r["pid"],
                "model":        r["model"] or "Unknown",
                "sid":          r["sid"],
                "first_name":   r["first_name"] or "",
                "last_name":    r["last_name"]  or "",
                "stored_at":    r["stored_at"].isoformat()    if r["stored_at"]    else None,
                "retrieved_at": r["retrieved_at"].isoformat() if r["retrieved_at"] else None,
            }

            if stored_in and withdrawn_in:
                deposited_and_withdrawn.append(entry)
            elif withdrawn_in and not stored_in:
                withdrawn_only.append(entry)
            elif stored_in and not withdrawn_in:
                deposited_only.append(entry)

        return {
            "status": "success",
            "data": {
                "from":                     from_dt.isoformat(),
                "to":                       to_dt.isoformat(),
                "deposited_and_withdrawn":  deposited_and_withdrawn,
                "withdrawn_only":           withdrawn_only,
                "deposited_only":           deposited_only,
            },
        }, 200

    except Exception as e:
        logger.error(f"Activity report error: {e}")
        return {"status": "error", "message": str(e)}, 500
    finally:
        put_conn(conn)