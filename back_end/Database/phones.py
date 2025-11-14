# back_end/Database/phones.py
from back_end.Database.db import get_conn, put_conn

# ------------------ CRUD ------------------
def create_phone(data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO phones (sid, brand, model, imei, cond, admin_note, stud_note, is_stored)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING pid;
            """, (
                data["sid"], data["brand"], data["model"], data.get("imei"),
                data.get("cond"), data.get("admin_note"), data.get("stud_note"),
                data.get("is_stored", False)
            ))
            pid = cur.fetchone()[0]
            conn.commit()
            return {"status": "success", "data": {"pid": pid}}, 201
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)

def get_phone(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM phones WHERE sid = %s;", (sid,))
            row = cur.fetchone()
            if not row:
                return {"status": "error", "message": "Phone not found"}, 404
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": dict(zip(columns, row))}, 200
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

def update_phone(sid, data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE phones
                SET brand = COALESCE(%s, brand),
                    model = COALESCE(%s, model),
                    imei = COALESCE(%s, imei),
                    cond = COALESCE(%s, cond),
                    admin_note = COALESCE(%s, admin_note),
                    stud_note = COALESCE(%s, stud_note),
                    is_stored = COALESCE(%s, is_stored)
                WHERE sid = %s
                RETURNING pid;
            """, (
                data.get("brand"), data.get("model"), data.get("imei"), data.get("cond"),
                data.get("admin_note"), data.get("stud_note"), data.get("is_stored"), sid
            ))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"sid": sid}}, 200
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)

def delete_phone(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM phones WHERE sid = %s RETURNING sid;", (sid,))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"sid": sid}}, 200
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

def reassign_phone(old_sid, new_sid):
    if not old_sid or not new_sid:
        return {"status": "error", "message": "old_sid and new_sid are required"}, 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE phones SET sid = %s WHERE sid = %s RETURNING pid;", (new_sid, old_sid))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found for the given old_sid"}, 404
            conn.commit()
            return {"status": "success", "data": {"new_owner": new_sid}}, 200
    finally:
        put_conn(conn)

def regenerate_pid(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE phones SET pid = NULL WHERE sid = %s RETURNING sid;", (sid,))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Phone not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"sid": sid}, "message": "PID regenerated"}, 200
    finally:
        put_conn(conn)
