"""SSRF guard parity unit tests for /api/models register / update.

Phase 1 (模型 stack 解耦) 把 ``validate_outbound_url()`` 補上到 model
registry create / update — 之前完全沒守,admin 可以填任何 URL
(例如 ``http://csp-db:5432``) 然後 health_checker / proxy 就會去打。

這檔走 ``_enforce_endpoint_url()`` 直接 unit test (不過 HTTP / DB),
因為平台 conftest 在 SQLite 上跑會卡到既有 platform_links.required_roles
的 JSONB → SQLite 渲染問題,屬 pre-existing infra debt;本 PR 不修。
HTTP 路徑的端到端驗證在 cutover runbook 上手動跑 (見 plan §1.5)。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.models import _enforce_endpoint_url


# ── 預設 deny ──────────────────────────────────────────────────────────────────

def test_register_blocks_docker_service_name_without_trusted_hosts(monkeypatch):
    """沒設 ANILA_TRUSTED_HOSTS → docker service name 應該被擋。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://gemma4:8000/v1")
    assert exc.value.status_code == 400
    assert "single-label" in str(exc.value.detail)


@pytest.mark.parametrize("bad", [
    "http://csp-db:5432",
    "http://redis:6379",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://169.254.169.254/latest/meta-data/",
])
def test_register_blocks_internal_or_loopback_services(monkeypatch, bad):
    """CSP 內部 service / loopback / metadata 一律擋 — 不該被任何 flag 救。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url(bad)
    assert exc.value.status_code == 400


# ── allow-list 放行 ────────────────────────────────────────────────────────────

def test_register_allows_trusted_docker_service_name(monkeypatch):
    """把 service name 列進 ANILA_TRUSTED_HOSTS → 通過。
    這是 cross-stack 註冊 (anila-models-net 內 gemma4 / gpt-oss-20b /
    nv-embed-proxy) 的核心路徑,沒這條 Phase 1 就跑不了。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv(
        "ANILA_TRUSTED_HOSTS",
        "gemma4,gpt-oss-20b,nv-embed-proxy,host.docker.internal",
    )
    # 不該 raise
    _enforce_endpoint_url("http://gemma4:8000/v1")
    _enforce_endpoint_url("http://gpt-oss-20b:8000/v1")
    _enforce_endpoint_url("http://nv-embed-proxy:8000/v1")
    _enforce_endpoint_url("http://host.docker.internal:7011/v1")


# ── RFC 1918 仍受 ALLOW_PRIVATE_ENDPOINT 管 (沒動到既有合約) ─────────────────────

def test_register_blocks_rfc1918_without_allow_private(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://172.16.120.35:7000/v1")
    assert "ANILA_ALLOW_PRIVATE_ENDPOINT" in str(exc.value.detail)


def test_register_allows_rfc1918_with_allow_private(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    _enforce_endpoint_url("http://172.16.120.35:7000/v1")


# ── HTTPException shape (mirror agents.py) ─────────────────────────────────────

def test_enforce_raises_400_with_detail(monkeypatch):
    """錯誤路徑要回 HTTP 400 + 可讀 detail (frontend 的 alert() 會直接顯示)。
    Scheme 失敗 (HTTP 但沒 ALLOW_HTTP_ENDPOINT) 屬非 fixable,detail 是 plain string。
    """
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://gemma4:8000/v1")
    assert exc.value.status_code == 400
    # scheme failure → plain string detail
    assert isinstance(exc.value.detail, str) and exc.value.detail


# ── typed 400 for fixable failures (Phase 2) ───────────────────────────────────

def test_fixable_single_label_returns_typed_400(monkeypatch):
    """Single-label hostname (e.g. docker service name) failure → structured
    detail dict so frontend can render '加進 trusted hosts?' confirm modal。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://foobar:8000/v1")
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "untrusted_host"
    assert exc.value.detail["host"] == "foobar"
    assert exc.value.detail["reason"] == "single_label"
    assert "message" in exc.value.detail
    assert "hint" in exc.value.detail


def test_fixable_internal_zone_returns_typed_400(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://api.svc.cluster.local/v1")
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["host"] == "api.svc.cluster.local"
    assert exc.value.detail["reason"] == "internal_zone"


def test_loopback_NOT_fixable_returns_plain_string(monkeypatch):
    """Loopback / localhost 屬不可 fix 的安全擋,detail 保持 plain string
    避免前端誤渲染成 '加進 trusted hosts?' confirm modal。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://127.0.0.1:8000/v1")
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, str)  # NOT a dict — frontend won't offer fix


def test_metadata_address_NOT_fixable(monkeypatch):
    """169.254.169.254 (cloud metadata) — 永遠擋,絕不該變 fixable。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url("http://169.254.169.254/latest/")
    assert isinstance(exc.value.detail, str)
