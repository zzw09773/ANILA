#!/bin/sh
# 這個腳本不再備份。排程在 compose 的 backup 服務，產出在 share/backups/。
# 不要把這支加進 crontab。手順：docs/runbooks/csp-db-backup-restore.md
printf '%s\n' "排程備份已改由 compose 的 backup 服務負責，每天寫入 share/backups/。" >&2
printf '%s\n' "不要再手動跑這個腳本，也不要改 crontab。" >&2
printf '%s\n' "狀態看 share/backups/status.json，或治理中心儀表板「最後一次備份」。" >&2
printf '%s\n' "還原：docs/runbooks/csp-db-backup-restore.md" >&2
exit 1
