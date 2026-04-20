from psycopg2 import pool
from back_end.secrets import Secrets
from back_end.config import DatabaseConfig as _DC

# Edit back_end/config.py → DatabaseConfig to change the connection settings.
db_pool = pool.SimpleConnectionPool(
    _DC.SYNC_POOL_MIN, _DC.SYNC_POOL_MAX,
    host=_DC.SYNC_HOST,
    port=_DC.SYNC_PORT,
    database=_DC.SYNC_DATABASE,
    user=Secrets.DB_USER,
    password=Secrets.DB_PASSWORD,
)

def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)
