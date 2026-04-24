from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extras import Json

from script.db import DB_CONFIG, DB_NAME
from script.project_paths import LIST_ITEMS_FILE, ROOT_DIR
HERE = ROOT_DIR


def create_database():
    conn = psycopg2.connect(dbname="postgres", **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{DB_NAME}"')
            print(f"Database '{DB_NAME}' created.")
        else:
            print(f"Database '{DB_NAME}' already exists.")
    conn.close()


def create_table_and_seed():
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS westlaw (
                id SERIAL PRIMARY KEY
            )
            """
        )
        cur.execute("SELECT COUNT(*) FROM westlaw")
        if cur.fetchone()[0] == 0:
            cur.executemany("INSERT INTO westlaw DEFAULT VALUES", [()] * 5)
            print("Inserted 5 initial rows.")
        else:
            print("Table already has data, skipping seed.")
    conn.close()


def create_doc_items_table():
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS doc_items (
                id SERIAL PRIMARY KEY,
                rank INTEGER,
                view_rank INTEGER,
                doc_link TEXT,
                doc_url TEXT,
                title TEXT,
                doc_guid TEXT,
                content_type TEXT,
                citation TEXT,
                court_line TEXT,
                title_description TEXT,
                summary TEXT,
                key_number_hierarchy_index INTEGER,
                headnote TEXT,
                case_preview TEXT,
                case_document_guid TEXT,
                suppress_check_box BOOLEAN,
                suppress_document_link BOOLEAN,
                headnote_date TEXT,
                number_citations INTEGER,
                headnote_citing_references_link TEXT,
                headnote_guid TEXT,
                result_type_code TEXT,
                type_view_name TEXT,
                riflag JSONB,
                status INTEGER DEFAULT 0,
                search_key_number TEXT DEFAULT '291k2055',
                doc_html TEXT,
                doc_md TEXT,
                is_primary BOOLEAN DEFAULT FALSE,
                create_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        print("Table 'doc_items' created.")
    conn.close()


def ensure_table_timestamps(cur, table_name):
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = %s
        )
        """,
        (table_name,),
    )
    if not cur.fetchone()[0]:
        return

    cur.execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN IF NOT EXISTS create_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        """
    )
    cur.execute(
        f"""
        ALTER TABLE {table_name}
        ADD COLUMN IF NOT EXISTS update_time TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        """
    )
    cur.execute(
        f"""
        UPDATE {table_name}
        SET create_time = COALESCE(create_time, CURRENT_TIMESTAMP),
            update_time = COALESCE(update_time, CURRENT_TIMESTAMP)
        WHERE create_time IS NULL OR update_time IS NULL
        """
    )

    trigger_name = f"{table_name}_set_update_time"
    cur.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_name}")
    cur.execute(
        f"""
        CREATE TRIGGER {trigger_name}
        BEFORE UPDATE ON {table_name}
        FOR EACH ROW
        EXECUTE FUNCTION set_update_time()
        """
    )


def ensure_doc_items_schema():
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION set_update_time()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.create_time IS NULL THEN
                    NEW.create_time = CURRENT_TIMESTAMP;
                END IF;
                NEW.update_time = CURRENT_TIMESTAMP;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )

        cur.execute("ALTER TABLE IF EXISTS doc_items ADD COLUMN IF NOT EXISTS doc_html TEXT")
        cur.execute("ALTER TABLE IF EXISTS doc_items ADD COLUMN IF NOT EXISTS doc_md TEXT")
        cur.execute(
            "ALTER TABLE IF EXISTS doc_items ADD COLUMN IF NOT EXISTS is_primary BOOLEAN DEFAULT FALSE"
        )
        # html2md_pipeline.py 写入的清洗产物（只保留 md 的长度字段）
        cur.execute("ALTER TABLE IF EXISTS doc_items ADD COLUMN IF NOT EXISTS doc_html_clean TEXT")
        cur.execute("ALTER TABLE IF EXISTS doc_items ADD COLUMN IF NOT EXISTS doc_md_clean TEXT")
        cur.execute("ALTER TABLE IF EXISTS doc_items ADD COLUMN IF NOT EXISTS doc_md_clean_len INTEGER")
        # 清洗处理状态：0 待处理 / 1 处理中 / 2 成功 / 3 失败
        cur.execute(
            "ALTER TABLE IF EXISTS doc_items "
            "ADD COLUMN IF NOT EXISTS clean_process_status INTEGER DEFAULT 0"
        )
        cur.execute(
            "ALTER TABLE IF EXISTS doc_items "
            "ADD COLUMN IF NOT EXISTS clean_process_fail_reason TEXT"
        )
        # 分块处理状态（与 chunks 表解耦，独立落在 doc_items 上）
        cur.execute(
            "ALTER TABLE IF EXISTS doc_items "
            "ADD COLUMN IF NOT EXISTS chunk_process_status INTEGER DEFAULT 0"
        )
        cur.execute(
            "ALTER TABLE IF EXISTS doc_items "
            "ADD COLUMN IF NOT EXISTS chunk_process_fail_reason TEXT"
        )
        cur.execute(
            "ALTER TABLE IF EXISTS doc_items "
            "ADD COLUMN IF NOT EXISTS chunk_count INTEGER"
        )

        # 旧列迁移：process_status → clean_process_status（若旧列仍存在，把值搬过去后删掉）
        rename_map = {
            "process_status": "clean_process_status",
            "process_fail_reason": "clean_process_fail_reason",
        }
        for old, new in rename_map.items():
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'doc_items' AND column_name = %s",
                (old,),
            )
            if cur.fetchone():
                cur.execute(f"UPDATE doc_items SET {new} = {old} WHERE {old} IS NOT NULL")
                cur.execute(f"ALTER TABLE doc_items DROP COLUMN {old}")

        # 丢弃不再维护的长度字段
        cur.execute("ALTER TABLE IF EXISTS doc_items DROP COLUMN IF EXISTS doc_html_len")
        cur.execute("ALTER TABLE IF EXISTS doc_items DROP COLUMN IF EXISTS doc_html_clean_len")
        cur.execute("ALTER TABLE IF EXISTS doc_items_test DROP COLUMN IF EXISTS doc_html_len")

        ensure_table_timestamps(cur, "doc_items")
        ensure_table_timestamps(cur, "doc_items_test")
        print("Ensured doc_items schema (dropped *_len, renamed process_* → clean_process_*).")
    conn.close()


def create_chunks_table():
    """父子分块 + BGE-M3 向量索引（pgvector, 1024 维）。"""
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id               BIGSERIAL PRIMARY KEY,
                doc_id           INTEGER NOT NULL REFERENCES doc_items(id) ON DELETE CASCADE,
                parent_id        BIGINT REFERENCES chunks(id) ON DELETE CASCADE,
                level            SMALLINT NOT NULL,
                heading          TEXT,
                chunk_order      INTEGER,
                content          TEXT NOT NULL,
                token_count      INTEGER,
                char_start       INTEGER,
                char_end         INTEGER,
                embedding        vector(1024),
                embed_model      TEXT,
                create_time      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT chunks_level_chk CHECK (level IN (0, 1)),
                CONSTRAINT chunks_parent_shape_chk CHECK (
                    (level = 0 AND parent_id IS NULL)
                    OR (level = 1 AND parent_id IS NOT NULL)
                )
            )
            """
        )
        cur.execute("ALTER TABLE IF EXISTS chunks DROP COLUMN IF EXISTS heading_order")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_order ON chunks(doc_id, chunk_order)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_level ON chunks(level)")
        # 向量索引：HNSW + cosine，仅对子块有数据
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
            ON chunks USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            """
        )
        ensure_table_timestamps(cur, "chunks")
        print("Ensured chunks table + vector(1024) HNSW index + doc_items.chunk_* columns.")
    conn.close()


def create_chunks_test_table():
    """pgvector 未装时的占位 test 表：embedding 用 REAL[] 存。

    待 pgvector 就位后可 ALTER：
        ALTER TABLE chunks_test ALTER COLUMN embedding TYPE vector(1024)
            USING embedding::vector;
    """
    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks_test (
                id               BIGSERIAL PRIMARY KEY,
                doc_id           INTEGER NOT NULL REFERENCES doc_items(id) ON DELETE CASCADE,
                parent_id        BIGINT REFERENCES chunks_test(id) ON DELETE CASCADE,
                level            SMALLINT NOT NULL,
                heading          TEXT,
                chunk_order      INTEGER,
                content          TEXT NOT NULL,
                token_count      INTEGER,
                char_start       INTEGER,
                char_end         INTEGER,
                embedding        REAL[],
                embed_model      TEXT,
                create_time      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                update_time      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT chunks_test_level_chk CHECK (level IN (0, 1)),
                CONSTRAINT chunks_test_parent_shape_chk CHECK (
                    (level = 0 AND parent_id IS NULL)
                    OR (level = 1 AND parent_id IS NOT NULL)
                )
            )
            """
        )
        cur.execute("ALTER TABLE IF EXISTS chunks_test DROP COLUMN IF EXISTS heading_order")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_test_doc ON chunks_test(doc_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_test_doc_order ON chunks_test(doc_id, chunk_order)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_test_parent ON chunks_test(parent_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_test_level ON chunks_test(level)")
        ensure_table_timestamps(cur, "chunks_test")
        print("Ensured chunks_test (REAL[] placeholder; switch to vector(1024) when pgvector ready).")
    conn.close()


def seed_doc_items():
    list_items_path = LIST_ITEMS_FILE
    with list_items_path.open("r", encoding="utf-8") as f:
        items = json.load(f)

    conn = psycopg2.connect(dbname=DB_NAME, **DB_CONFIG)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM doc_items")
        if cur.fetchone()[0] > 0:
            print("doc_items already has data, skipping seed.")
            conn.close()
            return

        sql = """
            INSERT INTO doc_items (
                rank, view_rank, doc_link, doc_url, title, doc_guid,
                content_type, citation, court_line, title_description,
                summary, key_number_hierarchy_index, headnote, case_preview,
                case_document_guid, suppress_check_box, suppress_document_link,
                headnote_date, number_citations, headnote_citing_references_link,
                headnote_guid, result_type_code, type_view_name, riflag,
                status, search_key_number
            ) VALUES (
                %(rank)s, %(viewRank)s, %(docLink)s, %(docUrl)s, %(title)s, %(docGuid)s,
                %(contentType)s, %(citation)s, %(courtLine)s, %(titleDescription)s,
                %(summary)s, %(keyNumberHierarchyIndex)s, %(headnote)s, %(casePreview)s,
                %(caseDocumentGuid)s, %(suppressCheckBox)s, %(suppressDocumentLink)s,
                %(headnoteDate)s, %(numberCitations)s, %(headnoteCitingReferencesLink)s,
                %(headnoteGuid)s, %(resultTypeCode)s, %(typeViewName)s, %(riflag)s,
                %(status)s, %(searchKeyNumber)s
            )
        """
        for item in items:
            params = {
                "rank": item.get("rank"),
                "viewRank": item.get("viewRank"),
                "docLink": item.get("docLink"),
                "docUrl": item.get("docUrl"),
                "title": item.get("title"),
                "docGuid": item.get("docGuid"),
                "contentType": item.get("contentType"),
                "citation": item.get("citation"),
                "courtLine": item.get("courtLine"),
                "titleDescription": item.get("titleDescription"),
                "summary": item.get("summary"),
                "keyNumberHierarchyIndex": item.get("keyNumberHierarchyIndex"),
                "headnote": item.get("headnote"),
                "casePreview": item.get("casePreview"),
                "caseDocumentGuid": item.get("caseDocumentGuid"),
                "suppressCheckBox": item.get("suppressCheckBox"),
                "suppressDocumentLink": item.get("suppressDocumentLink"),
                "headnoteDate": item.get("headnoteDate"),
                "numberCitations": item.get("numberCitations"),
                "headnoteCitingReferencesLink": item.get("headnoteCitingReferencesLink"),
                "headnoteGuid": item.get("headnoteGuid"),
                "resultTypeCode": item.get("resultTypeCode"),
                "typeViewName": item.get("typeViewName"),
                "riflag": Json(item["riflag"]) if item.get("riflag") else None,
                "status": 0,
                "searchKeyNumber": "291k2055",
            }
            cur.execute(sql, params)

        print(f"Inserted {len(items)} rows into doc_items.")
    conn.close()


if __name__ == "__main__":
    create_database()
    create_table_and_seed()
    create_doc_items_table()
    ensure_doc_items_schema()
    create_chunks_table()
    seed_doc_items()
    print("Done.")
