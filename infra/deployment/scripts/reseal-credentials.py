#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 SECRET_KEY 密封的憑證從舊密鑰轉封到新密鑰。

涵蓋 ``jwt_signing_keys``、``user_llm_credentials``，以及同樣用
``encrypt_credential`` 封起來的 service token／OIDC client secret。
預設只試算、不寫入。加上 ``--apply`` 才 commit。

更換 ``SECRET_KEY`` 之前一定要先跑這個。CSP 若解不開 active 簽章私鑰，
啟動時會直接拒絕，並指向這支指令。

用法（在 repo 根目錄，資料庫連線跟要改的那個環境相同）::

    PYTHONPATH=services/csp:packages/anila-core/src \\
      python infra/deployment/scripts/reseal-credentials.py \\
      --old-secret "$OLD_SECRET" --new-secret "$NEW_SECRET"

確認筆數後再::

    PYTHONPATH=services/csp:packages/anila-core/src \\
      python infra/deployment/scripts/reseal-credentials.py \\
      --old-secret "$OLD_SECRET" --new-secret "$NEW_SECRET" --apply

然後才把環境裡的 ``SECRET_KEY`` 改成新值並重建 CSP。
``scripts/reencrypt-credentials.py`` 只做同一把密鑰的 PBKDF2 迭代升級，
不能拿來換 SECRET_KEY。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "services" / "csp"))
sys.path.insert(0, str(_ROOT / "packages" / "anila-core" / "src"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用舊 SECRET_KEY 把密封憑證轉封到新 SECRET_KEY")
    parser.add_argument("--old-secret", required=True, help="目前列上的 SECRET_KEY（old_secret）")
    parser.add_argument("--new-secret", required=True, help="即將寫進環境的 SECRET_KEY")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="寫入並 commit。沒有這個旗標就只試算後 rollback。",
    )
    args = parser.parse_args(argv)

    from app.database import SessionLocal
    from app.services.credential_reseal import reseal_credentials

    db = SessionLocal()
    try:
        report = reseal_credentials(
            db, old_secret=args.old_secret, new_secret=args.new_secret
        )
        print(
            "jwt_signing_keys={jwt} user_llm_credentials={llm} "
            "agent_credentials={agent} service_clients={client} auth_providers={oidc}".format(
                jwt=report.jwt_signing_keys,
                llm=report.user_llm_credentials,
                agent=report.agent_credentials,
                client=report.service_clients,
                oidc=report.auth_providers,
            )
        )
        if args.apply:
            db.commit()
            print("applied")
        else:
            db.rollback()
            print("dry-run（未寫入）。要寫入請加 --apply")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
