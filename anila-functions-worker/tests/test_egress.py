"""Network egress assertions for the sandbox-exec / sandbox-extract
containers.

Spec §5.7 requires:

* sandbox-exec  → can reach **only** the egress proxy; raw socket
                  to any other IP fails (no route)
* sandbox-extract → cannot reach anything at all
                  (extract-net is internal:true with no proxy bridge)

Tests assert "cannot connect" without binding to a specific errno —
``connection refused`` vs ``timeout`` vs ``no route`` depends on
docker version, kernel, and target IP. Per spec round-3 round 2 #M3.
"""

from __future__ import annotations

import os
import socket

import pytest


def _is_exec_container() -> bool:
    """Distinguish exec vs extract container by env (set in compose)."""
    return os.environ.get("JOBS_DIR", "").endswith("jobs-exec")


def _try_connect(host: str, port: int, timeout: float = 2.0) -> Exception | None:
    """Returns the exception if connect failed, None on success."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return None
    except Exception as exc:
        return exc
    finally:
        sock.close()


@pytest.mark.skipif(not _is_exec_container(), reason="exec container only")
def test_exec_cannot_reach_csp_directly() -> None:
    """sandbox-exec is on functions-net only; CSP is on anila-net.
    Direct TCP connect should fail — no route across networks."""
    err = _try_connect("anila-platform-csp-1", 8000)
    assert err is not None, "expected connection failure to CSP"


@pytest.mark.skipif(not _is_exec_container(), reason="exec container only")
def test_exec_cannot_reach_public_internet_directly() -> None:
    """Even if HTTP_PROXY is unset, raw TCP to public IP must fail —
    no route out of the internal:true network."""
    err = _try_connect("8.8.8.8", 443, timeout=3.0)
    assert err is not None


@pytest.mark.skipif(not _is_exec_container(), reason="exec container only")
def test_exec_can_reach_egress_proxy() -> None:
    """The proxy is on functions-net; its 3128 port should be reachable."""
    err = _try_connect("anila-functions-egress", 3128, timeout=3.0)
    assert err is None, f"egress proxy unreachable: {err}"


@pytest.mark.skipif(_is_exec_container(), reason="extract container only")
def test_extract_cannot_reach_egress_proxy() -> None:
    """sandbox-extract is on extract-net only. Even the egress proxy
    is on a different network — no route."""
    err = _try_connect("anila-functions-egress", 3128, timeout=3.0)
    assert err is not None


@pytest.mark.skipif(_is_exec_container(), reason="extract container only")
def test_extract_cannot_reach_public_internet() -> None:
    err = _try_connect("8.8.8.8", 443, timeout=3.0)
    assert err is not None
