"""F5 + MEDIUM-A + MEDIUM-B：chunking-preview 的 HTTP 狀態碼要跟「誰的錯」說同一件事。

軸是 severity（不是 retryable）：severity=warning = 檔案的錯 → 4xx；
severity=error/critical = 伺服器的錯（組態／基礎設施／安全）→ 5xx。

MEDIUM-B：除了測純函式，另測「路由真的接上它」——走 TestClient、狀態碼＋
Content-Type 雙斷言（SPA catch-all 會回 200 text/html，只驗狀態碼會被騙）。
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from anila_core.ingestion.errors import ParseError, RemoteParseError, StoreError
from app.api.ingestion.preview import _ingestion_error_status


# ── 純函式 ────────────────────────────────────────────────────────────────

def test_remote_endpoint_down_maps_to_5xx() -> None:
    err = RemoteParseError.endpoint_unavailable(user_message="docling 端點無回應")
    assert err.retryable is True
    assert err.severity == "error"
    assert _ingestion_error_status(err) == 500


def test_bad_config_maps_to_5xx() -> None:
    """MEDIUM-A:E_PARSE_BAD_CONFIG 是維運 .env 打錯字——retryable=False 但
    severity=error,是伺服器端的錯,要 5xx(維運的告警看得到),不是 4xx。"""
    err = ParseError.bad_config(user_message="設定值不合法,請聯絡平台管理員")
    assert err.retryable is False
    assert err.severity == "error"
    assert _ingestion_error_status(err) == 500


def test_file_level_error_maps_to_4xx() -> None:
    err = ParseError.corrupt(user_message="檔案損毀")
    assert err.retryable is False
    assert err.severity == "warning"
    assert _ingestion_error_status(err) == 400


def test_format_unsupported_maps_to_4xx() -> None:
    err = ParseError.format_unsupported(user_message="不支援此格式")
    assert _ingestion_error_status(err) == 400


def test_rls_violation_critical_maps_to_5xx() -> None:
    err = StoreError.rls_violation(user_message="audit only")
    assert err.severity == "critical"
    assert _ingestion_error_status(err) == 500


# ── MEDIUM-B：路由真的接上映射（不是只有純函式）────────────────────────────

def test_route_returns_500_for_remote_endpoint_down(
    client: TestClient, db, monkeypatch,
):
    """遠端故障 → 路由回 500 application/json;改回硬寫 400 這條要紅。"""
    from tests.conftest import login, make_user

    make_user(db, username="prev-remote")
    token = login(client, username="prev-remote")

    def boom(filename, content, mime_type=None):
        raise RemoteParseError.endpoint_unavailable(
            user_message="docling 端點無回應"
        )

    # preview.py 用 `from ...parsers import extract_text`（綁定快照），要 patch
    # preview module 的名字才測得到路由，不是 parsers module 本身。
    monkeypatch.setattr("app.api.ingestion.preview.extract_text", boom)

    resp = client.post(
        "/api/ingestion/chunking-preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )
    assert resp.status_code == 500, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "端點無回應" in resp.json()["detail"]


def test_route_returns_400_for_corrupt_file(client: TestClient, db, monkeypatch):
    """檔案損毀 → 4xx(舊分支沒被 5xx 化弄壞)。"""
    from tests.conftest import login, make_user

    make_user(db, username="prev-corrupt")
    token = login(client, username="prev-corrupt")

    def boom(filename, content, mime_type=None):
        raise ParseError.corrupt(user_message="檔案無法解析")

    # preview.py 用 `from ...parsers import extract_text`（綁定快照），要 patch
    # preview module 的名字才測得到路由，不是 parsers module 本身。
    monkeypatch.setattr("app.api.ingestion.preview.extract_text", boom)

    resp = client.post(
        "/api/ingestion/chunking-preview",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )
    assert resp.status_code == 400, resp.text
    assert resp.headers["content-type"].startswith("application/json")


# ── MEDIUM-1 R7：分類軸在所有接縫同一答案 ─────────────────────────────────

def test_log_level_matches_severity_warning(client: TestClient, db, monkeypatch, caplog):
    """使用者的錯(severity=warning,如壞檔)要記 WARNING,不是 ERROR——
    那是日常事件,灌進 ERROR 會稀釋掉「維運要看的東西」。"""
    import logging

    from tests.conftest import login, make_user

    make_user(db, username="prev-log-warn")
    token = login(client, username="prev-log-warn")

    def boom(filename, content, mime_type=None):
        raise ParseError.corrupt(user_message="檔案無法解析")

    monkeypatch.setattr("app.api.ingestion.preview.extract_text", boom)

    with caplog.at_level(logging.WARNING, logger="app.api.ingestion.preview"):
        resp = client.post(
            "/api/ingestion/chunking-preview",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        )
    assert resp.status_code == 400
    recs = [r for r in caplog.records if "chunking-preview parse failed" in r.message]
    assert recs, "parse 失敗沒進 log"
    assert all(r.levelno == logging.WARNING for r in recs), (
        "severity=warning 的案例被記成 ERROR——與 attachment_service 的軸不一致"
    )


def test_log_level_matches_severity_error(client: TestClient, db, monkeypatch, caplog):
    """我們的錯(severity=error,如遠端端點掛)要記 ERROR(維運要看)。"""
    import logging

    from tests.conftest import login, make_user

    make_user(db, username="prev-log-err")
    token = login(client, username="prev-log-err")

    def boom(filename, content, mime_type=None):
        raise RemoteParseError.endpoint_unavailable(user_message="docling 端點無回應")

    monkeypatch.setattr("app.api.ingestion.preview.extract_text", boom)

    with caplog.at_level(logging.WARNING, logger="app.api.ingestion.preview"):
        resp = client.post(
            "/api/ingestion/chunking-preview",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("x.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
        )
    assert resp.status_code == 500
    recs = [r for r in caplog.records if "chunking-preview parse failed" in r.message]
    assert recs, "parse 失敗沒進 log"
    assert all(r.levelno >= logging.ERROR for r in recs), (
        "severity=error 的案例沒被記到 ERROR 以上——維運看不到"
    )
