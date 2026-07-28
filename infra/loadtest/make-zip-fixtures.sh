#!/usr/bin/env bash
# Build distinct ZIP fixtures for the W2-2 (upload_zip) canary measurement.
# Each zip holds many members so the per-member loop in upload_zip does real work.
set -eu
# Resolve the repo root from this script's own location, the same way
# run-sweep.sh and pg-sample.sh do. This previously hardcoded an absolute path
# to the worktree of the run that first wrote the script, so on any other
# checkout it silently built the fixtures into the *other* tree and left
# ./fixtures empty — profile 3b then failed in init with "open() ... no such
# file". Never hardcode a worktree path here.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="$REPO_ROOT/infra/loadtest/fixtures"
N="${1:-40}"        # number of distinct zips
MEMBERS="${2:-60}"  # files per zip
rm -rf "$DEST"; mkdir -p "$DEST"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

for z in $(seq 1 "$N"); do
  d="$TMP/z$z"; mkdir -p "$d"
  for m in $(seq 1 "$MEMBERS"); do
    {
      echo "# 壓縮包 $z 成員 $m"
      for s in $(seq 1 12); do
        echo "## $z-$m-$s 節"
        echo "本節說明壓縮包 $z 第 $m 個檔案第 $s 段。向量資料庫儲存切片嵌入，"
        echo "檢索以餘弦相似度排序，快取降低重複查詢延遲，稽核保存存取來源與時間。"
        echo "設定值 $((z * 10000 + m * 100 + s))，逾時 $((30 + s)) 秒。"
      done
    } > "$d/member-$m.md"
  done
  (cd "$d" && zip -q -r "$DEST/bundle-$z.zip" .)
done
echo "built $N zips in $DEST"
du -sh "$DEST"
ls "$DEST" | head -3
