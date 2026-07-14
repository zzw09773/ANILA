#!/usr/bin/env bash
#
# anila-agent image 搬運腳本：外網 build → 壓縮 tar → 搬進 air-gap 內網 / MLSteam load。
#
# 用法：
#   scripts/docker-ship.sh build                      # 只 build image
#   scripts/docker-ship.sh save                       # build（若無）+ 存成 anila-agent_<tag>.tar.gz
#   scripts/docker-ship.sh load anila-agent_1.0.0.tar.gz   # 在內網主機 load 進 docker
#
# 環境變數（可覆寫）：
#   ANILA_IMAGE   image 名（預設 anila-agent）
#   ANILA_TAG     tag（預設 1.0.0）
set -euo pipefail

IMAGE="${ANILA_IMAGE:-anila-agent}"
TAG="${ANILA_TAG:-1.0.0}"
REF="${IMAGE}:${TAG}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MONOREPO_ROOT="$(cd "$ROOT/../.." && pwd)"
DOCKERFILE="$MONOREPO_ROOT/packages/anila-agent/Dockerfile"

cmd="${1:-save}"
case "$cmd" in
  build)
    docker build ${DOCKER_TARGET:+--target "$DOCKER_TARGET"} -f "$DOCKERFILE" -t "$REF" "$MONOREPO_ROOT"
    ;;
  save)
    docker image inspect "$REF" >/dev/null 2>&1 || docker build -f "$DOCKERFILE" -t "$REF" "$MONOREPO_ROOT"
    out="${ROOT}/${IMAGE}_${TAG}.tar.gz"
    echo "→ saving ${REF} ..."
    docker save "$REF" | gzip > "$out"
    sz="$(du -h "$out" | cut -f1)"
    echo "→ ${out} (${sz})"
    echo
    echo "搬進內網後，在那台主機跑："
    echo "  scripts/docker-ship.sh load $(basename "$out")"
    echo "或直接： gunzip -c $(basename "$out") | docker load"
    ;;
  load)
    f="${2:?用法: docker-ship.sh load <file.tar.gz>}"
    echo "→ loading ${f} ..."
    gunzip -c "$f" | docker load
    echo "→ done. 跑： docker run --rm -p 8200:8200 --env-file .env ${REF}"
    ;;
  *)
    echo "用法: $0 {build|save|load <file.tar.gz>}" >&2
    exit 2
    ;;
esac
