#!/usr/bin/env bash
# preflight.sh — 安裝或更新前，在內網主機檢查這台準備好了沒。不改任何東西。
#
#   bash preflight.sh [出貨包目錄或 tar.gz]
#
# ✗ 表示照原樣跑 anila-update.sh 會失敗，✓ 通過，⚠ 要看一下但不擋。
# 每一條 ✗ 都印出要執行的指令。有 ✗ 時以非零結束。
# 2026-09-30 .35 演練實撞的項目都在這裡：安裝目錄要 sudo 先建、443 被別的
# 服務佔用、compose 版本、Docker 用 containerd 儲存。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
source "$HERE/release-lib.sh"

FAILS=0
pass() { printf '✓ %s\n' "$*"; }
fail() { printf '✗ %s\n' "$*"; FAILS=$((FAILS + 1)); }
note() { printf '⚠ %s\n' "$*"; }
fix()  { printf '    %s\n' "$*"; }

bundle="${1:-}"
root="/opt/anila"
anchor_dir="/var/lib/anila"
me="$(id -un)"

printf '== ANILA 安裝前檢查（%s，%s）==\n' "$(hostname)" "$me"

# ── 指令 ─────────────────────────────────────────────────────────────────────
for cmd in docker openssl curl tar gzip sha256sum; do
  if command -v "$cmd" >/dev/null 2>&1; then
    pass "有 $cmd"
  else
    fail "找不到 $cmd"
  fi
done

# ── Docker ───────────────────────────────────────────────────────────────────
if docker info >/dev/null 2>&1; then
  pass "目前帳號可以用 docker"
  driver="$(docker info --format '{{.Driver}}' 2>/dev/null || true)"
  if [[ "$driver" == overlayfs ]]; then
    note "Docker 用 containerd 儲存（${driver}）。出貨包同時記兩種映像 ID，可以裝；舊版出貨包（2026-09-30 以前）在這種主機會拒絕載入。"
  else
    pass "Docker 儲存：${driver:-未知}"
  fi
else
  fail "目前帳號不能用 docker"
  fix "sudo usermod -aG docker $me    # 然後重新登入"
fi
cv="$(docker compose version --short 2>/dev/null || true)"
if compose_version_ok "$cv"; then
  pass "docker compose ${cv}"
else
  fail "docker compose 版本 ${cv:-讀不到}，需要 2.17 以上"
fi

# ── 安裝目錄與安裝記錄 ───────────────────────────────────────────────────────
# 順序固定：先安裝根目錄，再 /var/lib/anila。安裝程式動任何東西之前做同一套。
# 現在安裝與更新都用 sudo。不是 root、目錄又不存在、不可寫或進不去，印出要改跑的指令。
for d in "$root" "$anchor_dir"; do
  if [[ -d "$d" && -w "$d" && -x "$d" ]]; then
    pass "$d 存在且可寫"
  elif install_dir_ready "$d"; then
    pass "$d 會由 root 建立"
  else
    fail "$d 不存在或不可寫"
    if [[ -n "$bundle" ]]; then
      fix "$(printf 'sudo bash %q %q' "$HERE/anila-update.sh" "$bundle")"
    else
      fix "$(printf 'sudo bash %q' "$HERE/anila-update.sh")"
    fi
  fi
done

# ── 執行身分 ─────────────────────────────────────────────────────────────────
# 平台的上傳、附件目錄屬於服務帳號（權限 700）。一般帳號做不了更新前的檔案快照，
# 所以安裝與更新都用 sudo 跑（2026-10-01 .35 更新演練）。
if [[ "$(id -u)" -eq 0 ]]; then
  pass "以 root 執行"
else
  note "安裝、更新、回復都要用 sudo 執行：sudo bash …/anila-update.sh …（這次預檢用目前帳號跑，只能看到部分項目）"
fi
# 安裝記錄目錄是 root 的 700。一般帳號看不到，就不判斷是不是第一次安裝。
anchor_known=1
if [[ -d "$anchor_dir" && ! -r "$anchor_dir" ]]; then
  anchor_known=0
  note "看不到 $anchor_dir（只有 root 讀得到），用 sudo 跑才能判斷這次是第一次安裝還是更新"
fi
if [[ "$anchor_known" == 1 && -f "$anchor_dir/install-anchor" ]]; then
  note "已有安裝記錄：$(tr '\n' ' ' < "$anchor_dir/install-anchor")— 這次是更新，不是第一次安裝"
fi
if docker volume ls -q 2>/dev/null | grep -q '^anila_'; then
  if [[ "$anchor_known" == 1 && ! -f "$anchor_dir/install-anchor" ]]; then
    fail "已有 anila_ 開頭的 volume，但沒有安裝記錄。這是舊部署，要先認領"
    fix "sudo bash $root/anila-update.sh adopt $root/versions/<目前版本>    # 見 docs/deploy/UPDATE.md"
  fi
fi

# ── 埠 ───────────────────────────────────────────────────────────────────────
env_file="$root/state/.env"
port_of() {
  local key="$1" def="$2" v=""
  if [[ -r "$env_file" ]]; then
    v="$(grep -E "^${key}=" "$env_file" | tail -1 | cut -d= -f2-)"
  fi
  printf '%s' "${v:-$def}"
}
listening() { port_is_listening "$1"; }
anila_owns() { anila_nginx_owns_port "$1"; }
for spec in "NGINX_HTTP_PORT 80" "NGINX_HTTPS_PORT 443" "ANILA_UI_HTTPS_PORT 4443"; do
  read -r key def <<<"$spec"
  port="$(port_of "$key" "$def")"
  # HTTPS 固定先聽 443。被佔用時安裝程式自己改走下一個空埠，預檢不因此失敗。
  if [[ "$key" == NGINX_HTTPS_PORT ]]; then
    port=443
  fi
  if ! listening "$port"; then
    pass "埠 $port 空著（$key）"
  elif anila_owns "$port"; then
    pass "埠 $port 是 ANILA 自己在用（$key）"
  elif [[ "$key" == NGINX_HTTPS_PORT ]]; then
    owner="$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E ":$port->" | awk '{print $1}' | head -1)"
    note "埠 443 被 ${owner:-其他程式} 佔用。安裝與更新會自動改聽下一個空埠（從 8443 起）；443 空出來後會改回 443。"
  else
    owner="$(docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E ":$port->" | awk '{print $1}' | head -1)"
    fail "埠 $port 被 ${owner:-其他程式} 佔用（$key）"
    fix "mkdir -p $root/state && printf '%s=%s\\n' $key <另一個埠> >> $env_file && chmod 600 $env_file"
  fi
done

# ── 磁碟 ─────────────────────────────────────────────────────────────────────
need_gb=20
if [[ -n "$bundle" && -e "$bundle" ]]; then
  size_gb="$(du -sBG "$bundle" 2>/dev/null | awk '{print int($1)}')"
  # 解壓、載入映像、兩版快照：約四倍出貨包大小
  need_gb=$(( size_gb * 4 > need_gb ? size_gb * 4 : need_gb ))
fi
for path in "$(dirname "$root")" "$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"; do
  [[ -d "$path" ]] || continue
  free_gb="$(df -BG --output=avail "$path" 2>/dev/null | tail -1 | tr -dc 0-9)"
  if [[ -n "$free_gb" && "$free_gb" -ge "$need_gb" ]]; then
    pass "$path 剩 ${free_gb}G（需要約 ${need_gb}G）"
  else
    fail "$path 只剩 ${free_gb:-?}G，需要約 ${need_gb}G"
  fi
done

# ── 出貨包 ───────────────────────────────────────────────────────────────────
if [[ -n "$bundle" ]]; then
  if [[ -f "$bundle" && "$bundle" == *.tar.gz ]]; then
    pass "出貨包：$bundle（SHA256 $(sha256sum "$bundle" | cut -c1-16)…，請與打包時印出的值對照）"
  elif [[ -d "$bundle" && -f "$bundle/manifest.sha256" ]]; then
    if (cd "$bundle" && sha256sum -c --quiet manifest.sha256 >/dev/null 2>&1); then
      pass "出貨包清單核對通過：$bundle"
    else
      fail "出貨包清單核對失敗：$bundle（檔案損壞或被改過）"
    fi
  else
    fail "看不懂出貨包：$bundle"
  fi
  # 讀得到已安裝版本才比先後。ANILA_PREFLIGHT_ROOT 只給測試把這段指到暫存。
  order_root="${ANILA_PREFLIGHT_ROOT:-$root}"
  installed="$(release_installed_version "$order_root")"
  if [[ -n "$installed" ]]; then
    bver="$(release_peek_bundle_version "$bundle")"
    if [[ -z "$bver" ]]; then
      fail "讀不到出貨包的版本，無法和已安裝的 ${installed} 比較"
    elif release_compare_versions "$bver" "$installed"; then
      pass "出貨包 ${bver} 比已安裝的 ${installed} 新"
    else
      rc=$?
      case "$rc" in
        1) fail "這一版已經安裝了（${bver}）" ;;
        2) fail "出貨包 ${bver} 比已安裝的 ${installed} 舊。要回到舊版請用 rollback" ;;
        *) fail "版本格式不對，無法比較 ${bver} 與 ${installed}" ;;
      esac
    fi
  fi
fi

echo
if (( FAILS > 0 )); then
  printf '%d 項沒過。照上面的指令處理完再跑一次。\n' "$FAILS"
  exit 1
fi
echo "全部通過，可以執行 anila-update.sh。"
