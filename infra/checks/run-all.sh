#!/usr/bin/env bash
# 三人手動檢查入口（非 CI、不擋 commit／merge／deploy）。
#
#   infra/checks/run-all.sh          # 跑全部，結尾印摘要
#   infra/checks/run-all.sh orm      # 僅 ORM↔PG
#   infra/checks/run-all.sh contrast # 僅對比色
#   infra/checks/run-all.sh zh       # 僅簡體／大陸用語
#
# Exit: 0 全過；3 有發現；1 檢查壞了；2 用法錯。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CHECKS="$ROOT/infra/checks"

PY="${CHECKS_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in \
    "$ROOT/services/csp/.venv/bin/python" \
    "/home/c1147259/桌面/ANILA/anila-migration-20260706/ANILA/services/csp/.venv/bin/python"
  do
    if [[ -x "$candidate" ]]; then PY="$candidate"; break; fi
  done
fi
PY="${PY:-python3}"

which_run="${1:-all}"

run_orm() {
  echo "======== Check 1 · ORM ↔ PG ========"
  local dsn
  if ! dsn="$("$CHECKS/prepare_scratch_db.sh" --prepare)"; then
    echo "ORM: BROKEN（scratch DB / alembic）"
    return 1
  fi
  local rc=0
  local export_pp="$ROOT/services/csp:$ROOT/packages/anila-core/src${PYTHONPATH:+:$PYTHONPATH}"
  PYTHONPATH="$export_pp" \
    "$PY" "$CHECKS/check_orm_pg_drift.py" --mode drift --dsn "$dsn" || rc=$?
  local prc=0
  PYTHONPATH="$export_pp" \
    "$PY" "$CHECKS/check_orm_pg_drift.py" --mode policy || prc=$?
  "$CHECKS/prepare_scratch_db.sh" --destroy >/dev/null || true
  if [[ "$rc" -eq 1 || "$prc" -eq 1 ]]; then return 1; fi
  if [[ "$rc" -eq 3 || "$prc" -eq 3 ]]; then return 3; fi
  return 0
}

run_contrast() {
  echo "======== Check 2 · contrast ========"
  python3 "$CHECKS/check_contrast.py"
}

run_zh() {
  echo "======== Check 3 · zh-TW ========"
  # lint 用 exit 1=有發現；對外對齊成 3
  local rc=0
  "$ROOT/infra/ci/lint-zh-tw.sh" || rc=$?
  if [[ "$rc" -eq 1 ]]; then return 3; fi
  return "$rc"
}

rc_orm=0
rc_contrast=0
rc_zh=0
ran_orm=0
ran_contrast=0
ran_zh=0

case "$which_run" in
  all)
    ran_orm=1; ran_contrast=1; ran_zh=1
    run_orm || rc_orm=$?
    run_contrast || rc_contrast=$?
    run_zh || rc_zh=$?
    ;;
  orm) ran_orm=1; run_orm || rc_orm=$? ;;
  contrast) ran_contrast=1; run_contrast || rc_contrast=$? ;;
  zh) ran_zh=1; run_zh || rc_zh=$? ;;
  *) echo "未知檢查: $which_run（orm|contrast|zh|all）" >&2; exit 2 ;;
esac

echo
echo "======== 摘要 ========"
worst=0
print_one() {
  local label="$1" rc="$2" ran="$3"
  [[ "$ran" -eq 1 ]] || return 0
  local status
  case "$rc" in
    0) status="PASS" ;;
    3) status="FINDINGS"; [[ "$worst" -ne 1 ]] && worst=3 ;;
    1) status="BROKEN"; worst=1 ;;
    *) status="rc=$rc"; [[ "$worst" -eq 0 ]] && worst=$rc ;;
  esac
  printf "  %-10s %s\n" "$label" "$status"
}
print_one "ORM↔PG" "$rc_orm" "$ran_orm"
print_one "contrast" "$rc_contrast" "$ran_contrast"
print_one "zh-TW" "$rc_zh" "$ran_zh"

exit "$worst"
