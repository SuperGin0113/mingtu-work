# fetchers 常用命令

三阶段：**采列表 (browse) → 爬正文 (fetch HTML) → 下载图片 (pic)**。前两步在 `browse.py` 里串跑，图片是独立第三步。

## 1. 采列表 + 爬正文（默认两阶段串跑）

```bash
python -m spider.fetchers.browse --url "<browse_url>" --limit 100
```

- `<browse_url>` = Westlaw `Browse/Home/WestKeyNumberSystem?...` 这种入口
- `--limit 0` = 不限条数

## 2. 只采列表（不爬正文）

```bash
python -m spider.fetchers.browse --url "<browse_url>" --limit 100 --browse-only
```

## 3. 断点续跑（只爬正文，跳过采列表）

```bash
python -m spider.fetchers.browse --fetch-only --limit 100
```

## 4. 下载图片（独立第三阶段）

```bash
python -m spider.fetchers.doc_pic_mongo --limit 200
```

## 5. 查看进度 / 调试

```bash
# 看 mongo 里 doc_items 的 status 分布
python -m spider.fetchers.browse --status

# 看图片表的 status 分布
python -m spider.fetchers.doc_pic_mongo --status

# 预览：harvest 一次但不写库（带 --url）；或列出 pending 行（不带 --url）
python -m spider.fetchers.browse --dry-run --url "<browse_url>" --limit 4
python -m spider.fetchers.browse --dry-run --limit 10

# 把 FAILED 的全部重置回 PENDING
python -m spider.fetchers.browse --reset-failed
python -m spider.fetchers.doc_pic_mongo --reset-failed
```

## 常用可调参数

| 参数 | 作用 | 默认 |
|---|---|---|
| `--limit N` | 最多处理 N 条；`0` 不限 | `0` |
| `--harvest-timeout S` | browse 阶段等 XHR 的最长秒数 | `120` |
| `--cool-down S` | browse → fetch 间冷却秒数；`0` 跳过；`<0` 随机 60–120s | `-1` |
