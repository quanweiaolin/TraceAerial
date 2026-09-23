#!/usr/bin/env bash
# 随机抽样检查videos.storage_path记录的路径在磁盘上是否真实存在，
# 用于验证扫盘/路径迁移之后DB和磁盘没有对不上的情况。
# 用法: ./verify_storage_paths_sample.sh [database_url] [抽样条数，默认100]
set -euo pipefail

DATABASE_URL="${1:-postgresql://reader:reader@localhost:5432/aerial_db}"
SAMPLE_SIZE="${2:-100}"

paths=$(psql "$DATABASE_URL" -t -A \
  -c "SELECT storage_path FROM aerial.videos WHERE deleted_at IS NULL ORDER BY random() LIMIT ${SAMPLE_SIZE};")

checked=0
missing=0
while IFS= read -r path; do
  [ -z "$path" ] && continue
  checked=$((checked + 1))
  if [ ! -f "$path" ]; then
    echo "缺失: $path"
    missing=$((missing + 1))
  fi
done <<< "$paths"

echo "抽样检查: ${checked} 条，缺失 ${missing} 条"
