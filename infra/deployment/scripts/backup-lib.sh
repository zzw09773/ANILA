#!/bin/sh
# 每日備份的共用函式。給 backup-loop.sh / backup-prune.sh 用，也給
# compose 的 backup 服務（alpine 的 /bin/sh，不是 bash）用。
#
# 目錄（ANILA_BACKUP_DIR，容器內固定 /backups）：
#   daily/YYYYMMDD-HHMMSS/   完整的一輪才會出現在這裡
#   monthly/YYYYMM/          該月第一份成功備份（同檔案系統時硬連結）
#   LATEST                   一行，指向最近一次成功的 daily/ 相對路徑
#   status.json              最近一輪的結果；失敗時保留上次成功的時間
# 寫入一律先到暫名再 rename。拉備份的人只會看到完整檔。

# 要打包的目錄（相對 ANILA_BACKUP_SRC）。順序就是 tar 檔名。
# router-sessions 排除 state/：那裡可能有後援用的服務憑證，不進每日包。
backup_sources() {
  printf '%s\n' \
    "uploads files-uploads.tar 0" \
    "attachments files-attachments.tar 0" \
    "static files-static.tar 0" \
    "pki files-pki.tar 0" \
    "quickstart files-quickstart.tar 0" \
    "studio-artifacts files-studio-artifacts.tar 0" \
    "router-sessions files-router-sessions.tar 1" \
    "n8n files-n8n.tar 0"
}

iso_now() {
  if [ -n "${ANILA_BACKUP_NOW:-}" ]; then
    printf '%s\n' "$ANILA_BACKUP_NOW"
    return 0
  fi
  date -u +%Y-%m-%dT%H:%M:%SZ
}

stamp_now() {
  if [ -n "${ANILA_BACKUP_STAMP:-}" ]; then
    printf '%s\n' "$ANILA_BACKUP_STAMP"
    return 0
  fi
  date -u +%Y%m%d-%H%M%S
}

month_now() {
  if [ -n "${ANILA_BACKUP_MONTH:-}" ]; then
    printf '%s\n' "$ANILA_BACKUP_MONTH"
    return 0
  fi
  if [ -e /usr/share/zoneinfo/Asia/Taipei ]; then
    TZ=Asia/Taipei date +%Y%m
    return 0
  fi
  date -u +%Y%m
}

# 只刪我們命名的每日／每月目錄。LATEST、status.json、.incoming、其他名字留著。
prune_backups() {
  root=$1
  keep_daily=$2
  keep_monthly=$3
  _prune_dirs "$root/daily" "$keep_daily" \
    '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'
  _prune_dirs "$root/monthly" "$keep_monthly" \
    '[0-9][0-9][0-9][0-9][0-9][0-9]'
}

_prune_dirs() {
  parent=$1
  keep=$2
  pattern=$3
  [ -d "$parent" ] || return 0
  list=$(find "$parent" -mindepth 1 -maxdepth 1 -type d -name "$pattern" | sort)
  [ -n "$list" ] || return 0
  count=$(printf '%s\n' "$list" | wc -l | tr -d '[:space:]')
  drop=$((count - keep))
  [ "$drop" -gt 0 ] || return 0
  printf '%s\n' "$list" | head -n "$drop" | while IFS= read -r dir; do
    [ -n "$dir" ] || continue
    rm -rf "$dir"
  done
}

read_prev_success() {
  PREV_SUCCESS_AT=""
  PREV_SUCCESS_SIZE=0
  prev="$BACKUP_DIR/status.json"
  [ -f "$prev" ] || return 0
  PREV_SUCCESS_AT=$(sed -n 's/.*"last_success_at"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$prev" | head -n 1)
  PREV_SUCCESS_SIZE=$(sed -n 's/.*"last_success_size_bytes"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$prev" | head -n 1)
  if [ -z "$PREV_SUCCESS_SIZE" ]; then
    PREV_SUCCESS_SIZE=0
  fi
}

write_status() {
  result=$1
  size=$2
  snapshot=$3
  err=$4
  success_at=$5
  success_size=$6
  if [ -n "$success_at" ]; then
    sat="\"$success_at\""
  else
    sat=null
  fi
  if [ -z "$success_size" ]; then
    success_size=0
  fi
  if [ -n "$snapshot" ]; then
    snap="\"$snapshot\""
  else
    snap=null
  fi
  tmp="$BACKUP_DIR/status.json.tmp"
  printf '%s\n' \
    "{\"schema\":1,\"last_run_at\":\"$RUN_AT\",\"last_result\":\"$result\",\"last_size_bytes\":$size,\"last_success_at\":$sat,\"last_success_size_bytes\":$success_size,\"snapshot\":$snap,\"error\":\"$err\"}" \
    > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$BACKUP_DIR/status.json"
}

write_latest() {
  rel=$1
  tmp="$BACKUP_DIR/LATEST.tmp"
  printf '%s\n' "$rel" > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$BACKUP_DIR/LATEST"
}

snapshot_bytes() {
  dir=$1
  total=0
  for f in "$dir"/*; do
    [ -f "$f" ] || continue
    n=$(wc -c < "$f" | tr -d '[:space:]')
    total=$((total + n))
  done
  printf '%s\n' "$total"
}

# pg_dump 寫到呼叫端給的路徑。測試可把 ANILA_PG_DUMP_CMD 指到一支假程式。
run_pg_dump() {
  out=$1
  # 測試把這支指到一支 shell 腳本。用 sh 跑，避免暫存目錄掛 noexec。
  if [ -n "${ANILA_PG_DUMP_CMD:-}" ]; then
    sh "$ANILA_PG_DUMP_CMD" "$out"
    return $?
  fi
  host=${PGHOST:-csp-db}
  user=${POSTGRES_USER:-csp}
  db=${POSTGRES_DB:-csp}
  pass=${POSTGRES_PASSWORD:-}
  if [ -z "$pass" ]; then
    return 1
  fi
  passfile=$(mktemp)
  chmod 600 "$passfile"
  esc=$(printf '%s' "$pass" | sed 's/\\/\\\\/g; s/:/\\:/g')
  printf '%s:5432:%s:%s:%s\n' "$host" "$db" "$user" "$esc" > "$passfile"
  PGPASSFILE=$passfile pg_dump -h "$host" -p 5432 -U "$user" -d "$db" \
    -Fc --no-password -f "$out"
  rc=$?
  rm -f "$passfile"
  unset PGPASSFILE
  return $rc
}

_file_is_sqlite() {
  hdr=$(dd if="$1" bs=15 count=1 2>/dev/null || true)
  [ "$hdr" = "SQLite format 3" ]
}

_sqlite_backup() {
  _sql_src=$1
  _sql_dest=$2
  rm -f "$_sql_dest"
  mkdir -p "$(dirname "$_sql_dest")"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$_sql_src" ".backup '$_sql_dest'"
    return $?
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$_sql_src" "$_sql_dest" <<'PY'
import sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
source = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
target = sqlite3.connect(dest)
with target:
    source.backup(target)
source.close()
PY
    return $?
  fi
  printf '%s\n' "sqlite backup needs sqlite3 or python3" >&2
  return 1
}

# 先把 SQLite 做成一致性複本，其餘檔案用 tar 讀。tar 回 1（檔案在讀取途中
# 被改寫）或更大的錯誤都不發布，交給外層重試。
_stage_tree() {
  srcdir=$1
  staging=$2
  exclude_state=$3
  list=$(mktemp) || return 1
  find "$srcdir" -type f > "$list" || { rm -f "$list"; return 1; }
  sqlite_list=$(mktemp) || { rm -f "$list"; return 1; }
  : > "$sqlite_list"
  while IFS= read -r srcfile; do
    [ -n "$srcfile" ] || continue
    rel=${srcfile#"$srcdir"/}
    case "$rel" in
      state|state/*)
        if [ "$exclude_state" = "1" ]; then
          continue
        fi
        ;;
    esac
    if _file_is_sqlite "$srcfile"; then
      printf '%s\n' "$rel" >> "$sqlite_list"
      if ! _sqlite_backup "$srcfile" "$staging/$rel"; then
        rm -f "$list" "$sqlite_list"
        return 1
      fi
    fi
  done < "$list"
  rm -f "$list"
  excludes=$(mktemp) || { rm -f "$sqlite_list"; return 1; }
  : > "$excludes"
  if [ "$exclude_state" = "1" ]; then
    printf '%s\n' "state" "./state" >> "$excludes"
  fi
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    printf '%s\n' "$rel" "./$rel" >> "$excludes"
  done < "$sqlite_list"
  rm -f "$sqlite_list"
  bundle=$(mktemp) || { rm -f "$excludes"; return 1; }
  tar_rc=0
  if [ -s "$excludes" ]; then
    tar --exclude-from="$excludes" -C "$srcdir" -cf "$bundle" . || tar_rc=$?
  else
    tar -C "$srcdir" -cf "$bundle" . || tar_rc=$?
  fi
  rm -f "$excludes"
  if [ "$tar_rc" -ne 0 ]; then
    rm -f "$bundle"
    return 1
  fi
  tar -C "$staging" -xf "$bundle" || { rm -f "$bundle"; return 1; }
  rm -f "$bundle"
  return 0
}

tar_tree() {
  srcdir=$1
  outfile=$2
  exclude_state=$3
  partial="$outfile.partial"
  staging=$(mktemp -d "${TMPDIR:-/tmp}/anila-tar.XXXXXX") || return 1
  if ! _stage_tree "$srcdir" "$staging" "$exclude_state"; then
    rm -rf "$staging"
    rm -f "$partial"
    return 1
  fi
  tar -C "$staging" -czf "$partial" .
  rc=$?
  rm -rf "$staging"
  if [ "$rc" -ne 0 ]; then
    rm -f "$partial"
    return 1
  fi
  if [ ! -s "$partial" ]; then
    rm -f "$partial"
    return 1
  fi
  mv -f "$partial" "$outfile"
  return 0
}

_file_sha256() {
  _hash_file=$1
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$_hash_file" | awk '{print $1}'
    return
  fi
  python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$_hash_file"
}

_same_file_set() {
  _cmp_src=$1
  _cmp_dest=$2
  for f in "$_cmp_src"/*; do
    [ -f "$f" ] || continue
    _cmp_base=$(basename "$f")
    [ -f "$_cmp_dest/$_cmp_base" ] || return 1
    _cmp_a=$(_file_sha256 "$f")
    _cmp_b=$(_file_sha256 "$_cmp_dest/$_cmp_base")
    [ "$_cmp_a" = "$_cmp_b" ] || return 1
  done
  for f in "$_cmp_dest"/*; do
    [ -f "$f" ] || continue
    _cmp_base=$(basename "$f")
    [ -f "$_cmp_src/$_cmp_base" ] || return 1
  done
  return 0
}

publish_monthly() {
  month=$1
  src=$2
  dest="$BACKUP_DIR/monthly/$month"
  if [ -d "$dest" ] && _same_file_set "$src" "$dest"; then
    return 0
  fi
  parent="$BACKUP_DIR/monthly"
  mkdir -p "$parent" || return 1
  staging="$parent/.staging-$month"
  rm -rf "$staging"
  mkdir -p "$staging" || return 1
  for f in "$src"/*; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    if ! ln "$f" "$staging/$base" 2>/dev/null; then
      cp -a "$f" "$staging/$base" || { rm -rf "$staging"; return 1; }
    fi
  done
  if ! _same_file_set "$src" "$staging"; then
    rm -rf "$staging"
    return 1
  fi
  if [ -d "$dest" ]; then
    trash="$parent/.trash-$month-$$"
    if ! mv "$dest" "$trash"; then
      rm -rf "$staging"
      return 1
    fi
    if ! mv "$staging" "$dest"; then
      mv "$trash" "$dest" 2>/dev/null || true
      rm -rf "$staging"
      return 1
    fi
    rm -rf "$trash"
    return 0
  fi
  if [ -e "$dest" ]; then
    rm -rf "$staging"
    return 1
  fi
  mv "$staging" "$dest" || { rm -rf "$staging"; return 1; }
  return 0
}

finish_perms() {
  chmod 755 "$BACKUP_DIR"
  if [ -f "$BACKUP_DIR/status.json" ]; then
    chmod 644 "$BACKUP_DIR/status.json"
  fi
  if [ -f "$BACKUP_DIR/LATEST" ]; then
    chmod 644 "$BACKUP_DIR/LATEST"
  fi
  if [ -d "$BACKUP_DIR/daily" ]; then
    chmod 750 "$BACKUP_DIR/daily"
  fi
  if [ -d "$BACKUP_DIR/monthly" ]; then
    chmod 750 "$BACKUP_DIR/monthly"
  fi
  for tree in "$BACKUP_DIR/daily" "$BACKUP_DIR/monthly"; do
    [ -d "$tree" ] || continue
    find "$tree" -mindepth 1 -maxdepth 1 -type d -exec chmod 750 {} \;
    find "$tree" -type f -exec chmod 640 {} \;
  done
  uid=${BACKUP_FILE_UID:-}
  if [ -n "$uid" ]; then
    gid=${BACKUP_FILE_GID:-$uid}
    chown -R "$uid:$gid" "$BACKUP_DIR" 2>/dev/null || true
    chmod 755 "$BACKUP_DIR"
    if [ -f "$BACKUP_DIR/status.json" ]; then
      chmod 644 "$BACKUP_DIR/status.json"
    fi
    if [ -f "$BACKUP_DIR/LATEST" ]; then
      chmod 644 "$BACKUP_DIR/LATEST"
    fi
  fi
}

fail_run() {
  code=$1
  if [ -n "${INCOMING:-}" ]; then
    rm -rf "$INCOMING"
  fi
  if [ -d "$BACKUP_DIR/.incoming" ]; then
    rmdir "$BACKUP_DIR/.incoming" 2>/dev/null || rm -rf "$BACKUP_DIR/.incoming"
  fi
  write_status failure 0 "" "$code" "$PREV_SUCCESS_AT" "$PREV_SUCCESS_SIZE"
  finish_perms
  return 1
}

backup_once() {
  set +e
  _backup_once_impl
  rc=$?
  set -e
  return $rc
}

_backup_once_impl() {
  BACKUP_DIR=${ANILA_BACKUP_DIR:-/backups}
  SRC=${ANILA_BACKUP_SRC:-/backup-src}
  mkdir -p "$BACKUP_DIR" || return 1
  chmod 755 "$BACKUP_DIR" || return 1
  RUN_AT=$(iso_now) || return 1
  STAMP=$(stamp_now) || return 1
  MONTH=$(month_now) || return 1
  read_prev_success || return 1

  # 上一輪若在 publish 前死掉，暫存目錄不該被拉走。
  rm -rf "$BACKUP_DIR/.incoming"
  INCOMING="$BACKUP_DIR/.incoming/$STAMP"
  mkdir -p "$INCOMING" || {
    fail_run publish_failed
    return 1
  }

  if [ "${ANILA_BACKUP_FAIL_BEFORE_PUBLISH:-}" = "1" ]; then
    printf 'partial' > "$INCOMING/db.dump.partial" || true
    fail_run publish_failed
    return 1
  fi

  # 來源目錄必須在；少一個掛載就不要產出「看起來完整」的快照。
  # 不用管線：管線裡的 while 是子殼，漏掉的目錄會被悄悄吃掉。
  listfile="$INCOMING/.sources"
  backup_sources > "$listfile" || {
    fail_run source_missing
    return 1
  }
  while IFS= read -r src_line; do
    [ -n "$src_line" ] || continue
    set -- $src_line
    if [ ! -d "$SRC/$1" ]; then
      rm -f "$listfile"
      fail_run source_missing
      return 1
    fi
  done < "$listfile"

  dump_partial="$INCOMING/db.dump.partial"
  if ! run_pg_dump "$dump_partial"; then
    fail_run pg_dump_failed
    return 1
  fi
  dump_size=$(wc -c < "$dump_partial" | tr -d '[:space:]')
  if [ "$dump_size" -lt 1000 ]; then
    fail_run dump_too_small
    return 1
  fi
  mv -f "$dump_partial" "$INCOMING/db.dump" || {
    fail_run publish_failed
    return 1
  }

  while IFS= read -r src_line; do
    [ -n "$src_line" ] || continue
    set -- $src_line
    if ! tar_tree "$SRC/$1" "$INCOMING/$2" "$3"; then
      rm -f "$listfile"
      fail_run tar_failed
      return 1
    fi
  done < "$listfile"
  rm -f "$listfile"

  mkdir -p "$BACKUP_DIR/daily" || {
    fail_run publish_failed
    return 1
  }
  # 目錄改名是這輪對外可見的瞬間。在這之前 LATEST 與 status 都還指著上一份。
  if ! mv "$INCOMING" "$BACKUP_DIR/daily/$STAMP"; then
    fail_run publish_failed
    return 1
  fi
  rmdir "$BACKUP_DIR/.incoming" 2>/dev/null || true
  INCOMING=""

  if ! publish_monthly "$MONTH" "$BACKUP_DIR/daily/$STAMP"; then
    fail_run monthly_failed
    return 1
  fi
  if ! prune_backups "$BACKUP_DIR" "${ANILA_BACKUP_KEEP_DAILY:-14}" "${ANILA_BACKUP_KEEP_MONTHLY:-6}"; then
    fail_run publish_failed
    return 1
  fi
  total=$(snapshot_bytes "$BACKUP_DIR/daily/$STAMP") || total=0
  if ! write_latest "daily/$STAMP"; then
    fail_run publish_failed
    return 1
  fi
  if ! write_status success "$total" "daily/$STAMP" "" "$RUN_AT" "$total"; then
    fail_run publish_failed
    return 1
  fi
  finish_perms || return 1
  return 0
}
