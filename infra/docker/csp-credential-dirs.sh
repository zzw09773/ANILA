#!/bin/sh
# 在 CSP 降權之前把憑證子目錄準備好。
# 擁有者是 10005：沒有服務以這個 uid 跑。mode 2770，只有該群組進得去。
# CSP、anila-studio、ingestion-worker 都是 uid 10001；若子目錄也屬於
# 10001，它們會讀到彼此的憑證檔。
#
# 升級：根目錄的扁平 <client>.token 是舊版憑證（uid 10001、mode 0640）。
# 同 uid 的 studio / worker 即使唯讀掛載也讀得到。只清這一層，不動子目錄
# 裡的 token，也不跟著符號連結寫到別處。真的有刪掉檔案才留下標記，讓
# CSP 把 router-primary 輪替一次且不留寬限。沒有扁平檔的下次啟動不再標記，
# 避免每一輪都換權杖。
set -eu
root=${1:-${ANILA_SERVICE_CLIENT_DIR:-/run/anila/service-clients}}
mkdir -p "$root"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  fi
}

as_root chown 10001:10002 "$root"
chmod 2750 "$root"

install_dir() {
  name=$1
  gid=$2
  dir="$root/$name"
  mkdir -p "$dir"
  as_root chown 10005:"$gid" "$dir"
  chmod 2770 "$dir"
}

install_dir router-primary 10002
install_dir anila-studio 10003
install_dir ingestion-worker 10004

# find -type f 不會選到符號連結。先覆寫再刪，硬連結也不留舊明文。
olds=$(find "$root" -maxdepth 1 -type f -name '*.token' || true)
if [ -n "$olds" ]; then
  printf '%s\n' "$olds" | while IFS= read -r old; do
    [ -n "$old" ] || continue
    if [ -L "$old" ]; then
      continue
    fi
    dd if=/dev/zero of="$old" bs=4096 count=1 conv=notrunc status=none 2>/dev/null || true
    : > "$old" || true
    rm -f -- "$old"
  done
  marker="$root/.force-rotate-router-primary"
  if [ -L "$marker" ]; then
    rm -f -- "$marker"
  fi
  : > "$marker"
  as_root chown 10001:10002 "$marker"
  chmod 0640 "$marker"
fi
