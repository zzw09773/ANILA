#!/bin/sh
# csp-db 啟動時依看得到的記憶體決定 Postgres 參數。
# 不新增操作者要填的環境變數。cgroup 有上限且小於 MemTotal 時用上限，否則用 MemTotal。
# 測試可設 POSTGRES_MEMINFO、POSTGRES_CGROUP_MAX。POSTGRES_MEMCONF_LIB=1 時只定義函式。

postgres_compute() {
  # RAM/16（128MB–32GB）、RAM/2（128MB–256GB）、RAM/32（64MB–2GB）。
  # work_mem 是 5% 記憶體再除以 max_connections，4MB–64MB。
  kib=$1
  shared=$((kib / 16 / 1024))
  if [ "$shared" -lt 128 ]; then
    shared=128
  fi
  if [ "$shared" -gt 32768 ]; then
    shared=32768
  fi
  cache=$((kib / 2 / 1024))
  if [ "$cache" -lt 128 ]; then
    cache=128
  fi
  if [ "$cache" -gt 262144 ]; then
    cache=262144
  fi
  maint=$((kib / 32 / 1024))
  if [ "$maint" -lt 64 ]; then
    maint=64
  fi
  if [ "$maint" -gt 2048 ]; then
    maint=2048
  fi
  work=$((kib * 5 / 100 / 120 / 1024))
  if [ "$work" -lt 4 ]; then
    work=4
  fi
  if [ "$work" -gt 64 ]; then
    work=64
  fi
}

_positive_kib() {
  case "$1" in
    ''|*[!0-9]*|0) return 1 ;;
  esac
  return 0
}

postgres_visible_kib() {
  meminfo="${POSTGRES_MEMINFO:-/proc/meminfo}"
  total=
  if [ -r "$meminfo" ]; then
    raw_total=$(awk '/^MemTotal:/ { print $2; exit }' "$meminfo")
    if _positive_kib "$raw_total"; then
      total=$raw_total
    fi
  fi
  if [ "${POSTGRES_CGROUP_MAX+x}" = x ]; then
    cgroup_file=$POSTGRES_CGROUP_MAX
  elif [ -r /sys/fs/cgroup/memory.max ]; then
    cgroup_file=/sys/fs/cgroup/memory.max
  elif [ -r /sys/fs/cgroup/memory/memory.limit_in_bytes ]; then
    cgroup_file=/sys/fs/cgroup/memory/memory.limit_in_bytes
  else
    cgroup_file=
  fi
  limit_kib=
  if [ -n "$cgroup_file" ] && [ -r "$cgroup_file" ]; then
    raw=$(tr -d '[:space:]' < "$cgroup_file")
    case "$raw" in
      max | "" | *[!0-9]*)
        ;;
      *)
        candidate=$((raw / 1024))
        if _positive_kib "$candidate"; then
          limit_kib=$candidate
        fi
        ;;
    esac
  fi
  if [ -n "$limit_kib" ] && [ -n "$total" ] && [ "$limit_kib" -lt "$total" ]; then
    kib=$limit_kib
  elif [ -n "$total" ]; then
    kib=$total
  elif [ -n "$limit_kib" ]; then
    kib=$limit_kib
  else
    return 1
  fi
  printf '%s\n' "$kib"
}

if [ "${POSTGRES_MEMCONF_LIB:-}" = 1 ]; then
  return 0
fi

if ! kib=$(postgres_visible_kib); then
  printf '警告：讀不到容器記憶體（cgroup 與 MemTotal 都不可用），記憶體參數用 Postgres 內建預設。\n' >&2
  exec docker-entrypoint.sh postgres -c max_connections=120
fi
postgres_compute "$kib"
printf 'csp-db 記憶體設定：可見 %s KiB，shared_buffers=%sMB，effective_cache_size=%sMB，maintenance_work_mem=%sMB，work_mem=%sMB，max_connections=120\n' \
  "$kib" "$shared" "$cache" "$maint" "$work" >&2
exec docker-entrypoint.sh postgres \
  -c max_connections=120 \
  -c "shared_buffers=${shared}MB" \
  -c "effective_cache_size=${cache}MB" \
  -c "maintenance_work_mem=${maint}MB" \
  -c "work_mem=${work}MB"
