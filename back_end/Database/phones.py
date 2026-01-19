# back_end/Database/API/phones.py
from back_end.Database.db import get_conn, put_conn
import logging

logger = logging.getLogger(__name__)


# ------------------ CRUD ------------------
def create_phone(data):
    """Create a new phone (no location assignment)"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        INSERT INTO phones (sid, model, imei, cond, admin_note, stud_note)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING pid;
                        """, (
                            data["sid"],
                            data["model"],
                            data["imei"],
                            data.get("cond"),
                            data.get("admin_note"),
                            data.get("stud_note")
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
                               CASE WHEN ps.pid IS NOT NULL THEN TRUE ELSE FALSE END as is_stored
                        FROM phones p
                                 LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL
                                 LEFT JOIN locations l ON ps.lid = l.lid
                        WHERE p.sid = %s;
                        """, (sid,))
            rows = cur.fetchall()

            if not rows:
                logger.warning(f"No phones found for student {sid}")
                return {"status": "error", "message": "No phones found"}, 404

            columns = [desc[0] for desc in cur.description]
            data = [dict(zip(columns, r)) for r in rows]

            # Convert datetime to string
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
                               CASE WHEN ps.pid IS NOT NULL THEN TRUE ELSE FALSE END as is_stored
                        FROM phones p
                                 LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL
                                 LEFT JOIN locations l ON ps.lid = l.lid
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
    """Update phone details (NOT storage status - use deposit/withdraw for that)"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        UPDATE phones
                        SET model      = COALESCE(%s, model),
                            imei       = COALESCE(%s, imei),
                            cond       = COALESCE(%s, cond),
                            admin_note = COALESCE(%s, admin_note),
                            stud_note  = COALESCE(%s, stud_note)
                        WHERE pid = %s;
                        """, (
                            data.get("model"),
                            data.get("imei"),
                            data.get("cond"),
                            data.get("admin_note"),
                            data.get("stud_note"),
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
            # Check if phone is currently stored
            cur.execute("""
                        SELECT lid
                        FROM phone_storage
                        WHERE pid = %s
                          AND retrieved_at IS NULL
                        """, (pid,))

            if cur.fetchone():
                return {
                    "status": "error",
                    "message": "Cannot delete phone while it's in storage. Please withdraw it first."
                }, 400

            # Get phone info before deletion
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
                                 LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL
                        WHERE ps.pid IS NULL
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
                               l.x,
                               l.y,
                               CASE WHEN ps.pid IS NOT NULL THEN TRUE ELSE FALSE END as is_stored
                        FROM phones p
                                 LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL
                                 LEFT JOIN locations l ON ps.lid = l.lid
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
                        SELECT COUNT(*)                                   AS total,
                               COUNT(ps.pid)                              AS stored,
                               COUNT(*) - COUNT(ps.pid)                   AS not_stored,
                               COUNT(*) FILTER (WHERE p.cond = 'Damaged') AS damaged,
                               COUNT(*) FILTER (WHERE p.cond = 'Broken')  AS broken
                        FROM phones p
                                 LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL;
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
            # Get old sid for logging
            cur.execute("SELECT sid FROM phones WHERE pid = %s", (pid,))
            result = cur.fetchone()
            if not result:
                return {"status": "error", "message": "Phone not found for the given pid"}, 404
            old_sid = result[0]

            # Verify new student exists
            cur.execute("SELECT sid FROM students WHERE sid = %s", (new_sid,))
            if not cur.fetchone():
                return {"status": "error", "message": "New student not found"}, 404

            cur.execute("UPDATE phones SET sid = %s WHERE pid = %s RETURNING pid;", (new_sid, pid))
            conn.commit()

            logger.info(f"Reassigned phone {pid} from {old_sid} to {new_sid}")
            return {"status": "success", "data": {"pid": pid, "old_owner": old_sid, "new_owner": new_sid}}, 200
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


def get_phone_with_slot_status(pid):
    """Get phone details with current slot monitoring status"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        SELECT p.*,
                               ps.lid,
                               ps.stored_at,
                               l.x,
                               l.y,
                               s.binary_state,
                               s.tx_state,
                               s.last_distance,
                               s.last_change_ts
                        FROM phones p
                                 LEFT JOIN phone_storage ps ON p.pid = ps.pid AND ps.retrieved_at IS NULL
                                 LEFT JOIN locations l ON ps.lid = l.lid
                                 LEFT JOIN slot_current_state s ON l.lid = s.lid
                        WHERE p.pid = %s;
                        """, (pid,))
            row = cur.fetchone()

            if not row:
                return {"status": "error", "message": "Phone not found"}, 404

            columns = [desc[0] for desc in cur.description]
            data = dict(zip(columns, row))

            # Convert datetime fields
            if data.get('stored_at'):
                data['stored_at'] = data['stored_at'].isoformat()
            if data.get('last_change_ts'):
                data['last_change_ts'] = data['last_change_ts'].isoformat()
            if data.get('created_at'):
                data['created_at'] = data['created_at'].isoformat()
            if data.get('modified_at'):
                data['modified_at'] = data['modified_at'].isoformat()

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