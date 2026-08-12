#!/usr/bin/env bash
# 在對應 Python ABI 容器內收集平台 Python 相依閉包，供氣隙內 overlay patch 使用。
#
#   bash infra/deployment/offline/build-platform-wheelhouse.sh
#   bash infra/deployment/offline/build-platform-wheelhouse.sh cp313
#
# 這支工具只處理 Python wheelhouse；從零離線重建仍會被 Dockerfile 的 apt/npm/
# Playwright/字型步驟擋住，故不在本段承諾內。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd -P)"
OFFLINE_ROOT="$REPO_ROOT/infra/deployment/offline"
DIST_ROOT="$OFFLINE_ROOT/dist"
TARGET="${1:-all}"

usage() {
    echo "用法: bash infra/deployment/offline/build-platform-wheelhouse.sh [all|cp313|cp312]" >&2
}

case "$TARGET" in
    all|cp313|cp312) ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage
        exit 2
        ;;
esac

command -v docker >/dev/null 2>&1 || {
    echo "✗ docker 不在 PATH 上" >&2
    exit 2
}

mkdir -p "$DIST_ROOT"

prepare_house() {
    local abi="$1"
    rm -rf -- "$DIST_ROOT/$abi"
    # Remove the legacy per-house manifest from older runs.
    rm -f -- "$DIST_ROOT/$abi.freeze.txt"
    mkdir -p "$DIST_ROOT/$abi"
}

collect_abi() {
    local abi="$1" image="$2"
    prepare_house "$abi"
    echo ">> 收集 $abi wheelhouse（容器：$image）"

    docker run --rm -i \
        --mount "type=bind,src=$REPO_ROOT,dst=/repo,readonly" \
        --mount "type=bind,src=$DIST_ROOT,dst=/dist" \
        "$image" sh -s -- "$abi" <<'CONTAINER_SCRIPT'
set -eu

ABI="$1"
HOUSE="/dist/$ABI"
SOURCE_ROOT="/tmp/anila-wheelhouse-src"
SERVICE_STAGE_ROOT="/tmp/anila-wheelhouse-services"
CONSTRAINTS="/tmp/anila-core-constraints.txt"

rm -rf "$SOURCE_ROOT" "$SERVICE_STAGE_ROOT"
mkdir -p "$SOURCE_ROOT"
mkdir -p "$SERVICE_STAGE_ROOT"

if [ "$ABI" = "cp313" ]; then
    # pip 的 PEP 517 建置可能在來源樹寫暫存 metadata；repo 維持唯讀，
    # 只複製本段需要建 wheel 的 local project 到容器可寫位置。
    cp -a /repo/packages/anila-core "$SOURCE_ROOT/anila-core"
    cp -a /repo/services/ingestion-worker "$SOURCE_ROOT/ingestion-worker"
    cp -a /repo/services/anila-studio "$SOURCE_ROOT/anila-studio"
    cp -a /repo/services/asr-gateway "$SOURCE_ROOT/asr-gateway"
elif [ "$ABI" = "cp312" ]; then
    cp -a /repo/services/asr-decoder "$SOURCE_ROOT/asr-decoder"
else
    echo "✗ 未知 ABI: $ABI" >&2
    exit 2
fi

: > "$CONSTRAINTS"

if [ "$ABI" = "cp313" ]; then
    CORE_REF="anila-core @ file:///tmp/anila-wheelhouse-src/anila-core"
    CORE_RAG_REF="anila-core[rag] @ file:///tmp/anila-wheelhouse-src/anila-core"

    python -m pip wheel \
        --disable-pip-version-check \
        --no-cache-dir \
        --no-deps \
        --wheel-dir "$HOUSE" \
        "$SOURCE_ROOT/anila-core"

    # --find-links 只是增加候選來源；這個 PEP 508 file:// constraint 才會
    # 把 in-repo anila-core 釘在本地來源，阻止公開 index 的同名套件插隊。
    printf '%s\n' "$CORE_REF" > "$CONSTRAINTS"
fi

resolve_service() {
    local label="$1"
    shift
    local service_stage="$SERVICE_STAGE_ROOT/$label"
    rm -rf "$service_stage"
    mkdir -p "$service_stage"
    echo ">> resolve $label"
    python -m pip wheel \
        --disable-pip-version-check \
        --no-cache-dir \
        --wheel-dir "$service_stage" \
        --find-links "$HOUSE" \
        -c "$CONSTRAINTS" \
        "$@"
    python /repo/infra/deployment/offline/audit-wheelhouse.py \
        "$service_stage" \
        --write-manifest "$HOUSE/$label.freeze.txt"
    find "$service_stage" -maxdepth 1 -type f -name '*.whl' \
        -exec cp -a {} "$HOUSE/" \;
}

case "$ABI" in
    cp313)
        resolve_service csp \
            -r /repo/services/csp/requirements.txt \
            "$CORE_RAG_REF"
        resolve_service ingestion-worker \
            "$SOURCE_ROOT/ingestion-worker" \
            "$CORE_RAG_REF"
        resolve_service router \
            -r /repo/services/anila-core-router/requirements.txt \
            "$CORE_REF"
        resolve_service anila-studio "$SOURCE_ROOT/anila-studio"
        resolve_service asr-gateway "$SOURCE_ROOT/asr-gateway"
        ;;
    cp312)
        resolve_service asr-decoder "$SOURCE_ROOT/asr-decoder"
        ;;
esac

# 同一 ABI 的所有服務已 append 到同一倉；多版本 distribution 可合法共存，
# 但每一個服務 manifest 必須完整覆蓋其自身 resolve 實際收集的 wheels。
python /repo/infra/deployment/offline/audit-wheelhouse.py \
    "$HOUSE"
CONTAINER_SCRIPT

    if [[ ! -d "$DIST_ROOT/$abi" ]]; then
        echo "✗ $abi wheelhouse 目錄不存在：$DIST_ROOT/$abi" >&2
        return 1
    fi

    local wheel
    wheel="$(find "$DIST_ROOT/$abi" -type f -name '*.whl' -print -quit)"
    if [[ -z "$wheel" ]]; then
        echo "✗ $abi wheelhouse 沒有任何 *.whl：$DIST_ROOT/$abi" >&2
        return 1
    fi

    local -a expected_services
    case "$abi" in
        cp313)
            expected_services=(csp ingestion-worker router anila-studio asr-gateway)
            ;;
        cp312)
            expected_services=(asr-decoder)
            ;;
    esac

    local service manifest
    for service in "${expected_services[@]}"; do
        manifest="$DIST_ROOT/$abi/$service.freeze.txt"
        if [[ ! -s "$manifest" ]]; then
            echo "✗ $abi service freeze manifest 不存在或為空：$manifest" >&2
            return 1
        fi
    done

    echo ">> 已驗證 $abi：至少一個 wheel，且所有服務 freeze manifest 非空"
}

case "$TARGET" in
    all)
        collect_abi cp313 python:3.13-slim
        collect_abi cp312 python:3.12-slim
        ;;
    cp313)
        collect_abi cp313 python:3.13-slim
        ;;
    cp312)
        collect_abi cp312 python:3.12-slim
        ;;
esac

echo ">> 完成：$DIST_ROOT（每個要求的 ABI house 與服務 freeze manifests 均已通過 audit 並完成產物驗證）"
