"""Slice 6a — ``validate_outbound_url`` endpoint_kind domain split.

http-scheme 放寬旗標按端點類型分域(Slice 6a;model 域原「production 無條件
拒收」不變量已由 PLAN.md P0.2「2026-07-29 拍板」改為旗標統一判定)。

- ``model``   — http 由 ``ANILA_ALLOW_HTTP_ENDPOINT`` 明確放行,預設拒收;
                production 與 dev 同樣依此旗標判定(PLAN.md P0.2,
                2026-07-29 拍板:內網模型 gateway 走 http)。
- ``agent``   — http 由 ``ANILA_ALLOW_HTTP_AGENT_ENDPOINT`` 放行;legacy
                ``ANILA_ALLOW_HTTP_ENDPOINT`` 仍 fallback(帶 deprecation 警告,
                內網 MLSteam 純 http NodePort agent 靠它)。
- ``generic`` — 既有全域語意(預設 kind);既有呼叫端零行為變更。

所有 host / IP / DNS / trusted-host 檢查在三種 kind 完全相同 —— 分域只碰
scheme。旗標每次呼叫即讀 env,monkeypatch 即時生效。
"""
from __future__ import annotations

import logging

import pytest

from anila_core.security import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_GENERIC,
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

_HTTP_MODEL = "ANILA_ALLOW_HTTP_ENDPOINT"
_HTTP_AGENT = "ANILA_ALLOW_HTTP_AGENT_ENDPOINT"
_ENV = "ANILA_ENV"
_PUBLIC = "http://api.example.com/v1"
_PUBLIC_HTTPS = "https://api.example.com/v1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (_HTTP_MODEL, _HTTP_AGENT, _ENV, "ANILA_ALLOW_PRIVATE_ENDPOINT",
                "ANILA_TRUSTED_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    yield


# ── model × http × prod/dev flags ───────────────────────────────────────────

def test_model_https_always_ok(monkeypatch):
    monkeypatch.setenv(_ENV, "production")
    validate_outbound_url(_PUBLIC_HTTPS, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_model_http_dev_with_flag_ok(monkeypatch):
    """非 production + ANILA_ALLOW_HTTP_ENDPOINT=1 → 放行(dev 例外)。"""
    monkeypatch.setenv(_HTTP_MODEL, "1")
    validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_model_http_dev_without_flag_rejected(monkeypatch):
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_MODEL)


@pytest.mark.parametrize("env_val", ["production", "prod"])
def test_model_http_production_with_flag_ok(monkeypatch, env_val):
    """PLAN.md P0.2(2026-07-29 拍板):production 與 dev 同樣依
    ANILA_ALLOW_HTTP_ENDPOINT 判定 —— 內網模型 gateway 走 http。"""
    monkeypatch.setenv(_HTTP_MODEL, "1")
    monkeypatch.setenv(_ENV, env_val)
    validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_MODEL)


@pytest.mark.parametrize("env_val", ["production", "prod"])
def test_model_http_production_without_flag_rejected(monkeypatch, env_val):
    """旗標未設時 production 仍拒收 http(預設姿態不變)。"""
    monkeypatch.setenv(_ENV, env_val)
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_MODEL)


def test_model_http_production_trusted_host_without_flag_rejected(monkeypatch):
    """trusted host 只繞 host 檢查,scheme 先行 —— 旗標未設時 production
    model http 即使 host 在 trusted list 也擋(trusted 救不了 scheme)。"""
    monkeypatch.setenv(_ENV, "production")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gemma4")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http://gemma4:8000/v1", endpoint_kind=ENDPOINT_KIND_MODEL)


def test_model_http_dev_loopback_still_rejected(monkeypatch):
    """host 檢查不受 kind 影響:model+http+dev+flag 仍擋 loopback。"""
    monkeypatch.setenv(_HTTP_MODEL, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http://127.0.0.1:8000/v1", endpoint_kind=ENDPOINT_KIND_MODEL)


# ── agent × http × new flag × legacy flag warning ───────────────────────────

def test_agent_http_new_flag_ok(monkeypatch):
    monkeypatch.setenv(_HTTP_AGENT, "1")
    validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_AGENT)


def test_agent_http_new_flag_ok_even_in_production(monkeypatch):
    """agent endpoint transition 例外:production 不 gate agent http。"""
    monkeypatch.setenv(_HTTP_AGENT, "1")
    monkeypatch.setenv(_ENV, "production")
    validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_AGENT)


def test_agent_http_legacy_flag_fallback_warns(monkeypatch, caplog):
    """legacy ANILA_ALLOW_HTTP_ENDPOINT 仍放行 agent http,但記 deprecation 警告
    (不打斷內網 MLSteam 純 http NodePort agent)。"""
    monkeypatch.setenv(_HTTP_MODEL, "1")  # legacy only
    with caplog.at_level(logging.WARNING, logger="anila_core.security.url_guard"):
        validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_AGENT)
    assert any("ANILA_ALLOW_HTTP_AGENT_ENDPOINT" in r.message for r in caplog.records)


def test_agent_http_no_flags_rejected(monkeypatch):
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_AGENT)


# ── generic (default) unaffected ────────────────────────────────────────────

def test_generic_http_with_flag_ok(monkeypatch):
    monkeypatch.setenv(_HTTP_MODEL, "1")
    validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_GENERIC)


def test_generic_default_kind_matches_explicit(monkeypatch):
    """省略 endpoint_kind == 'generic';與既有呼叫端行為一致。"""
    monkeypatch.setenv(_HTTP_MODEL, "1")
    validate_outbound_url(_PUBLIC)  # no kind arg


def test_generic_http_no_flag_rejected(monkeypatch):
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(_PUBLIC)
    assert "ANILA_ALLOW_HTTP_ENDPOINT" in str(exc.value)


def test_generic_https_ok_no_flags():
    validate_outbound_url(_PUBLIC_HTTPS)


def test_agent_flag_does_not_leak_to_model(monkeypatch):
    """ANILA_ALLOW_HTTP_AGENT_ENDPOINT 只放行 agent —— model kind 不受其影響
    (dev 非 production 下,仍需 model 自己的 ANILA_ALLOW_HTTP_ENDPOINT)。"""
    monkeypatch.setenv(_HTTP_AGENT, "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(_PUBLIC, endpoint_kind=ENDPOINT_KIND_MODEL)
