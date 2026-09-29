"""在 csp 容器裡寫入更新稽核。給 anila-update.sh 呼叫。

    python -m app.platform_release_cli \\
        --action update --operator c1147259 \\
        --from-version 2026.09.29-1 --to-version 2026.09.29-2 \\
        --result success
"""
from __future__ import annotations

import argparse
import sys

from app.database import SessionLocal
from app.services.platform_release import record_platform_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="寫入平台更新稽核")
    parser.add_argument("--action", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--from-version", required=True)
    parser.add_argument("--to-version", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    operator = args.operator.strip()[:100]
    if not operator:
        print("更新紀錄缺欄位", file=sys.stderr)
        return 2
    db = SessionLocal()
    try:
        record_platform_event(
            db,
            action=args.action,
            operator=operator,
            from_version=args.from_version,
            to_version=args.to_version,
            result=args.result,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
