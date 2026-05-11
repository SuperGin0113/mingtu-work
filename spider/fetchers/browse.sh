#!/usr/bin/env bash
# 批量跑 spider.fetchers.browse --browse-only：
#   从 JSON 里读每条 url，挨个采列表 upsert 到 mongo doc_items。
#   已成功的 guid 追加到 state 文件，重跑自动跳过；单条失败不中断后续。
#
# 用法:
#   ./browse.sh                            # 只跑 is_leaf=true，limit=0(不限)
#   LIMIT=200 ./browse.sh                  # 每条最多采 200
#   INCLUDE_NONLEAF=1 ./browse.sh          # 也跑 is_leaf=false 的节点
#   JSON=/path/to/other.json ./browse.sh   # 换数据源
#   STATE=/path/to/done.guids ./browse.sh  # 自定义状态文件
#   SLEEP=30 ./browse.sh                   # 两条之间固定间隔秒数；不设=随机 15-45
#   HARVEST_TIMEOUT=180 ./browse.sh        # 单条 harvest 最长等待秒数
#
#   DRY=1 ./browse.sh                      # shell 层 dry-run：只打印命令不调 python
#                                          # 想让 python 真跑但不写 mongo，参考 browse.py 的 --dry-run

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

JSON="${JSON:-/Users/gin/AI-project/tmp-handler-guidlist/westlaw_trademarks_leaf.json}"
LIMIT="${LIMIT:-0}"
HARVEST_TIMEOUT="${HARVEST_TIMEOUT:-120}"
INCLUDE_NONLEAF="${INCLUDE_NONLEAF:-0}"
STATE="${STATE:-$PROJECT_ROOT/data/browse_done.guids}"
SLEEP="${SLEEP:-}"
DRY="${DRY:-0}"
PY="${PY:-python}"

if ! command -v jq >/dev/null 2>&1; then
  echo "[browse.sh] jq not found in PATH" >&2
  exit 2
fi
if [[ ! -f "$JSON" ]]; then
  echo "[browse.sh] JSON not found: $JSON" >&2
  exit 2
fi

mkdir -p "$(dirname "$STATE")"
touch "$STATE"

if [[ "$INCLUDE_NONLEAF" == "1" ]]; then
  JQ_FILTER='.leaves_kn[]'
else
  JQ_FILTER='.leaves_kn[] | select(.is_leaf==true)'
fi

TOTAL=$(jq -r "[$JQ_FILTER] | length" "$JSON")
echo "[browse.sh] json=$JSON"
echo "[browse.sh] total=$TOTAL  include_nonleaf=$INCLUDE_NONLEAF  limit=$LIMIT  harvest_timeout=$HARVEST_TIMEOUT"
echo "[browse.sh] state=$STATE  done_so_far=$(wc -l < "$STATE" | tr -d ' ')"
echo "[browse.sh] cwd=$PROJECT_ROOT"

cd "$PROJECT_ROOT"

i=0
ok=0
fail=0
skip=0

while IFS=$'\t' read -r guid text url; do
  i=$((i + 1))

  if [[ -z "$guid" || -z "$url" ]]; then
    echo "[$i/$TOTAL] skip (empty guid/url)"
    skip=$((skip + 1))
    continue
  fi

  if grep -qxF "$guid" "$STATE"; then
    echo "[$i/$TOTAL] skip (done) $guid  $text"
    skip=$((skip + 1))
    continue
  fi

  echo
  echo "============================================================"
  echo "[$i/$TOTAL] guid=$guid"
  echo "          text=$text"
  echo "          url=${url:0:140}"
  echo "============================================================"

  if [[ "$DRY" == "1" ]]; then
    printf '[dry-run] %s -m spider.fetchers.browse --url %q --browse-only --limit %s --harvest-timeout %s\n' \
      "$PY" "$url" "$LIMIT" "$HARVEST_TIMEOUT"
    ok=$((ok + 1))
  else
    if "$PY" -m spider.fetchers.browse \
        --url "$url" \
        --browse-only \
        --limit "$LIMIT" \
        --harvest-timeout "$HARVEST_TIMEOUT"; then
      printf '%s\n' "$guid" >> "$STATE"
      ok=$((ok + 1))
      echo "[$i/$TOTAL] OK"
    else
      rc=$?
      fail=$((fail + 1))
      echo "[$i/$TOTAL] FAILED (exit $rc), continuing..."
    fi
  fi

  if [[ $i -lt $TOTAL ]]; then
    if [[ -n "$SLEEP" ]]; then
      delay="$SLEEP"
    else
      delay=$((15 + RANDOM % 31))
    fi
    echo "[wait] ${delay}s"
    sleep "$delay"
  fi
done < <(jq -rc "$JQ_FILTER | [.guid, .text, .url] | @tsv" "$JSON")

echo
echo "[browse.sh] done. ok=$ok  fail=$fail  skip=$skip  total=$TOTAL"
