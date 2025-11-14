# back_end/Database/students.py
from back_end.Database.db import get_conn, put_conn

# ------------------ CRUD ------------------
def create_student(data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO students (sid, last_name, first_name, embed, location)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING sid;
            """, (
                data["sid"],
                data.get("last_name"),
                data["first_name"],
                data["embed"],
                data.get("location")
            ))
            sid = cur.fetchone()[0]
            conn.commit()
            return {"status": "success", "data": {"sid": sid}}, 201
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)

def get_student(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students WHERE sid = %s;", (sid,))
            row = cur.fetchone()
            if not row:
                return {"status": "error", "message": "Student not found"}, 404
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": dict(zip(columns, row))}, 200
    finally:
        put_conn(conn)

def list_students():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students ORDER BY sid;")
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)

def update_student(sid, data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE students
                SET last_name = COALESCE(%s, last_name),
                    first_name = COALESCE(%s, first_name),
                    embed = COALESCE(%s, embed),
                    location = COALESCE(%s, location)
                WHERE sid = %s
                RETURNING sid;
            """, (
                data.get("last_name"),
                data.get("first_name"),
                data.get("embed"),
                data.get("location"),
                sid
            ))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Student not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"sid": sid}}, 200
    except Exception as e:
        conn.rollback()
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)

def delete_student(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM students WHERE sid = %s RETURNING sid;", (sid,))
            if cur.rowcount == 0:
                return {"status": "error", "message": "Student not found"}, 404
            conn.commit()
            return {"status": "success", "data": {"sid": sid}}, 200
    finally:
        put_conn(conn)

# ------------------ ADVANCED ------------------
def search_students(query):
    if not query:
        return {"status": "error", "message": "Missing search query"}, 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM students
                WHERE sid ILIKE %s OR first_name ILIKE %s OR last_name ILIKE %s
                ORDER BY sid;
            """, (f"%{query}%", f"%{query}%", f"%{query}%"))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)

def students_near_location(x, y, limit=10):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *, sqrt(power(location[1] - %s, 2) + power(location[2] - %s, 2)) AS distance
                FROM students
                ORDER BY distance
                LIMIT %s;
            """, (x, y, limit))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)

def recently_modified_students(since):
    if not since:
        return {"status": "error", "message": "Missing 'since' timestamp"}, 400
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM students
                WHERE modified_at > %s
                ORDER BY modified_at DESC;
            """, (since,))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)
