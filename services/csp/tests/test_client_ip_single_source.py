"""稽核紀錄的來源位址只有一個出處：``app.utils.client_ip.client_ip``。

CSP 前面是 nginx 容器。直接讀 ``request.client.host`` 拿到的是 nginx 在
Docker 網路上的位址，每一筆登入稽核都會記成同一個 172.x 位址。
"""
from __future__ import annotations

import re
from pathlib import Path

from starlette.requests import Request

from app.utils.client_ip import client_ip

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
ALLOWED = {APP_ROOT / "utils" / "client_ip.py"}
SOCKET_PEER = re.compile(r"\.client\.host\b")


def test_no_module_reads_the_socket_peer_directly():
    offenders = sorted(
        str(path.relative_to(APP_ROOT))
        for path in APP_ROOT.rglob("*.py")
        if path not in ALLOWED and SOCKET_PEER.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []


def _request(headers: dict[str, str], peer: str = "172.18.0.9") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 40000),
        }
    )


def test_proxy_observed_address_wins_over_container_peer():
    assert client_ip(_request({"X-Real-IP": "10.53.4.21"})) == "10.53.4.21"


def test_caller_supplied_forwarded_hop_is_not_trusted():
    request = _request({"X-Forwarded-For": "1.2.3.4, 10.53.4.21"})
    assert client_ip(request) == "10.53.4.21"
