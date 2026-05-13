from __future__ import annotations

import os

import pymongo

# ──────────────────────────────────────────────────────────────
# 旧 PostgreSQL（spider / export_md.py 还在用）
# 依赖 psycopg2，仅在调用方真正连库时才会触发 import
# ──────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres",
    "password": "Super@1997",
}
DB_NAME = "westlaw"


def connect_db():
    import psycopg2  # noqa: PLC0415
    return psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)


# ──────────────────────────────────────────────────────────────
# 新 MongoDB（html2md_pipeline.py 清洗管线）
# ──────────────────────────────────────────────────────────────
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.environ.get("MONGO_DB", "westlaw_spider")
DOC_ITEMS_COLLECTION = os.environ.get("MONGO_DOC_ITEMS", "doc_items_trademark")


def get_mongo_client() -> pymongo.MongoClient:
    return pymongo.MongoClient(MONGO_URI)


def get_doc_items_collection(client: pymongo.MongoClient | None = None):
    cli = client or get_mongo_client()
    return cli[MONGO_DB_NAME][DOC_ITEMS_COLLECTION]
