"""False-green-light invariants (2026-07-30).

A check that passes must mean the thing it names actually works.

Acceptance (each with a revert-mutant that goes red):
1. Upstream 401 on the chat path → path NOT verified; host confirmed.
2. Versioned chat path genuinely works (e.g. 400 empty-messages) → success.
3. Only `/` answers health probe → NOT ``healthy`` (``degraded``).
4. Reported outcome carries no endpoint address for a caller who may not see one.
5. Connection test remains a diagnostic — never a register/approve gate (OE-1).
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")
os.environ.setdefault("ANILA_ALLOW_HTTP_ENDPOINT", "1")
os.environ.setdefault("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "1")
os.environ.setdefault("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
os.environ.setdefault(
    "ANILA_TRUSTED_HOSTS", "agent-box,mock-llm,secret-host.internal,agent"
)

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.agents import health as agent_health
from app.services import health_checker
from app.services.endpoint_author_service import ENDPOINT_REDACTED
from app.services.health_checker import (
    HEALTH_DEGRADED,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    probe_model_health_detailed,
)
from tests.conftest import login, make_agent, make_model, make_user


# ── helpers ──────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


def _client_returning(status_by_suffix: dict[str, int] | int):
    """httpx.AsyncClient stand-in. ``status_by_suffix`` maps URL suffix → code,
    or a single int for every request.
    """

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if isinstance(status_by_suffix, int):
                return _Resp(status_by_suffix)
            for suffix, code in status_by_suffix.items():
                if url.endswith(suffix):
                    return _Resp(code)
            raise httpx.ConnectError(f"no handler for {url}")

        async def post(self, url, json=None, headers=None):
            if isinstance(status_by_suffix, int):
                return _Resp(status_by_suffix)
            for suffix, code in status_by_suffix.items():
                if url.endswith(suffix):
                    return _Resp(code)
            raise httpx.ConnectError(f"no handler for {url}")

    return _Client


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── 1. 401 on chat path → path not verified ──────────────────────────────────


def test_connection_401_does_not_verify_path(client: TestClient, db: Session, monkeypatch):
    """Acceptance 1: 401 everywhere → host yes, path/creds unknown, not green."""
    owner = make_user(db, username="gl-owner-401", role="developer")
    agent = make_agent(
        db, owner=owner, name="gl-agent-401", approval_status="registered"
    )
    agent.endpoint_url = "http://agent-box:9100"
    db.commit()

    monkeypatch.setattr(agent_health.httpx, "AsyncClient", _client_returning(401))

    token = login(client, "gl-owner-401")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["host_reachable"] is True
    assert body["reachable"] is True
    assert body["path_verified"] is None
    assert body["credentials_accepted"] is None
    assert body["token_accepted"] is None
    assert body["status_code"] == 401
    assert "未驗證路徑" in body["detail"]
    assert "未驗證憑證" in body["detail"]
    # Must not look like the old false green (token_accepted from !=401).
    assert body["token_accepted"] is not True


def test_mutant_connection_401_old_neq401_would_go_red():
    """Mutant for A1: restoring ``accepted = status != 401`` mis-labels 404 as pass."""
    host, creds, path, detail = agent_health._classify_connection_status(404)
    assert host is True
    assert path is False
    assert creds is None
    # Old rule (mutant): accepted = (404 != 401) → True. Prove new rule differs.
    old_accepted = 404 != 401
    assert old_accepted is True
    assert creds is not True
    assert "路徑未通過驗證" in detail


# ── 2. versioned path works → success ────────────────────────────────────────


def test_connection_versioned_path_works(client: TestClient, db: Session, monkeypatch):
    """Acceptance 2: agent empty-messages 400 → path + creds verified."""
    owner = make_user(db, username="gl-owner-ok", role="developer")
    agent = make_agent(
        db, owner=owner, name="gl-agent-ok", approval_status="registered"
    )
    agent.endpoint_url = "http://agent-box:9100"
    db.commit()

    monkeypatch.setattr(agent_health.httpx, "AsyncClient", _client_returning(400))

    token = login(client, "gl-owner-ok")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["host_reachable"] is True
    assert body["path_verified"] is True
    assert body["credentials_accepted"] is True
    assert body["token_accepted"] is True
    assert body["status_code"] == 400
    assert "路徑與憑證皆通過" in body["detail"]


def test_connection_probe_fires_without_issued_csk(
    client: TestClient, db: Session, monkeypatch
):
    """P2.1: probe always mints a dispatch JWT — no 409 for missing csk-.

    Pre-P2.1 returned 409 when no active credential existed. Governance UI
    only surfaces ``detail`` on any non-2xx; nothing depends on 409 here.
    """
    from app.models.agent_credential import AgentCredential

    owner = make_user(db, username="gl-owner-nocred", role="developer")
    agent = make_agent(
        db, owner=owner, name="gl-agent-nocred", approval_status="registered"
    )
    agent.endpoint_url = "http://agent-box:9100"
    db.commit()
    assert (
        db.query(AgentCredential)
        .filter(AgentCredential.agent_id == agent.id)
        .count()
        == 0
    )

    seen = {"posted": False}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            seen["posted"] = True
            assert headers is not None
            assert headers.get("Authorization", "").startswith("Bearer ")
            assert "X-CSP-Service-Token" not in (headers or {})
            return _Resp(400)

    monkeypatch.setattr(agent_health.httpx, "AsyncClient", _Client)

    token = login(client, "gl-owner-nocred")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    assert resp.status_code != 409, resp.text
    assert resp.status_code == 200, resp.text
    assert seen["posted"] is True
    body = resp.json()
    assert body["host_reachable"] is True
    assert body["credentials_accepted"] is True
    assert body["path_verified"] is True


def test_mutant_connection_success_requires_classify():
    """Mutant for A2: if classifier treated only 200 as success, 400 goes red."""
    _host, creds, path, _ = agent_health._classify_connection_status(400)
    assert creds is True and path is True
    # Prove we did NOT require 200 — empty-messages 400 is the real agent signal.
    assert 400 != 200


# ── 3. `/`-only health → degraded, not healthy ───────────────────────────────


def test_health_root_only_is_degraded_not_healthy(monkeypatch):
    """Acceptance 3: only `/` answers → degraded (not the same as a real hit)."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if url.endswith("/health") or url.endswith("/v1/models"):
                raise httpx.ConnectError("skip")
            if url.endswith("/") or url.rstrip("/").endswith(":8080"):
                return _Resp(200)
            raise httpx.ConnectError("skip")

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080")
    )
    assert status == HEALTH_DEGRADED
    assert status != HEALTH_HEALTHY


def test_health_real_path_still_healthy(monkeypatch):
    """Genuine 2xx on a real probe path still reports healthy."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(
        health_checker.httpx,
        "AsyncClient",
        _client_returning({"/health": 200, "/v1/models": 401}),
    )
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080/v1")
    )
    assert status == HEALTH_HEALTHY


def test_health_401_on_every_real_path_is_not_healthy(monkeypatch):
    """Acceptance: unauthenticated 401 on every real probe ≠ healthy.

    A 401 only proves something is listening. It does not prove the
    platform key still works — that was the false-green shape.
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(
        health_checker.httpx,
        "AsyncClient",
        _client_returning(401),
    )
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080/v1")
    )
    assert status != HEALTH_HEALTHY
    assert status == HEALTH_UNKNOWN


def test_health_200_on_real_path_is_healthy(monkeypatch):
    """Acceptance: a genuine 200 on /health still reports healthy."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    monkeypatch.setattr(
        health_checker.httpx,
        "AsyncClient",
        _client_returning({"/health": 200}),
    )
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080")
    )
    assert status == HEALTH_HEALTHY


def test_mutant_health_401_old_lt500_rule_goes_red():
    """Mutant: restoring ``!=404 and <500`` would mis-label 401 as a hit."""
    assert health_checker._real_probe_hit(401) is False
    assert health_checker._real_probe_hit(403) is False
    assert health_checker._real_probe_hit(200) is True
    old_rule = lambda code: code != 404 and code < 500
    assert old_rule(401) is True
    assert old_rule(401) != health_checker._real_probe_hit(401)


def test_mutant_health_root_as_healthy_goes_red(monkeypatch):
    """Mutant for A3: if `/` were still accepted as healthy, this assert fails."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if url.endswith("/health") or url.endswith("/v1/models"):
                raise httpx.ConnectError("skip")
            return _Resp(200)  # `/` only

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080")
    )
    old_rule_would_be_healthy = True  # old: any path <500 including `/`
    assert status == HEALTH_DEGRADED
    assert status != HEALTH_HEALTHY
    assert old_rule_would_be_healthy and status != HEALTH_HEALTHY


# ── 4. no endpoint address for callers who may not see one ───────────────────


def test_connection_detail_redacts_address_for_non_author(
    client: TestClient, db: Session, monkeypatch
):
    """Acceptance 4: connect-error detail has no host for non-author caller."""
    # Developer owns the agent (may test-connection) but has NO endpoint-author
    # grant — can_see_endpoint_address is False unless owner/grant.
    owner = make_user(db, username="gl-dev-vis", role="developer")
    secret = "http://secret-host.internal:9100/v1"
    agent = make_agent(
        db, owner=owner, name="gl-agent-vis", approval_status="registered"
    )
    agent.endpoint_url = secret
    db.commit()

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError(
                f"[Errno 111] Connection refused: {url}"
            )

    monkeypatch.setattr(agent_health.httpx, "AsyncClient", _BoomClient)

    token = login(client, "gl-dev-vis")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["host_reachable"] is False
    blob = str(body)
    assert "secret-host.internal" not in blob
    assert secret not in blob
    assert "9100" not in body["detail"]
    assert ENDPOINT_REDACTED not in body["detail"]  # we omit, not sentinel-swap
    assert "無法連線" in body["detail"]


def test_connection_detail_may_include_exc_for_author(
    client: TestClient, db: Session, monkeypatch
):
    """Owner (platform) may see the address-shaped exception text."""
    owner = make_user(db, username="gl-platform-owner", role="owner")
    secret = "http://secret-host.internal:9100"
    agent = make_agent(
        db, owner=owner, name="gl-agent-own", approval_status="registered"
    )
    agent.endpoint_url = secret
    db.commit()

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError(
                f"[Errno 111] Connection refused: {url}"
            )

    monkeypatch.setattr(agent_health.httpx, "AsyncClient", _BoomClient)
    token = login(client, "gl-platform-owner")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "secret-host.internal" in body["detail"]


def test_mutant_connection_always_embed_exc_goes_red(
    client: TestClient, db: Session, monkeypatch
):
    """Mutant for A4: if detail always embedded ``str(exc)``, non-author leaks."""
    owner = make_user(db, username="gl-dev-mut4", role="developer")
    agent = make_agent(
        db, owner=owner, name="gl-agent-mut4", approval_status="registered"
    )
    agent.endpoint_url = "http://secret-host.internal:9100"
    db.commit()

    leaked = "http://secret-host.internal:9100/v1/chat/completions"
    mutant_detail = f"無法連線到 agent 端點: ConnectError({leaked})"
    assert "secret-host.internal" in mutant_detail

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError(leaked)

    monkeypatch.setattr(agent_health.httpx, "AsyncClient", _BoomClient)
    token = login(client, "gl-dev-mut4")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    body = resp.json()
    assert "secret-host.internal" not in body["detail"]
    assert body["detail"] != mutant_detail


# ── 5. still not a gate (OE-1) ───────────────────────────────────────────────


def test_register_does_not_require_connection_test(
    client: TestClient, db: Session, monkeypatch
):
    """Acceptance 5 / OE-1: registering an agent needs no passing connection test."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent-box")

    make_user(db, username="gl-dev-reg", role="developer")
    model = make_model(db, name="gl-base-llm")
    token = login(client, "gl-dev-reg")
    resp = client.post(
        "/api/agents/register",
        headers=_bearer(token),
        json={
            "name": "gl-no-gate-agent",
            "endpoint_url": "http://agent-box:9100",
            "description_for_router": "gate-check agent for false-green invariant",
            "base_model_id": model.id,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approval_status"] == "registered"
    assert "connection_test_passed" not in body
    assert body["approval_status"] != "pending_connection_test"


# ── classifier unit table ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expect_creds,expect_path",
    [
        (200, True, True),
        (400, True, True),
        (422, True, True),
        (401, None, None),
        (403, None, None),
        (404, None, False),
        (405, None, True),
        (429, None, True),
        (500, None, True),
        (502, None, True),
    ],
)
def test_classify_connection_status_table(code, expect_creds, expect_path):
    host, creds, path, detail = agent_health._classify_connection_status(code)
    assert host is True
    assert creds is expect_creds
    assert path is expect_path
    assert detail  # always explain what was / was not verified
    assert "http://" not in detail
    assert "https://" not in detail


# ── review follow-ups ────────────────────────────────────────────────────────


def test_health_v1_base_health_404_does_not_short_circuit_healthy(monkeypatch):
    """``…/v1`` + ``/health`` must hit ``/health`` (not ``/v1/health``).

    A 404 on the wrong join used to short-circuit to healthy before
    ``/v1/models`` was tried. With strip + no-404-as-hit, only a real
    surface response counts.
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    seen: list[str] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            seen.append(url)
            if url.endswith("/v1/health"):
                return _Resp(404)  # wrong join — must NOT be probed
            if url.endswith("/health"):
                return _Resp(404)  # real /health missing
            if url.endswith("/v1/models"):
                raise httpx.ConnectError("models down")
            if url.rstrip("/").endswith(":8080") or url.endswith("8080/"):
                return _Resp(200)
            raise httpx.ConnectError("skip")

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080/v1")
    )
    assert "http://mock-llm:8080/health" in seen
    assert "http://mock-llm:8080/v1/health" not in seen
    assert status != HEALTH_HEALTHY
    assert status == HEALTH_DEGRADED  # `/` still answers → weak signal


def test_health_real_path_404_is_not_healthy(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if url.endswith("/health") or url.endswith("/v1/models"):
                return _Resp(404)
            return _Resp(200)  # `/` only

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    status, _ = asyncio.run(
        probe_model_health_detailed("http://mock-llm:8080")
    )
    assert status == HEALTH_DEGRADED


def test_ssrf_reject_detail_redacts_host_for_non_author(
    client: TestClient, db: Session, monkeypatch
):
    """SSRF reject must not embed hostname unless can_see_endpoint_address."""
    from anila_core.security import UnsafeEndpointError

    owner = make_user(db, username="gl-dev-ssrf", role="developer")
    agent = make_agent(
        db, owner=owner, name="gl-agent-ssrf", approval_status="registered"
    )
    agent.endpoint_url = "http://secret-host.internal:9100"
    db.commit()

    def _boom(url, endpoint_kind=None):
        raise UnsafeEndpointError(
            "endpoint_url host 'secret-host.internal' resolved to private "
            "address '10.53.100.12'",
            host="secret-host.internal",
            reason="private_ip",
        )

    monkeypatch.setattr(agent_health, "validate_outbound_url", _boom)
    token = login(client, "gl-dev-ssrf")
    resp = client.post(
        f"/api/agents/{agent.id}/test-connection",
        headers=_bearer(token),
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "secret-host.internal" not in detail
    assert "10.53.100.12" not in detail
    assert detail == "端點未通過出向安全驗證"


def test_probe_url_strips_v1_for_health():
    assert (
        health_checker._probe_url("http://agent-box:9100/v1", "/health")
        == "http://agent-box:9100/health"
    )
    assert (
        health_checker._probe_url("http://agent-box:9100/v1", "/v1/models")
        == "http://agent-box:9100/v1/models"
    )
    assert (
        health_checker._probe_url("http://agent-box:9100/v1", "/")
        == "http://agent-box:9100/"
    )