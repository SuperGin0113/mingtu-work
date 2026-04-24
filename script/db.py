from __future__ import annotations

import psycopg2

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "Super@1997",
}
DB_NAME = "westlaw"


def connect_db():
    return psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
