"""DNS 解析要有明確上限，逾時不得當成「解不出來所以放行」。"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from anila_core.security.url_guard import (
    REASON_PRIVATE_IP,
    UnsafeEndpointError,
    validate_outbound_url,
)


def test_dns_timeout_stops_a_hung_lookup_and_does_not_allow_it(monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def _hang(host, port=None, *args, **kwargs):
        entered.set()
        release.wait(5)
        raise socket.gaierror(socket.EAI_AGAIN, "late")

    monkeypatch.setattr(socket, "getaddrinfo", _hang)
    box: dict = {}

    def _run() -> None:
        started = time.monotonic()
        try:
            validate_outbound_url(
                "https://slow.example.test/health",
                dns_timeout=0.2,
            )
            box["error"] = None
        except BaseException as exc:  # 測試要看到逾時型別，不是把例外吃掉
            box["error"] = exc
        box["elapsed"] = time.monotonic() - started

    worker = threading.Thread(target=_run)
    worker.start()
    try:
        assert entered.wait(1)
        worker.join(1.0)
        assert not worker.is_alive()
        assert box["elapsed"] < 1.0
        assert isinstance(box["error"], TimeoutError)
    finally:
        release.set()
        worker.join(2)


def test_dns_timeout_still_rejects_a_private_answer(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)

    def _private(host, port=None, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _private)
    with pytest.raises(UnsafeEndpointError) as exc:
        validate_outbound_url(
            "https://public.example.test/v1",
            dns_timeout=1.0,
        )
    assert exc.value.reason == REASON_PRIVATE_IP
