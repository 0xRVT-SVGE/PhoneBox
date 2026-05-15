# back_end/Database/students.py
from back_end.Database.db import get_conn, put_conn
import re
import logging

# Setup logging
logger = logging.getLogger(__name__)


# ------------------ CRUD ------------------
def create_student(data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        INSERT INTO students (sid, last_name, first_name, embed, year_group)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING sid;
                        """, (data["sid"],
                              data.get("last_name"),
                              data["first_name"],
                              data["embed"],
                              data.get("year_group"),   # None = unrestricted
                              ))
            sid = cur.fetchone()[0]
            conn.commit()

            logger.info(f"Created student {sid}: {data.get('first_name')} {data.get('last_name')} "
                        f"(year_group={data.get('year_group')})")
            return {"status": "success", "data": {"sid": sid}}, 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create student: {str(e)}")
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
                logger.warning(f"Student {sid} not found")
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
            logger.debug(f"Listed {len(rows)} students")
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)


def update_student(sid, data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            new_sid = data.get("sid") or None
            cur.execute("""
                        UPDATE students
                        SET sid        = COALESCE(%s, sid),
                            last_name  = COALESCE(%s, last_name),
                            first_name = COALESCE(%s, first_name),
                            embed      = COALESCE(%s, embed),
                            year_group = CASE
                                WHEN %s IS NULL THEN year_group
                                ELSE %s
                            END
                        WHERE sid = %s
                        RETURNING sid;
                        """, (
                            new_sid,
                            data.get("last_name"),
                            data.get("first_name"),
                            data.get("embed"),
                            # year_group: pass an int to set, omit/None to leave unchanged
                            data.get("year_group"),
                            data.get("year_group"),
                            sid
                        ))

            result = cur.fetchone()
            if not result:
                logger.warning(f"Student {sid} not found for update")
                return {"status": "error", "message": "Student not found"}, 404

            conn.commit()
            logger.info(f"Updated student {sid} -> {result[0]}")
            return {"status": "success", "data": {"sid": result[0]}}, 200

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update student {sid}: {str(e)}")
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


def delete_student(sid):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM students WHERE sid = %s RETURNING sid;", (sid,))
            if cur.rowcount == 0:
                logger.warning(f"Student {sid} not found for deletion")
                return {"status": "error", "message": "Student not found"}, 404

            conn.commit()
            logger.info(f"Deleted student {sid}")
            return {"status": "success", "data": {"sid": sid}}, 200
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to delete student {sid}: {str(e)}")
        return {"status": "error", "message": str(e)}, 400
    finally:
        put_conn(conn)


# ------------------ Advanced ------------------
def search_students(query):
    if not query:
        return {"status": "error", "message": "Missing search query"}, 400

    query = query.strip()

    sid_pattern = r"^E\d{4}$"
    name_pattern = r"^[A-Za-z\s]+$"

    is_sid = bool(re.match(sid_pattern, query))
    is_name = bool(re.match(name_pattern, query))

    if not (is_sid or is_name):
        return {"status": "error", "message": "Invalid search query format"}, 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if is_sid:
                cur.execute("""
                            SELECT *
                            FROM students
                            WHERE sid ILIKE %s
                            ORDER BY sid;
                            """, (f"%{query}%",))
            else:
                cur.execute("""
                            SELECT *
                            FROM students
                            WHERE first_name ILIKE %s
                               OR last_name ILIKE %s
                            ORDER BY sid;
                            """, (f"%{query}%", f"%{query}%"))

            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            logger.debug(f"Search '{query}' returned {len(rows)} students")
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
                        SELECT *
                        FROM students
                        WHERE modified_at > %s
                        ORDER BY modified_at DESC;
                        """, (since,))
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            return {"status": "success", "data": [dict(zip(columns, r)) for r in rows]}, 200
    finally:
        put_conn(conn)