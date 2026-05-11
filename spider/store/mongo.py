"""MongoDB 存储层：与原 PG fetch_doc_html / fetch_doc_pic 行为对齐的 CRUD 接口。

PG → Mongo 字段对应（保留 listItems 原始驼峰命名 + 爬取状态机）：
  doc_items     PG 表    →  MongoDB collection (DOC_ITEMS_COLLECTION)
  doc_images    PG 表    →  MongoDB collection (DOC_IMAGES_COLLECTION)

状态码与 PG 完全一致：
  0 待处理 / 1 处理中 / 2 成功 / 3 可重试 / 4 不可重试
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from bson import ObjectId
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database

from spider.config import (
    DOC_HTML_MAX_BYTES,
    DOC_IMAGES_COLLECTION,
    DOC_ITEMS_COLLECTION,
    MONGO_DB,
    MONGO_URI,
)

# 状态码（与 PG 一致）
STATUS_PENDING = 0
STATUS_PROCESSING = 1
STATUS_SUCCESS = 2
STATUS_RETRYABLE = 3
STATUS_FAILED = 4

STATUS_LABELS = {
    STATUS_PENDING: "pending",
    STATUS_PROCESSING: "processing",
    STATUS_SUCCESS: "success",
    STATUS_RETRYABLE: "retryable",
    STATUS_FAILED: "failed",
}

MAX_RETRIES = 3


# ==================== 客户端 ====================

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client


def get_db() -> Database:
    return get_client()[MONGO_DB]


def doc_items() -> Collection:
    return get_db()[DOC_ITEMS_COLLECTION]


def doc_images() -> Collection:
    return get_db()[DOC_IMAGES_COLLECTION]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ==================== 索引 ====================

def ensure_indexes() -> None:
    items = doc_items()
    items.create_index([("docGuid", ASCENDING)], unique=True, sparse=True, name="uniq_doc_guid")
    items.create_index([("status", ASCENDING), ("rank", ASCENDING)], name="status_rank")
    items.create_index([("source_browse_guid", ASCENDING)], name="source_browse_guid")
    # 下载时按 caseDocumentGuid 去重，索引加快 aggregation 的分组
    items.create_index([("caseDocumentGuid", ASCENDING), ("rank", ASCENDING)], name="case_rank")
    # get_pending_doc_rows 的 $lookup 自连接：按 caseDocumentGuid 反查 status=SUCCESS 兄弟
    items.create_index([("caseDocumentGuid", ASCENDING), ("status", ASCENDING)], name="case_status")

    images = doc_images()
    images.create_index(
        [("doc_item_id", ASCENDING), ("blob_id", ASCENDING)],
        unique=True,
        name="uniq_doc_blob",
    )
    images.create_index([("status", ASCENDING)], name="img_status")


# ==================== doc_items: 写入 ====================

def upsert_list_items(
    items_payload: Iterable[dict],
    source_browse_url: str,
    source_browse_guid: str | None,
    search_key_number: str | None = None,
) -> dict:
    """从 listItems[] upsert 到 mongo。同 docGuid 已存在则更新原始字段（不覆盖 status / doc_html）。

    不再写入 `is_primary`——去重交给 get_pending_doc_rows 在下载时按 caseDocumentGuid 动态计算。

    `search_key_number` 由调用方从 XHR `resultPageData.categoryPageTitle` 抽出（如 `k2056`）。
    """
    raw_items = list(items_payload)
    if not raw_items:
        return {"upserted": 0, "modified": 0}

    now = now_utc()
    ops: list[UpdateOne] = []
    for it in raw_items:
        doc_guid = it.get("docGuid")
        if not doc_guid:
            continue

        # $setOnInsert：只在首次插入时落，之后再 harvest 不会被覆盖（状态机 + 首次时间戳）
        set_on_insert = {
            "status": STATUS_PENDING,
            "retry_count": 0,
            "fail_reason": None,
            "doc_html": None,
            "image_count": 0,
            "create_time": now,
        }
        # $set：每次 harvest 都刷新（listItems 服务端可能更新过 title 等；source 也算最新一次的）
        # 注意：$setOnInsert 与 $set 不能含相同 key，否则 mongo 报 "create a conflict at <path>"。
        set_always = {k: v for k, v in it.items() if k != "docGuid"}
        set_always["source_browse_url"] = source_browse_url
        set_always["source_browse_guid"] = source_browse_guid
        set_always["update_time"] = now
        if search_key_number:
            set_always["searchKeyNumber"] = search_key_number

        ops.append(
            UpdateOne(
                {"docGuid": doc_guid},
                {"$setOnInsert": set_on_insert, "$set": set_always},
                upsert=True,
            )
        )

    if not ops:
        return {"upserted": 0, "modified": 0}

    result = doc_items().bulk_write(ops, ordered=False)
    return {
        "upserted": len(result.upserted_ids or {}),
        "modified": result.modified_count,
    }


# ==================== doc_items: 读取 / 状态机 ====================

def reset_stale_doc_processing() -> int:
    """启动时把上轮中断卡在 processing 的记录重置为 pending。"""
    res = doc_items().update_many(
        {"status": STATUS_PROCESSING},
        {"$set": {"status": STATUS_PENDING, "update_time": now_utc()}},
    )
    return res.modified_count


def get_pending_doc_rows(limit: int = 0) -> list[tuple]:
    """下载时按 caseDocumentGuid 去重：每组只取最低 rank 的待下载条目；
    若该组已有 STATUS_SUCCESS，则整组跳过（自动避免重复爬取）。

    返回元组与 PG 版 get_pending_rows 兼容：
        (id, rank, doc_url, case_document_guid, retry_count)
    其中 id 是 ObjectId。
    """
    coll = doc_items()

    # 把超过上限的先转 FAILED，之后聚合就不会选中它们
    coll.update_many(
        {
            "status": STATUS_RETRYABLE,
            "retry_count": {"$gte": MAX_RETRIES},
        },
        {"$set": {"status": STATUS_FAILED, "update_time": now_utc()}},
    )

    pipeline: list[dict] = [
        # 候选：pending 或 retryable 且未超重试上限
        {
            "$match": {
                "status": {"$in": [STATUS_PENDING, STATUS_RETRYABLE]},
                "retry_count": {"$lt": MAX_RETRIES},
            }
        },
        # 同 caseDocumentGuid 已有 SUCCESS 的整组排除
        {
            "$lookup": {
                "from": DOC_ITEMS_COLLECTION,
                "let": {"cg": "$caseDocumentGuid"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$caseDocumentGuid", "$$cg"]},
                            "status": STATUS_SUCCESS,
                        }
                    },
                    {"$limit": 1},
                ],
                "as": "_succeeded_sibling",
            }
        },
        {"$match": {"_succeeded_sibling": []}},
        # 同组取最低 rank
        {"$sort": {"rank": 1}},
        {
            "$group": {
                "_id": {"$ifNull": ["$caseDocumentGuid", "$docGuid"]},
                "doc": {"$first": "$$ROOT"},
            }
        },
        {"$replaceRoot": {"newRoot": "$doc"}},
        {"$sort": {"rank": 1}},
    ]
    if limit and limit > 0:
        pipeline.append({"$limit": limit})

    return [
        (
            d["_id"],
            d.get("rank"),
            d.get("docUrl"),
            d.get("caseDocumentGuid"),
            d.get("retry_count", 0),
        )
        for d in coll.aggregate(pipeline)
    ]


def doc_summary() -> dict[int, int]:
    """全局 doc_items 的 status 分布。"""
    cursor = doc_items().aggregate(
        [
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
    )
    return {row["_id"]: row["n"] for row in cursor}


def reset_failed_docs() -> int:
    """把 status=FAILED 的 doc_items 重置成 PENDING（清掉 retry_count + fail_reason）。"""
    res = doc_items().update_many(
        {"status": STATUS_FAILED},
        {
            "$set": {
                "status": STATUS_PENDING,
                "retry_count": 0,
                "fail_reason": None,
                "update_time": now_utc(),
            }
        },
    )
    return res.modified_count


def update_doc_row(
    doc_id: ObjectId,
    status: int,
    doc_html: str | None = None,
    inc_retry: bool = False,
    fail_reason: str | None = None,
) -> None:
    """对应 PG 的 update_row。doc_html 写入前做 size guard。"""
    update: dict[str, Any] = {"status": status, "update_time": now_utc()}
    if doc_html is not None:
        size = len(doc_html.encode("utf-8"))
        if size > DOC_HTML_MAX_BYTES:
            doc_items().update_one(
                {"_id": doc_id},
                {
                    "$set": {
                        "status": STATUS_FAILED,
                        "fail_reason": f"oversize_{size}",
                        "update_time": now_utc(),
                    }
                },
            )
            return
        update["doc_html"] = doc_html
        update["fail_reason"] = None
        ops: dict[str, Any] = {"$set": update}
    elif inc_retry:
        update["fail_reason"] = fail_reason
        ops = {"$set": update, "$inc": {"retry_count": 1}}
    else:
        update["fail_reason"] = fail_reason
        ops = {"$set": update}

    doc_items().update_one({"_id": doc_id}, ops)


# ==================== doc_images ====================

def save_content_images(doc_id: ObjectId, images: list[dict]) -> None:
    """把 HTML 解析出的 content images upsert 到 doc_images，并更新主表 image_count。

    images 元素结构（来自 fetcher 的 extract_content_images）：
      {blob_id, image_url, alt_text, width, height, position}
    """
    if images:
        now = now_utc()
        ops = []
        for img in images:
            ops.append(
                UpdateOne(
                    {"doc_item_id": doc_id, "blob_id": img["blob_id"]},
                    {
                        "$setOnInsert": {
                            "doc_item_id": doc_id,
                            "blob_id": img["blob_id"],
                            "image_url": img["image_url"],
                            "alt_text": img.get("alt_text"),
                            "width": img.get("width"),
                            "height": img.get("height"),
                            "position": img.get("position"),
                            "status": STATUS_PENDING,
                            "retry_count": 0,
                            "fail_reason": None,
                            "file_path": None,
                            "file_size": None,
                            "create_time": now,
                            "update_time": now,
                        }
                    },
                    upsert=True,
                )
            )
        doc_images().bulk_write(ops, ordered=False)
    doc_items().update_one(
        {"_id": doc_id}, {"$set": {"image_count": len(images), "update_time": now_utc()}}
    )


def reset_stale_image_processing() -> int:
    res = doc_images().update_many(
        {"status": STATUS_PROCESSING},
        {"$set": {"status": STATUS_PENDING, "update_time": now_utc()}},
    )
    return res.modified_count


def get_pending_image_rows(limit: int = 0) -> list[tuple]:
    """取所有待下载图片。
    （save_content_images 只在 doc_html 抓取成功时调用，所以 doc_images 里
    天然只包含"被去重选中并成功爬取的 doc"对应的图片。）

    返回元组与 PG 版 get_pending_images 兼容：
        (id, doc_item_id, case_document_guid, blob_id, image_url, retry_count)
    """
    # 超过重试上限的先转 FAILED
    doc_images().update_many(
        {"status": STATUS_RETRYABLE, "retry_count": {"$gte": MAX_RETRIES}},
        {"$set": {"status": STATUS_FAILED, "update_time": now_utc()}},
    )

    cursor = doc_images().find(
        {
            "status": {"$in": [STATUS_PENDING, STATUS_RETRYABLE]},
            "retry_count": {"$lt": MAX_RETRIES},
        },
        {
            "doc_item_id": 1,
            "blob_id": 1,
            "image_url": 1,
            "retry_count": 1,
            "position": 1,
        },
    ).sort([("doc_item_id", ASCENDING), ("position", ASCENDING)])
    if limit and limit > 0:
        cursor = cursor.limit(limit)

    raw = list(cursor)
    if not raw:
        return []

    doc_ids = list({r["doc_item_id"] for r in raw})
    case_guid_map = {
        d["_id"]: d.get("caseDocumentGuid")
        for d in doc_items().find({"_id": {"$in": doc_ids}}, {"caseDocumentGuid": 1})
    }

    return [
        (
            r["_id"],
            r["doc_item_id"],
            case_guid_map.get(r["doc_item_id"]),
            r.get("blob_id"),
            r.get("image_url"),
            r.get("retry_count", 0),
        )
        for r in raw
    ]


def update_image_row(
    img_id: ObjectId,
    status: int,
    file_path: str | None = None,
    file_size: int | None = None,
    inc_retry: bool = False,
    fail_reason: str | None = None,
) -> None:
    update: dict[str, Any] = {"status": status, "update_time": now_utc()}
    if file_path is not None:
        update["file_path"] = file_path
        update["file_size"] = file_size
        update["fail_reason"] = None
        ops: dict[str, Any] = {"$set": update}
    elif inc_retry:
        update["fail_reason"] = fail_reason
        ops = {"$set": update, "$inc": {"retry_count": 1}}
    else:
        update["fail_reason"] = fail_reason
        ops = {"$set": update}
    doc_images().update_one({"_id": img_id}, ops)


def update_doc_image_status(doc_item_id: ObjectId) -> None:
    """根据子表状态聚合更新主表 image_status 字段（与 PG update_doc_image_status 对齐）。"""
    cursor = doc_images().aggregate(
        [
            {"$match": {"doc_item_id": doc_item_id}},
            {
                "$group": {
                    "_id": None,
                    "total": {"$sum": 1},
                    "success": {"$sum": {"$cond": [{"$eq": ["$status", STATUS_SUCCESS]}, 1, 0]}},
                    "failed": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", [STATUS_RETRYABLE, STATUS_FAILED]]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
    )
    agg = next(cursor, None)
    if not agg or agg["total"] == 0:
        return
    if agg["success"] == agg["total"]:
        image_status = STATUS_SUCCESS
    elif agg["failed"] > 0 and agg["success"] > 0:
        image_status = STATUS_RETRYABLE
    elif agg["failed"] == agg["total"]:
        image_status = STATUS_FAILED
    else:
        return
    doc_items().update_one(
        {"_id": doc_item_id},
        {"$set": {"image_status": image_status, "update_time": now_utc()}},
    )


def image_summary() -> dict[int, int]:
    cursor = doc_images().aggregate(
        [
            {"$group": {"_id": "$status", "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
    )
    return {row["_id"]: row["n"] for row in cursor}


def reset_failed_images() -> int:
    """把 status=FAILED 的 doc_images 重置成 PENDING（清掉 retry_count + fail_reason）。"""
    res = doc_images().update_many(
        {"status": STATUS_FAILED},
        {
            "$set": {
                "status": STATUS_PENDING,
                "retry_count": 0,
                "fail_reason": None,
                "update_time": now_utc(),
            }
        },
    )
    return res.modified_count
