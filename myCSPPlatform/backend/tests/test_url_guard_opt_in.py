"""Sprint 7 X follow-up: url_guard 兩個 dev opt-in 行為合約。

- ``ANILA_ALLOW_HTTP_ENDPOINT=1`` 鬆綁 http scheme，與 host 檢查獨立。
- ``ANILA_ALLOW_PRIVATE_ENDPOINT=1`` 鬆綁 RFC 1918 私網 IP，與 scheme 獨立。
- loopback / link-local / cloud metadata / docker service name 永遠擋，
  不受任何 flag 影響。

兩個 flag 在每次 ``validate_outbound_url`` 呼叫時讀 env，所以 monkeypatch
即時生效，不需要 reimport module。
"""
from __future__ import annotations

import pytest

from anila_core.security import UnsafeEndpointError, validate_outbound_url


# ── scheme 行為 ─────────────────────────────────────────────────────────────

def test_https_always_allowed(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    validate_outbound_url("https://api.example.com/v1")  # 不該 raise


def test_http_blocked_by_default(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")  # 給 host 通過
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://api.example.com/v1")
    assert "ANILA_ALLOW_HTTP_ENDPOINT" in str(exc.value)


def test_http_allowed_when_flag_set(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    validate_outbound_url("http://api.example.com/v1")


def test_unknown_scheme_always_blocked(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("ftp://api.example.com/v1")


# ── 私網 IP 行為 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("addr", [
    "http://10.0.0.1/v1",
    "http://172.16.120.35/v1",
    "http://192.168.1.1/v1",
])
def test_rfc1918_blocked_by_default(monkeypatch, addr):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(addr)
    assert "ANILA_ALLOW_PRIVATE_ENDPOINT" in str(exc.value)


@pytest.mark.parametrize("addr", [
    "http://10.0.0.1/v1",
    "http://172.16.120.35:9000/v1",
    "http://192.168.1.1/v1",
    "https://172.16.120.35/v1",
])
def test_rfc1918_allowed_when_flag_set(monkeypatch, addr):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    validate_outbound_url(addr)


# ── 永遠擋（任一 flag 都救不了） ─────────────────────────────────────────────

@pytest.mark.parametrize("addr,why", [
    ("http://localhost/v1", "deny list"),
    ("http://127.0.0.1/v1", "loopback"),
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata link-local"),
    ("http://metadata.google.internal/", "deny list"),
    ("http://csp-db:5432/v1", "single-label docker service name"),
    ("http://kubelet.svc.cluster.local/v1", "k8s suffix"),
    ("http://printer.local/v1", "mDNS"),
    ("http://api.example.internal/v1", "internal zone"),
    ("http://224.0.0.1/v1", "multicast"),
    ("http://0.0.0.0/v1", "unspecified"),
])
def test_always_unsafe_even_with_both_flags(monkeypatch, addr, why):
    """兩個 dev opt-in 都打開仍要被擋 — 否則 SSRF guard 等於 dead code。"""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url(addr)


# ── 雜項 ────────────────────────────────────────────────────────────────────

def test_empty_url():
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("")


def test_no_hostname(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    with pytest.raises(UnsafeEndpointError):
        validate_outbound_url("http:///path")
