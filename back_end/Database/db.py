# back_end/Database/db.py
import logging
import time
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

# B5: per-connection last-used timestamp.
# Key: connection id (id(conn)), Value: last-used monotonic time.
# Only ping if the connection has been idle for more than HEALTH_CHECK_IDLE_S.
_conn_last_used: dict = {}
_HEALTH_CHECK_IDLE_S: float = _DC.HEALTH_CHECK_IDLE_S


def get_conn():
    """
    Acquire a connection from the pool.

    B4 fix: health-check cursor is now properly closed via context manager,
    preventing psycopg2 server-side cursor leaks.

    B5 fix: health-check SELECT 1 only runs when the connection has been idle
    for more than HEALTH_CHECK_IDLE_S (default 30 s). Connections proven
    healthy by recent use skip the ping entirely, cutting DB query latency
    in half under concurrent load.
    """
    conn = _raw_pool.getconn()
    now  = time.monotonic()
    cid  = id(conn)

    last_used = _conn_last_used.get(cid, 0.0)
    idle_s    = now - last_used

    if idle_s >= _HEALTH_CHECK_IDLE_S:
        # B4 + B5: ping only stale connections; use context manager to close cursor
        try:
            with conn.cursor() as cur:   # B4: cursor closed on exit, no leak
                cur.execute("SELECT 1")
        except OperationalError:
            logger.warning(
                "[DB] Stale connection detected — closing and reopening "
                f"(idle {idle_s:.0f}s)"
            )
            try:
                conn.close()
            except Exception:
                pass
            _conn_last_used.pop(cid, None)
            _raw_pool.putconn(conn, close=True)
            conn = _raw_pool.getconn()
            # Record the new connection's cid immediately
            _conn_last_used[id(conn)] = time.monotonic()
    # else: connection was used recently — skip the round-trip entirely (B5)

    return conn


def put_conn(conn):
    """Return a connection to the pool and record the return time."""
    _conn_last_used[id(conn)] = time.monotonic()
    _raw_pool.putconn(conn)
