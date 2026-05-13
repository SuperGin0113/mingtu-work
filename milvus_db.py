"""
Milvus connection and collection initialization for chunk vectors.

CLI:
    python milvus_db.py --init                  # 建 chunks_test 集合 + HNSW 索引
    python milvus_db.py --init --drop           # 先删后建
    python milvus_db.py --info                  # 查看集合状态
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    Function,
    FunctionType,
    MilvusClient,
    connections,
    utility,
)

load_dotenv()


MILVUS_HOST = os.environ.get("TEST_MILVUS_HOST")
MILVUS_PORT = os.environ.get("TEST_MILVUS_PORT") 
MILVUS_USER = os.environ.get("TEST_MILVUS_USER") 
MILVUS_PASSWORD = os.environ.get("TEST_MILVUS_PASSWORD") or os.environ.get("MILVUS_PASSWORD")
MILVUS_TOKEN = os.environ.get("TEST_MILVUS_TOKEN") or os.environ.get("MILVUS_TOKEN")

DEFAULT_COLLECTION = os.environ.get("MILVUS_COLLECTION", "chunks_test")
EMBED_DIM = 1024
ALIAS = "default"

EXPECTED_FIELDS = {
    "id",
    "doc_guid",
    "heading",
    "content",
    "token_count",
    "chunk_order",
    "char_start",
    "char_end",
    "title",
    "court_line",
    "title_description",
    "create_time",
    "update_time",
    "patent_type",
    "metadata",
    "embed_model",
    "embedding",
    "sparse",
}

CONTENT_ANALYZER = {
    "tokenizer": "standard",
    "filter": [
        "lowercase",
        {"type": "stop", "stop_words": ["_english_"]},
        {"type": "stemmer", "language": "english"},
    ],
}


def milvus_uri() -> str:
    if not MILVUS_HOST or not MILVUS_PORT:
        missing = ", ".join(
            v
            for v, val in [
                ("TEST_MILVUS_HOST", MILVUS_HOST),
                ("TEST_MILVUS_PORT", MILVUS_PORT),
            ]
            if not val
        )
        raise RuntimeError(f"Milvus connection not configured: set {missing} environment variables")
    return f"http://{MILVUS_HOST}:{MILVUS_PORT}"


def _auth_kwargs() -> dict[str, str]:
    if MILVUS_TOKEN:
        return {"token": MILVUS_TOKEN}
    if MILVUS_USER and MILVUS_PASSWORD:
        return {"user": MILVUS_USER, "password": MILVUS_PASSWORD}
    return {}


def connect() -> None:
    if ALIAS in connections.list_connections():
        try:
            connections.disconnect(ALIAS)
        except Exception:
            pass
    connections.connect(alias=ALIAS, uri=milvus_uri(), **_auth_kwargs())


def get_client() -> MilvusClient:
    return MilvusClient(uri=milvus_uri(), **_auth_kwargs())


def build_schema() -> CollectionSchema:
    fields = [
        FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema("doc_guid", DataType.VARCHAR, max_length=64),
        FieldSchema("chunk_order", DataType.INT32),
        FieldSchema("heading", DataType.VARCHAR, max_length=512),
        FieldSchema(
            "content",
            DataType.VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            enable_match=True,
            analyzer_params=CONTENT_ANALYZER,
        ),
        FieldSchema("token_count", DataType.INT32),
        FieldSchema("char_start", DataType.INT64),
        FieldSchema("char_end", DataType.INT64),
        FieldSchema("title", DataType.VARCHAR, max_length=1024),
        FieldSchema("court_line", DataType.VARCHAR, max_length=512),
        FieldSchema("title_description", DataType.VARCHAR, max_length=4096),
        FieldSchema(
            "patent_type",
            DataType.VARCHAR,
            max_length=32,
            nullable=True,
            default_value="design",
        ),
        FieldSchema("create_time", DataType.VARCHAR, max_length=32),
        FieldSchema("update_time", DataType.VARCHAR, max_length=32),
        FieldSchema("metadata", DataType.JSON, nullable=True),
        FieldSchema("embed_model", DataType.VARCHAR, max_length=64),
        FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=EMBED_DIM),
        FieldSchema("sparse", DataType.SPARSE_FLOAT_VECTOR),
    ]
    schema = CollectionSchema(
        fields=fields,
        description="Westlaw chunk vectors with document metadata and character spans",
        enable_dynamic_field=True,
    )
    schema.add_function(
        Function(
            name="bm25_fn",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["sparse"],
        )
    )
    return schema


SCALAR_INDEXES = [
    ("doc_guid", "INVERTED"),
    ("chunk_order", "INVERTED"),
    ("patent_type", "INVERTED"),
    ("content", "INVERTED"),
]


def _validate_schema(col: Collection) -> None:
    field_names = {field.name for field in col.schema.fields}
    missing = sorted(EXPECTED_FIELDS - field_names)
    id_fields = [field for field in col.schema.fields if field.name == "id"]
    id_auto = bool(id_fields and getattr(id_fields[0], "auto_id", False))
    if missing or not id_auto:
        raise RuntimeError(
            "Existing collection schema is not compatible. "
            f"missing={missing}, id_auto_id={id_auto}. "
            "Run `python milvus_db.py --init --drop` or use a new collection name."
        )


def init_collection(name: str = DEFAULT_COLLECTION, drop: bool = False) -> Collection:
    connect()
    exists = utility.has_collection(name, using=ALIAS)
    if exists and drop:
        utility.drop_collection(name, using=ALIAS)
        print(f"[drop] {name}")
        exists = False

    if exists:
        print(f"[skip] collection {name} already exists")
        col = Collection(name, using=ALIAS)
        _validate_schema(col)
    else:
        col = Collection(name=name, schema=build_schema(), using=ALIAS)
        print(f"[create] {name}")

    existing_indexes = {idx.field_name for idx in col.indexes}

    if "embedding" not in existing_indexes:
        col.create_index(
            field_name="embedding",
            index_params={
                "index_type": "HNSW",
                "metric_type": "COSINE",
                "params": {"M": 16, "efConstruction": 200},
            },
        )
        print("[index] embedding HNSW/COSINE")

    if "sparse" not in existing_indexes:
        col.create_index(
            field_name="sparse",
            index_params={
                "index_type": "SPARSE_INVERTED_INDEX",
                "metric_type": "BM25",
                "params": {"bm25_k1": 1.2, "bm25_b": 0.75},
            },
        )
        print("[index] sparse SPARSE_INVERTED_INDEX/BM25")

    for field, index_type in SCALAR_INDEXES:
        if field not in existing_indexes:
            col.create_index(
                field_name=field,
                index_params={"index_type": index_type},
            )
            print(f"[index] {field} {index_type}")

    col.load()
    print(f"[load ] {name} num_entities={col.num_entities}")
    return col


def show_info(name: str = DEFAULT_COLLECTION) -> None:
    connect()
    print("server:", utility.get_server_version(using=ALIAS))
    print("collections:", utility.list_collections(using=ALIAS))
    if utility.has_collection(name, using=ALIAS):
        col = Collection(name, using=ALIAS)
        print(f"\n=== {name} ===")
        print("fields:", [(field.name, field.dtype.name) for field in col.schema.fields])
        print("indexes:", [(index.field_name, index.params) for index in col.indexes])
        print("num_entities:", col.num_entities)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="Create collection and indexes.")
    parser.add_argument("--drop", action="store_true", help="Drop existing collection before init.")
    parser.add_argument("--info", action="store_true", help="Show collection info.")
    parser.add_argument("--name", default=DEFAULT_COLLECTION, help="Collection name.")
    args = parser.parse_args()

    if not (args.init or args.info):
        parser.error("Specify --init or --info.")

    if args.init:
        init_collection(args.name, drop=args.drop)
    if args.info:
        show_info(args.name)


if __name__ == "__main__":
    main()
