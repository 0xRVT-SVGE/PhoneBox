# back_end/Database/db.py
import logging
import psycopg2
from psycopg2 import pool, OperationalError
from back_end.secrets import Secrets
from back_end.config import DatabaseConfig as _DC

logger = logging.getLogger(__name__)

# ── Raw connection pool ───────────────────────────────────────────────────────
_raw_pool = pool.ThreadedConnectionPool(
    _DC.SYNC_POOL_MIN, _DC.SYNC_POOL_MAX,
    host=_DC.SYNC_HOST,
    port=_DC.SYNC_PORT,
    database=_DC.SYNC_DATABASE,
    user=Secrets.DB_USER,
    password=Secrets.DB_PASSWORD,
)


def get_conn():
    """
    Acquire a connection from the pool.

    Optimization #37: health-check on acquire.  If the connection is stale
    (e.g. PostgreSQL restarted), psycopg2 raises OperationalError on the
    ping; we close and recreate it so the caller always gets a live
    connection without needing a process restart.
    """
    conn = _raw_pool.getconn()
    try:
        # Cheap server round-trip — aborts immediately if the socket is dead
        conn.cursor().execute("SELECT 1")
    except OperationalError:
        logger.warning(
            "[DB] Stale connection detected — closing and reopening"
        )
        try:
            conn.close()
        except Exception:
            pass
        # Force psycopg2 to create a fresh connection on the next getconn call
        _raw_pool.putconn(conn, close=True)
        conn = _raw_pool.getconn()
    return conn


def put_conn(conn):
    """Return a connection to the pool."""
    _raw_pool.putconn(conn)