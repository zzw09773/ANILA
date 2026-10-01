"""host_not_trusted is an operator hint; the private switch names both gates."""

from __future__ import annotations

import pytest

from anila_core.security.url_guard import (
    FIXABLE_BY_TRUST_HOST,
    REASON_HOST_NOT_TRUSTED,
    REASON_PRIVATE_IP,
    UnsafeEndpointError,
    validate_outbound_url,
)

_SWITCH_OFF = "這台平台未允許私有 IP 端點（ANILA_ALLOW_PRIVATE_ENDPOINT）"
_ALSO_TRUST = "開啟後還需要把這台主機加入信任主機"


def test_host_not_trusted_is_fixable_by_trust_host(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("http://10.1.2.3/v1")
    assert exc.value.reason == REASON_HOST_NOT_TRUSTED
    assert exc.value.host == "10.1.2.3"
    assert exc.value.fixable_by_trust_host is True
    assert REASON_HOST_NOT_TRUSTED in FIXABLE_BY_TRUST_HOST


def test_private_switch_off_also_names_the_trust_list_when_host_is_missing(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("https://172.16.120.35/v1")
    assert exc.value.reason == REASON_PRIVATE_IP
    assert exc.value.fixable_by_trust_host is False
    assert str(exc.value) == f"{_SWITCH_OFF}。{_ALSO_TRUST}"


def test_private_switch_off_stays_quiet_when_the_host_is_already_trusted(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "10.9.8.7")
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url("https://10.9.8.7/v1")
    assert exc.value.reason == REASON_PRIVATE_IP
    assert str(exc.value) == _SWITCH_OFF
    assert _ALSO_TRUST not in str(exc.value)
