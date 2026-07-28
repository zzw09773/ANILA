#!/usr/bin/env python3
"""匯出 CSP 的 OpenAPI schema,並守住 ``response_model`` 覆蓋率 ratchet(W2-12)。

為什麼需要這支
--------------
CSP 的 ``/openapi.json`` 被 ``require_admin`` 鎖住而且 ``ENABLE_API_DOCS``
預設 false ——**這是對的**(中科院內網不該讓訪客做 endpoint recon),但副作用是
repo 內完全沒有 CSP 的 API 契約產出物,只有 ``anila-studio`` 有
(``services/anila-studio/scripts/export-openapi.py``)。於是:

- 沒有人能在 review 時看見 API 形狀改了什麼(diff 不存在)。
- 273 個 endpoint 有 95 個沒有 ``response_model``,而缺 ``response_model``
  已經造成過生產事故:``app/services/startup_migrations.py:245-247`` 的註解
  自承「Pydantic ResponseValidationError was firing on GET /api/audit-logs
  because PG returned ints」。

本腳本照 studio 那支的寫法(直接 import ``app`` 呼 ``app.openapi()``,不啟服務、
不碰 DB/Redis),另外多做一件 studio 沒有的事:**覆蓋率 ratchet**。

ratchet 而非目標
----------------
刻意**不設**「要達到 100%」的目標——那會逼一次性大改 95 個 endpoint,風險遠大於
收益。改成只釘上限:**缺 ``response_model`` 的 endpoint 數只准降不准升**。
新 endpoint 想不寫 ``response_model`` 就會在 CI 撞牆,而既有債務可以慢慢還。

用法
----
    python scripts/export-openapi.py                # 重新產生產出物 + ratchet
    python scripts/export-openapi.py --check        # CI:比對不同步就 fail
    python scripts/export-openapi.py --write-ratchet  # 蓄意調降 ratchet 上限

決定性
------
``app.openapi()`` 的內容取決於 route table 與 ``settings``。``Settings`` 的
``model_config`` 帶 ``env_file=".env"``(相對 CWD),所以本腳本會:

1. 先 ``chdir`` 到暫存目錄——不讓開發機的 ``.env`` 汙染產出物,也讓
   ``setup_logging()`` 的 ``Path("logs").mkdir()`` 落在暫存目錄而非工作樹。
2. 明確 pin 會進 ``info`` 區塊的 ``APP_NAME`` / ``APP_VERSION``。

不這樣做的話,本機跑出來的產出物與 CI 跑出來的會不一樣,`--check` 變成噪音。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CSP_ROOT = REPO_ROOT / "services" / "csp"
OPENAPI_PATH = CSP_ROOT / "openapi" / "csp.openapi.json"
RATCHET_PATH = CSP_ROOT / "openapi" / "response-model-ratchet.json"

#: 匯入 ``app.main`` 前必須固定的環境。全部用覆寫(不是 setdefault):
#: 目的正是壓掉開發機或 CI runner 的既有值,讓產出物可重現。
_PINNED_ENV: dict[str, str] = {
    "APP_NAME": "CSP Platform",
    "APP_VERSION": "1.0.0",
    # 不連任何真 DB。schema 匯出不需要連線,engine 是 lazy 的。
    "DATABASE_URL": "sqlite:///./openapi-export.db",
    "SECRET_KEY": "openapi-export-only-not-a-real-secret",
    "ANILA_ALLOW_DEV_SECRET": "1",
    "ANILA_DEPLOYMENT_PROFILE": "openapi-export",
    "SKIP_STARTUP_MIGRATIONS": "true",
    "DEBUG": "false",
    "HEALTH_CHECK_INTERVAL": "3600",
    "AUTO_REGISTER_MODELS": "",
    "AUTO_REGISTER_AGENTS": "",
    "AUTO_SEED_API_KEYS": "",
    "AUTO_REGISTER_LINKS": "",
    "ALLOW_AUTO_KEYGEN": "true",
}


def _load_app() -> Any:
    """Import CSP 的 ``app``,在隔離的暫存 CWD 裡。"""
    for key, value in _PINNED_ENV.items():
        os.environ[key] = value

    sandbox = Path(tempfile.mkdtemp(prefix="anila-openapi-export-"))
    jwt_dir = sandbox / "jwt"
    jwt_dir.mkdir()
    os.environ["JWT_PRIVATE_KEY_PATH"] = str(jwt_dir / "jwt-private.pem")
    os.environ["JWT_PUBLIC_KEY_PATH"] = str(jwt_dir / "jwt-public.pem")

    if str(CSP_ROOT) not in sys.path:
        sys.path.insert(0, str(CSP_ROOT))

    os.chdir(sandbox)
    from app.main import app  # noqa: E402 — 環境與 sys.path 必須先就位

    return app


def _iter_documented_routes(app: Any):
    """列出「算 endpoint」的 route。

    排除條件與為什麼:

    - 非 ``APIRoute``:mount / WebSocket 不在 REST 契約內。
    - ``include_in_schema=False``:``/docs``、``/openapi.json`` 與 SPA
      catch-all。SPA catch-all **只在 frontend dist 存在時才註冊**,把它算進去
      的話覆蓋率會隨建置狀態漂動,ratchet 就變成假訊號。
    - 只有 HEAD/OPTIONS 的 route:框架自動產生,不是人寫的 endpoint。
    """
    from fastapi.routing import APIRoute

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.include_in_schema:
            continue
        methods = sorted(m for m in (route.methods or ()) if m not in ("HEAD", "OPTIONS"))
        if not methods:
            continue
        yield route, methods


def measure_response_model_coverage(app: Any) -> dict[str, Any]:
    total = 0
    with_model = 0
    missing: list[str] = []
    for route, methods in _iter_documented_routes(app):
        total += 1
        if route.response_model is not None:
            with_model += 1
        else:
            missing.append(f"{'/'.join(methods)} {route.path}")
    return {
        "endpoints_total": total,
        "endpoints_with_response_model": with_model,
        "endpoints_missing_response_model": total - with_model,
        "coverage_pct": round(100.0 * with_model / total, 2) if total else 0.0,
        "missing": sorted(missing),
    }


def render_openapi(app: Any) -> str:
    schema = app.openapi()
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def render_ratchet(coverage: dict[str, Any]) -> str:
    payload = {
        "_comment": (
            "W2-12 response_model 覆蓋率 ratchet。"
            "endpoints_missing_response_model 是**上限**:只准降不准升。"
            "新 endpoint 沒寫 response_model 會讓 CI fail。"
            "要蓄意調整請跑 scripts/export-openapi.py --write-ratchet 並在 PR 說明理由。"
        ),
        "baseline_recorded_on": date.today().isoformat(),
        "endpoints_total": coverage["endpoints_total"],
        "endpoints_with_response_model": coverage["endpoints_with_response_model"],
        "endpoints_missing_response_model": coverage["endpoints_missing_response_model"],
        "coverage_pct": coverage["coverage_pct"],
        "missing": coverage["missing"],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def cmd_write(app: Any, *, write_ratchet: bool) -> int:
    schema_text = render_openapi(app)
    _write(OPENAPI_PATH, schema_text)
    paths = len(json.loads(schema_text).get("paths", {}))
    print(f"wrote {OPENAPI_PATH.relative_to(REPO_ROOT)} ({paths} paths)")

    coverage = measure_response_model_coverage(app)
    if write_ratchet or not RATCHET_PATH.exists():
        _write(RATCHET_PATH, render_ratchet(coverage))
        print(
            f"wrote {RATCHET_PATH.relative_to(REPO_ROOT)} "
            f"(missing ceiling = {coverage['endpoints_missing_response_model']}, "
            f"coverage {coverage['coverage_pct']}%)"
        )
    else:
        print(
            f"ratchet 未動(現況 missing="
            f"{coverage['endpoints_missing_response_model']}, "
            f"coverage {coverage['coverage_pct']}%)。"
            "要調整上限請加 --write-ratchet。"
        )
    return 0


def cmd_check(app: Any) -> int:
    failures: list[str] = []

    expected = render_openapi(app)
    if not OPENAPI_PATH.exists():
        failures.append(
            f"{OPENAPI_PATH.relative_to(REPO_ROOT)} 不存在 —— "
            "跑 `python scripts/export-openapi.py` 產生後 commit。"
        )
    elif OPENAPI_PATH.read_text(encoding="utf-8") != expected:
        failures.append(
            f"{OPENAPI_PATH.relative_to(REPO_ROOT)} 與程式碼不同步 —— "
            "跑 `python scripts/export-openapi.py` 重新產生後 commit。"
        )

    coverage = measure_response_model_coverage(app)
    if not RATCHET_PATH.exists():
        failures.append(
            f"{RATCHET_PATH.relative_to(REPO_ROOT)} 不存在 —— ratchet baseline 缺失。"
        )
    else:
        baseline = json.loads(RATCHET_PATH.read_text(encoding="utf-8"))
        ceiling = int(baseline["endpoints_missing_response_model"])
        actual = coverage["endpoints_missing_response_model"]
        if actual > ceiling:
            new_missing = sorted(
                set(coverage["missing"]) - set(baseline.get("missing", []))
            )
            failures.append(
                f"response_model 覆蓋率退步:缺 response_model 的 endpoint "
                f"{ceiling} → {actual}(ratchet 只准降)。新增的:\n    "
                + "\n    ".join(new_missing or ["(清單無差異,但總數上升)"])
            )
        else:
            print(
                f"ratchet OK:missing {actual} <= ceiling {ceiling}"
                f"(coverage {coverage['coverage_pct']}%)"
            )
            if actual < ceiling:
                print(
                    "  ↳ 覆蓋率已優於 baseline。可跑 --write-ratchet 把上限鎖到現值。"
                )

    if failures:
        print("\n".join(f"FAIL: {f}" for f in failures), file=sys.stderr)
        return 1
    print("OpenAPI 產出物與程式碼同步。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比對,不寫檔;不同步或 ratchet 退步就 exit 1(CI 用)。",
    )
    parser.add_argument(
        "--write-ratchet",
        action="store_true",
        help="把 ratchet 上限重設為現值(蓄意調整時才用)。",
    )
    args = parser.parse_args()

    app = _load_app()
    if args.check:
        if args.write_ratchet:
            parser.error("--check 與 --write-ratchet 互斥")
        return cmd_check(app)
    return cmd_write(app, write_ratchet=args.write_ratchet)


if __name__ == "__main__":
    raise SystemExit(main())
