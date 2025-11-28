# back_end/Database/phones.py
from back_end.Database.db import get_conn, put_conn

# ------------------ CRUD ------------------
def create_phone(data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO phones (sid, model, imei, cond, admin_note, stud_note, is_stored, location)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING pid;
            """, (
                data["sid"], data["model"], data.get("imei"),
                data.get("cond"), data.get("admin_note"), data.get("stud_note"),
                data.get("is_stored", False), data.get("location") if data.get("location") else None  # allow NULL; trigger will auto-assign
            ))
            pid = cur.fetchone()[0]
            conn.commit()
            return {"status": "success", "data": {"pid": pid}}, 201
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


def get_phones(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM phones WHERE sid = %s;", (sid,))
            rows = cur.fetchall()

            if not rows:
                return {"status": "error", "message": "No phones found"}, 404

            columns = [desc[0] for desc in cur.description]
            data = [dict(zip(columns, r)) for r in rows]

            return {"status": "success", "data": data}, 200
    finally:
        put_conn(conn)


def list_phones():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM phones ORDER BY sid;")
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)


def update_phone(pid, data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE phones
                SET model = COALESCE(%s, model),
                    imei = COALESCE(%s, imei),
                    cond = COALESCE(%s, cond),
                    admin_note = COALESCE(%s, admin_note),
                    stud_note = COALESCE(%s, stud_note),
                    is_stored = COALESCE(%s, is_stored),
                    location = COALESCE(%s, location)
                WHERE pid = %s
                RETURNING pid;
            """, (
                data.get("model"), data.get("imei"), data.get("cond"),
                data.get("admin_note"), data.get("stud_note"), data.get("is_stored"), data.get("location"), pid
            ))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found"}, 404

            conn.commit()
            return {"status": "success", "data": {"pid": pid}}, 200
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)

# Delete a phone by pid
def delete_phone(pid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM phones WHERE pid = %s RETURNING pid;", (pid,))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"pid": pid}}, 200
    finally:
        put_conn(conn)


# ------------------ Advanced ------------------
def phones_not_stored():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM phones WHERE is_stored = FALSE ORDER BY sid;")
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)


def phones_by_condition(cond):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM phones WHERE cond = %s ORDER BY sid;", (cond,))
            rows = cur.fetchall()
            if not rows:
                return {"status": "success", "data": [], "message": f"No phones with condition '{cond}'"}, 200
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)


def phone_stats():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE is_stored) AS stored,
                       COUNT(*) FILTER (WHERE NOT is_stored) AS in_use,
                       COUNT(*) FILTER (WHERE cond = 'Damaged') AS damaged,
                       COUNT(*) FILTER (WHERE cond = 'Broken') AS broken
                FROM phones;
            """)
            result = cur.fetchone()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": dict(zip(columns, result))}, 200
    finally:
        put_conn(conn)

# Reassign a single phone to a different student (operate on pid)
def reassign_phone(pid, new_sid):
    if not pid or not new_sid:
        return {"status": "error", "message": "pid and new_sid are required"}, 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE phones SET sid = %s WHERE pid = %s RETURNING pid;", (new_sid, pid))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found for the given pid"}, 404

            conn.commit()
            return {"status": "success", "data": {"pid": pid, "new_owner": new_sid}}, 200
    finally:
        put_conn(conn)


def regenerate_pid(pid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE phones SET pid = NULL WHERE pid = %s RETURNING pid;", (pid,))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"pid": pid}, "message": "PID regenerated"}, 200
    finally:
        put_conn(conn)

def phones_near_location(x, y, limit=10):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *, sqrt(power(location[1] - %s, 2) + power(location[2] - %s, 2)) AS distance
                FROM phones
                ORDER BY distance
                LIMIT %s;
            """, (x, y, limit))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200

    finally:
        put_conn(conn)