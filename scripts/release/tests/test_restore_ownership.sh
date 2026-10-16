#!/usr/bin/env bash
# Pure protocol tests: extract only the updater's restore functions, then replace
# every Docker/DB entry with shell functions. Never source the updater/test suite.
set -euo pipefail
REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
SCRIPT="${2:-$REPO/scripts/release/anila-update.sh}"
SQL_ASSET="$REPO/infra/deployment/scripts/assert-db-ownership.sql"
[[ -f "$SCRIPT" && -f "$SQL_ASSET" ]] || { printf 'required source asset missing\n' >&2; exit 2; }
WORK="$(mktemp -d /tmp/anila-restore-ownership-unit.XXXXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# Stdlib source read only, audit guard before source/file imports. -I -S -B does
# not load sitecustomize, user .pth or bytecode caches. No HOME or dotenv access.
python3 -I -S -B - "$SCRIPT" "$WORK/functions.sh" <<'PY'
import os,sys

def guard(event,args):
    if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
        parts=os.fsdecode(args[0]).replace('\\','/').split('/');base=parts[-1]
        if base.startswith('.env') or base.endswith(('.pem','.key')) or any(p in {'secrets','secret','credentials','keys','.aws'} for p in parts):raise PermissionError('protected path')
    if event in {'socket.connect','socket.bind','socket.getaddrinfo','subprocess.Popen','os.system','os.exec','os.posix_spawn'}:raise PermissionError('external entry')
sys.addaudithook(guard)
import hashlib,re
from pathlib import Path
raw=Path(sys.argv[1]).read_bytes();text=raw.decode();functions=[]
for name in ['verify_restored_database_ownership','restore_dump']:
    pattern=r'^'+re.escape(name)+r'\(\) \{\n.*?^\}\n'
    matches=list(re.finditer(pattern,text,re.M|re.S))
    if len(matches)>1 or name=='restore_dump' and len(matches)!=1:raise SystemExit('ambiguous function extraction')
    if matches:functions.append(matches[0].group())
Path(sys.argv[2]).write_text('\n'.join(functions))
print('source_sha256='+hashlib.sha256(raw).hexdigest())
print('full_script_sourced=false main_executed=false actual_sql_executed=false')
PY

docker() { printf 'forbidden_real_docker\n' >&2; return 99; }
psql() { printf 'forbidden_real_psql\n' >&2; return 99; }
pg_restore() { printf 'forbidden_real_pg_restore\n' >&2; return 99; }
assert_destructive_allowed() { printf 'guard\n' >> "$EVENTS"; }
stop_writers() { printf 'stop\n' >> "$EVENTS"; [[ "$MODE" != stop_failure ]]; }
info() { :; }
ok() { printf 'success\n' >> "$EVENTS"; }
die() { printf 'refused\n' >> "$EVENTS"; printf '%s\n' "$*" >&2; exit 1; }
dc() {
  local joined="$*" sql=''
  if [[ "$joined" == *' cp '* ]]; then
    printf 'copy\n' >> "$EVENTS"; [[ "$MODE" != copy_failure ]]; return
  fi
  if [[ "$joined" == *pg_restore* ]]; then
    printf 'restore\n' >> "$EVENTS"
    [[ "$joined" != *'--no-owner'* ]] || printf 'owner_flattened\n' >> "$EVENTS"
    [[ "$joined" == *'--exit-on-error'* ]] || return 96
    [[ "$MODE" != pg_restore_failure ]]; return
  fi
  if [[ "$joined" == *'CREATE DATABASE csp_restore'* ]]; then
    printf 'create\n' >> "$EVENTS"; [[ "$MODE" != create_failure ]]; return
  fi
  if [[ "$joined" == *'DROP SCHEMA IF EXISTS public CASCADE'* ]]; then
    printf 'schema\n' >> "$EVENTS"; [[ "$MODE" != schema_failure ]]; return
  fi
  if [[ "$joined" == *'version_num FROM alembic_version'* ]]; then
    printf 'revision\n' >> "$EVENTS"
    if [[ "$MODE" == revision_mismatch ]]; then printf 'r1_9999\n'; else printf 'r1_0062\n'; fi
    [[ "$MODE" != revision_query_failure ]]; return
  fi
  if [[ "$joined" == *'ALTER DATABASE csp RENAME TO csp_previous'* ]]; then
    printf 'promotion_attempt\n' >> "$EVENTS"
    [[ "$MODE" != switch_failure ]] || return 85
    printf 'promotion\n' >> "$EVENTS"; return 0
  fi
  if [[ "$joined" == *psql* && "$joined" == *csp_restore* && "$joined" != *' -c '* && "$joined" != *' -tAc '* ]]; then
    [[ "$joined" == *'ON_ERROR_STOP=1'* ]] || return 95
    sql="$(cat)"
    if [[ "$sql" == *boot_ddl_tables* && "$sql" == *'REVOKE ALL ON TABLE'* ]]; then
      printf 'repair\n' >> "$EVENTS"; [[ "$MODE" != repair_failure ]]; return
    fi
    if [[ "$sql" == *RESTORE_RUNTIME_ROLE_INVALID* && "$sql" == *RESTORE_REQUIRED_TABLES_MISSING* && "$sql" == *RESTORE_AUDIT_PRIVILEGES_INVALID* && "$sql" == *RESTORE_CHECKPOINT_INSERT_ALLOWED* ]]; then
      printf 'gate\n' >> "$EVENTS"
      # Simulated server rejection, not execution of SQL or proof of SQL semantics.
      case "$MODE" in
        posture_failure|posture_query_failure|role_missing|role_superuser|role_bypassrls|table_missing|boot_owner_bad|audit_owner_bad|audit_update_allowed|audit_delete_allowed|audit_truncate_allowed|checkpoint_insert_allowed) return 86 ;;
      esac
      return 0
    fi
    printf 'unexpected_sql\n' >> "$EVENTS"; return 94
  fi
  if [[ "$joined" == *'DROP DATABASE IF EXISTS csp_restore'* ]]; then printf 'cleanup\n' >> "$EVENTS"; return 0; fi
  printf 'unexpected_command\n' >> "$EVENTS"; return 93
}
source "$WORK/functions.sh"

has_event() { grep -q "^$1$" "$EVENTS"; }
first_event() { grep -n "^$1$" "$EVENTS" | head -1 | cut -d: -f1; }
passed=0; failed=0
for MODE in success stop_failure copy_failure create_failure schema_failure pg_restore_failure revision_mismatch revision_query_failure repair_failure posture_failure posture_query_failure missing_sql symlink_sql role_missing role_superuser role_bypassrls table_missing boot_owner_bad audit_owner_bad audit_update_allowed audit_delete_allowed audit_truncate_allowed checkpoint_insert_allowed switch_failure; do
  CASE="$WORK/$MODE"; mkdir -p "$CASE/tree/infra/deployment/scripts"
  EVENTS="$CASE/events"; : > "$EVENTS"
  printf 'PGDMP_SYNTHETIC_NOT_A_REAL_DUMP\n' > "$CASE/db.dump"
  printf 'r1_0062\n' > "$CASE/db.dump.alembic"
  if [[ "$MODE" == symlink_sql ]]; then
    ln -s "$SQL_ASSET" "$CASE/tree/infra/deployment/scripts/assert-db-ownership.sql"
  elif [[ "$MODE" != missing_sql ]]; then
    cp "$SQL_ASSET" "$CASE/tree/infra/deployment/scripts/assert-db-ownership.sql"
  fi
  rc=0
  if ( trap - EXIT; restore_dump "$CASE/tree" "$CASE/db.dump" ) < /dev/null > "$CASE/out" 2> "$CASE/err"; then rc=0; else rc=$?; fi
  good=1
  if [[ "$MODE" == success ]]; then
    [[ "$rc" == 0 ]] || good=0
    for event in stop copy create schema restore revision repair gate promotion success; do has_event "$event" || good=0; done
    has_event owner_flattened && good=0
    if [[ "$good" == 1 ]]; then
      [[ "$(first_event revision)" -lt "$(first_event repair)" && "$(first_event repair)" -lt "$(first_event gate)" && "$(first_event gate)" -lt "$(first_event promotion)" ]] || good=0
    fi
  elif [[ "$MODE" == switch_failure ]]; then
    [[ "$rc" != 0 ]] || good=0
    has_event success && good=0
  else
    [[ "$rc" != 0 ]] || good=0
    has_event promotion_attempt && good=0
    has_event success && good=0
  fi
  for unexpected in unexpected_command unexpected_sql; do has_event "$unexpected" && good=0; done
  if [[ "$good" == 1 ]]; then passed=$((passed+1)); printf 'PASS %s\n' "$MODE"; else failed=$((failed+1)); printf 'FAIL %s exit=%s\n' "$MODE" "$rc"; fi
done
printf 'protocol_cases=24 passed=%s failed=%s skipped=0 actual_sql_executed=false\n' "$passed" "$failed"
[[ "$failed" == 0 ]]
