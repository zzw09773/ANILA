#!/bin/sh
# 只做保留策略：14 份每日、6 份每月。備份迴圈成功後也會呼叫同一支函式。
set -eu

HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
# shellcheck disable=SC1091
. "$HERE/backup-lib.sh"

root=${1:-${ANILA_BACKUP_DIR:?set ANILA_BACKUP_DIR or pass a directory}}
keep_d=${ANILA_BACKUP_KEEP_DAILY:-14}
keep_m=${ANILA_BACKUP_KEEP_MONTHLY:-6}
prune_backups "$root" "$keep_d" "$keep_m"
