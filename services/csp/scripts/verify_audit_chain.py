#!/usr/bin/env python3
"""P2.7 稽核帳驗證:「有沒有人動過稽核紀錄?」

平時不用跑。只有在有爭議、或還原過備份之後才跑。

用法(在 csp 容器內)::

    # 只驗本機鏈的自洽性
    python scripts/verify_audit_chain.py

    # 拿一份已經交出去的稽核匯出檔上印的鏈頭來比對（真正有力的那一種）
    python scripts/verify_audit_chain.py --head 9f2c...

    # 順手把「今天以前還沒封存的日子」補封起來
    python scripts/verify_audit_chain.py --seal

回傳碼:0 = 沒查到異常;1 = 查到異常(細節印在 stdout)。

⚠ 這支指令證明的是「**已錨定區間**沒有被改過」。持有本機 superuser 的人
仍然可以改寫最後一個檢查點之後的資料 —— 那個 ≤24h 的盲區是單機氣隙拓撲的
物理極限,不是實作不夠好。想縮小它,唯一的辦法是更常把匯出檔交出去。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal  # noqa: E402
from app.services import audit_ledger  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="驗證 ANILA 稽核帳是否被竄改（P2.7）",
    )
    parser.add_argument(
        "--head",
        help="已發出的稽核匯出檔上印的鏈頭（64 hex）。有給才驗得了 superuser 級竄改。",
    )
    parser.add_argument(
        "--seal",
        action="store_true",
        help="驗證前先把已經過完、還沒封存的日子封成檢查點。",
    )
    args = parser.parse_args(argv)

    if args.seal:
        # 封存要寫 audit_checkpoints，而 runtime role 對那張表只有 SELECT ——
        # ``seal_once`` 會自己開 migration 身分的 session
        # （需要 MIGRATION_DATABASE_URL，compose 已經有）。
        sealed = audit_ledger.seal_once()
        print(f"已封存 {len(sealed)} 個新檢查點。")

    # 驗證是純讀取，一般 runtime 連線就夠。
    db = SessionLocal()
    try:
        result = audit_ledger.verify_chain(db, expected_head=args.head)
    finally:
        db.close()

    print(audit_ledger.format_verification(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
