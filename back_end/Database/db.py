from psycopg2 import pool

db_pool = pool.SimpleConnectionPool(
    1, 10,
    host="localhost",
    port=5432,
    database="PhoneBoxDB",
    user="admin",
    password="admin"
)

def get_conn():
    return db_pool.getconn()

def put_conn(conn):
    db_pool.putconn(conn)
