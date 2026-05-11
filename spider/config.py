from __future__ import annotations

import os

from dotenv import load_dotenv

from script.project_paths import ENV_FILE

load_dotenv(ENV_FILE)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "westlaw_spider")

DOC_ITEMS_COLLECTION = "doc_items"
DOC_IMAGES_COLLECTION = "doc_images"

DOC_HTML_MAX_BYTES = 15 * 1024 * 1024

# 抓取节奏（反爬）
HTML_REQUEST_INTERVAL = (9.0, 18.0)   # doc_html 条间随机 sleep 秒数
PIC_REQUEST_INTERVAL = (8.0, 17.0)     # doc_pic 条间随机 sleep 秒数
MAX_CONSECUTIVE_FAILS = 3              # 连续失败熔断阈值
