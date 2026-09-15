# -*- coding: utf-8 -*-
"""User-selectable thinking tiers (r1_0040)."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import httpx
import pytest
from sqlalchemy.orm import Session

from app.api import models as models_api
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.schemas.model_registry import ModelUpdate
from app.services import thinking_probe
from app.services.proxy.sampling import (
    ANILA_THINKING_TIER_KEY,
    apply_model_sampling_overrides,
    is_thinking_locked,
    resolve_thinking_level,
    stamp_thinking_locked,
)
from app.services.proxy.service import build_default_anila_meta as service_build_meta
from tests.conftest import login, make_model, make_user


GEMMA_ALL = ["none", "low", "medium", "high", "xhigh", "max"]
QWEN_LIKE = ["none", "low", "medium", "xhigh"]
NONE_ONLY = ["none"]


def _model(**kwargs) -> SimpleNamespace:
    defaults = dict(
        name="qwen3",
        model_type="llm",
        protocol="openai_compatible",
        thinking_effort=None,
        thinking_levels_supported=None,
        thinking_user_selectable=True,
        temperature=None,
        top_p=None,
        presence_penalty=None,
        max_tokens=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _open_router_llm(db: Session, name: str, *, primary: bool = False, **fields):
    model = make_model(db, name=name)
    model.router_enabled = True
    model.is_router_primary = primary
    for key, value in fields.items():
        setattr(model, key, value)
    db.add(RouterModelGrant(model_id=model.id, scope_type="all"))
    db.commit()
    db.refresh(model)
    return model


# ── resolve_thinking_level truth table ───────────────────────────────────────


@pytest.mark.parametrize(
    ("tier", "supported", "expected"),
    [
        (None, QWEN_LIKE, (None, False)),
        ("default", QWEN_LIKE, (None, False)),
        ("off", QWEN_LIKE, (None, False)),
        ("standard", QWEN_LIKE, ("medium", True)),
        ("standard", ["none", "low", "high"], ("low", True)),
        ("standard", ["none", "high"], ("high", True)),
        ("standard", None, (None, True)),
        ("standard", NONE_ONLY, (None, True)),
        ("deep", QWEN_LIKE, ("xhigh", True)),
        ("deep", ["none", "high", "medium"], ("high", True)),
        ("deep", None, (None, True)),
        ("deep", NONE_ONLY, (None, True)),
        ("standard", GEMMA_ALL, ("medium", True)),
        ("deep", GEMMA_ALL, ("max", True)),
        ("off", GEMMA_ALL, (None, False)),
        ("off", None, (None, False)),
    ],
)
def test_resolve_thinking_level_truth_table(tier, supported, expected):
    assert resolve_thinking_level(tier, supported, "xhigh") == expected


# ── apply_model_sampling_overrides priority ──────────────────────────────────


def test_priority_caller_reasoning_effort_wins():
    body = {"reasoning_effort": "low", ANILA_THINKING_TIER_KEY: "deep"}
    out = apply_model_sampling_overrides(
        body,
        _model(thinking_effort="max", thinking_levels_supported=QWEN_LIKE),
        thinking_tier="deep",
    )
    assert out["reasoning_effort"] == "low"
    assert ANILA_THINKING_TIER_KEY not in out


def test_priority_body_anila_thinking_tier_is_popped():
    body = {ANILA_THINKING_TIER_KEY: "deep", "messages": []}
    out = apply_model_sampling_overrides(
        body,
        _model(thinking_effort="low", thinking_levels_supported=QWEN_LIKE),
        thinking_tier="off",
    )
    assert ANILA_THINKING_TIER_KEY not in out
    assert out["reasoning_effort"] == "xhigh"
    assert out["chat_template_kwargs"] == {"enable_thinking": True}


def test_priority_conversation_tier_over_model_default():
    body = {"messages": []}
    out = apply_model_sampling_overrides(
        body,
        _model(thinking_effort="low", thinking_levels_supported=QWEN_LIKE),
        thinking_tier="standard",
    )
    assert out["reasoning_effort"] == "medium"
    assert ANILA_THINKING_TIER_KEY not in out


def test_priority_model_default_when_tier_is_default():
    body = {"messages": []}
    out = apply_model_sampling_overrides(
        body,
        _model(thinking_effort="xhigh", thinking_levels_supported=QWEN_LIKE),
        thinking_tier="default",
    )
    assert out["reasoning_effort"] == "xhigh"
    assert out["chat_template_kwargs"] == {"enable_thinking": True}


def test_anila_thinking_tier_popped_on_skip_rows():
    body = {ANILA_THINKING_TIER_KEY: "deep", "input": "hi"}
    out = apply_model_sampling_overrides(body, _model(model_type="embedding"))
    assert out == {"input": "hi"}


def test_locked_model_ignores_tiers_and_sets_thinking_locked():
    model = _model(
        thinking_effort="low",
        thinking_levels_supported=QWEN_LIKE,
        thinking_user_selectable=False,
    )
    out = apply_model_sampling_overrides(
        {ANILA_THINKING_TIER_KEY: "deep"},
        model,
        thinking_tier="deep",
    )
    assert ANILA_THINKING_TIER_KEY not in out
    assert out["reasoning_effort"] == "low"
    assert is_thinking_locked(model) is True
    meta = service_build_meta("qwen3", detail="d", thinking_locked=True)
    assert meta["thinking_locked"] is True
    existing = {}
    stamp_thinking_locked(existing, model)
    assert existing["thinking_locked"] is True


def test_off_sends_enable_thinking_false_without_reasoning_effort():
    out = apply_model_sampling_overrides(
        {},
        _model(thinking_effort="xhigh", thinking_levels_supported=QWEN_LIKE),
        thinking_tier="off",
    )
    assert out["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in out


def test_standard_with_null_supported_only_enables_thinking():
    out = apply_model_sampling_overrides(
        {},
        _model(thinking_effort="low", thinking_levels_supported=None),
        thinking_tier="standard",
    )
    assert out["chat_template_kwargs"] == {"enable_thinking": True}
    assert "reasoning_effort" not in out


# ── discover (real function, mock httpx) ─────────────────────────────────────


class _ProbeResp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _install_level_client(monkeypatch, handler) -> list:
    calls: list = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            calls.append({"url": url, "json": json, "headers": headers})
            return handler(json or {})

    monkeypatch.setattr(thinking_probe.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    return calls


def _qwen_reject_high_max(payload: dict):
    level = payload.get("reasoning_effort")
    if level in {"high", "max"}:
        return _ProbeResp(
            400,
            json.dumps(
                {"error": {"message": f"Unexpected reasoning effort {level}."}}
            ),
        )
    return _ProbeResp(200, "{}")


def _probe_target(**kwargs) -> SimpleNamespace:
    defaults = dict(
        name="qwen38-flash-next",
        model_type="llm",
        protocol="openai_compatible",
        endpoint_url="http://mock-llm:8080/v1",
        api_version="v1",
        api_key_secret_ref=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.thinking_discover
def test_discover_qwen_like_rejects_high_max(monkeypatch):
    _install_level_client(monkeypatch, _qwen_reject_high_max)
    result = asyncio.run(
        thinking_probe.discover_thinking_levels(_probe_target())
    )
    assert result.levels == QWEN_LIKE


@pytest.mark.thinking_discover
def test_discover_all_timeouts_return_none(monkeypatch):
    monkeypatch.setattr(thinking_probe, "DISCOVER_TIMEOUT_S", 0.05)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            await asyncio.sleep(1)
            return _ProbeResp(200, "{}")

    monkeypatch.setattr(thinking_probe.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    result = asyncio.run(
        thinking_probe.discover_thinking_levels(_probe_target())
    )
    assert result.levels is None


@pytest.mark.thinking_discover
def test_create_survives_discover_timeout(client, db, monkeypatch):
    monkeypatch.setattr(thinking_probe, "DISCOVER_TIMEOUT_S", 0.05)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            await asyncio.sleep(1)
            return _ProbeResp(200, "{}")

    monkeypatch.setattr(thinking_probe.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gateway.example.com")
    make_user(db, username="owner-disc-to", role="owner")
    headers = {"Authorization": f"Bearer {login(client, 'owner-disc-to')}"}
    resp = client.post(
        "/api/models",
        json={
            "name": "discover-timeout",
            "display_name": "Discover Timeout",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking_levels_supported"] is None
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "discover-timeout").one()
    assert row.thinking_levels_supported is None


@pytest.mark.thinking_discover
def test_create_stores_qwen_like_supported_set(client, db, monkeypatch):
    _install_level_client(monkeypatch, _qwen_reject_high_max)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "gateway.example.com")
    make_user(db, username="owner-disc-ok", role="owner")
    headers = {"Authorization": f"Bearer {login(client, 'owner-disc-ok')}"}
    resp = client.post(
        "/api/models",
        json={
            "name": "discover-qwen",
            "display_name": "Discover Qwen",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking_levels_supported"] == QWEN_LIKE


@pytest.mark.thinking_discover
def test_bulk_import_keeps_all_rows_when_one_times_out(db, monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    admin = make_user(db, "admin-disc-bulk", role="admin")
    source = make_model(db, name="seed-disc-bulk")
    source.endpoint_url = "http://mock-llm:8080/v1"
    source.protocol = "openai_compatible"
    db.commit()

    async def _listing(endpoint_url, api_key):
        return [
            {"id": "fast-a"},
            {"id": "slow-import"},
            {"id": "fast-b"},
        ]

    monkeypatch.setattr(models_api, "_fetch_upstream_model_listing", _listing)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            if (json or {}).get("model") == "slow-import":
                raise httpx.TimeoutException("timed out")
            return _ProbeResp(200, "{}")

    monkeypatch.setattr(thinking_probe.httpx, "AsyncClient", _Client)

    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.1"))
    result = asyncio.run(
        models_api.import_models_from_endpoint(
            models_api.ModelBulkImportRequest(source_model_id=source.id),
            req,
            admin,
            db,
        )
    )
    assert result.created == 3
    names = sorted(result.created_names)
    assert names == ["fast-a", "fast-b", "slow-import"]
    slow = db.query(ModelRegistry).filter(ModelRegistry.name == "slow-import").one()
    assert slow.thinking_levels_supported is None
    fast = db.query(ModelRegistry).filter(ModelRegistry.name == "fast-a").one()
    assert fast.thinking_levels_supported == GEMMA_ALL


# ── conversation PUT /thinking ───────────────────────────────────────────────


def test_put_thinking_owner_success_and_conversation_out(client, db):
    make_user(db, username="think-owner")
    _open_router_llm(db, "glm-think", primary=True)
    token = login(client, "think-owner")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "t", "origin": "anila-ui"},
    ).json()
    assert created["thinking_tier"] is None
    resp = client.put(
        f"/api/conversations/{created['id']}/thinking",
        headers=headers,
        json={
            "thinking_tier": "deep",
            "expected_version": created["router_selection_version"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["thinking_tier"] == "deep"
    assert body["router_selection_version"] == created["router_selection_version"] + 1


def test_put_thinking_foreign_404(client, db):
    make_user(db, username="think-owner2")
    make_user(db, username="think-intruder")
    _open_router_llm(db, "glm-think2", primary=True)
    token = login(client, "think-owner2")
    created = client.post(
        "/api/conversations",
        headers={"Authorization": f"Bearer {token}"},
        json={"title": "t", "origin": "anila-ui"},
    ).json()
    other = login(client, "think-intruder")
    denied = client.put(
        f"/api/conversations/{created['id']}/thinking",
        headers={"Authorization": f"Bearer {other}"},
        json={"thinking_tier": "off", "expected_version": created["router_selection_version"]},
    )
    assert denied.status_code == 404


def test_put_thinking_version_conflict_409_returns_current(client, db):
    make_user(db, username="think-cas")
    _open_router_llm(db, "glm-think-cas", primary=True)
    token = login(client, "think-cas")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "t", "origin": "anila-ui"},
    ).json()
    first = client.put(
        f"/api/conversations/{created['id']}/thinking",
        headers=headers,
        json={
            "thinking_tier": "standard",
            "expected_version": created["router_selection_version"],
        },
    )
    assert first.status_code == 200, first.text
    stale = client.put(
        f"/api/conversations/{created['id']}/thinking",
        headers=headers,
        json={
            "thinking_tier": "deep",
            "expected_version": created["router_selection_version"],
        },
    )
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["thinking_tier"] == "standard"
    assert detail["router_selection_version"] == first.json()["router_selection_version"]


def test_put_thinking_illegal_value_422(client, db):
    make_user(db, username="think-bad")
    _open_router_llm(db, "glm-think-bad", primary=True)
    token = login(client, "think-bad")
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": "t", "origin": "anila-ui"},
    ).json()
    resp = client.put(
        f"/api/conversations/{created['id']}/thinking",
        headers=headers,
        json={"thinking_tier": "turbo", "expected_version": 1},
    )
    assert resp.status_code == 422


def test_put_thinking_cookie_requires_csrf(client, db):
    from app.middleware.cookies import CSRF_COOKIE_NAME

    make_user(db, username="think-cookie")
    _open_router_llm(db, "glm-think-cookie", primary=True)
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "think-cookie", "password": "password"},
    )
    assert login_resp.status_code == 200, login_resp.text
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    created = client.post(
        "/api/conversations",
        headers={"X-CSRF-Token": csrf},
        json={"title": "t", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    missing = client.put(
        f"/api/conversations/{created.json()['id']}/thinking",
        json={"thinking_tier": "off", "expected_version": created.json()["router_selection_version"]},
    )
    assert missing.status_code == 403
    ok = client.put(
        f"/api/conversations/{created.json()['id']}/thinking",
        headers={"X-CSRF-Token": csrf},
        json={"thinking_tier": "off", "expected_version": created.json()["router_selection_version"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["thinking_tier"] == "off"


# ── router-models + model update lock ────────────────────────────────────────


def test_router_models_expose_thinking_fields(client, db):
    make_user(db, username="think-picker")
    _open_router_llm(
        db,
        "glm-picker",
        primary=True,
        thinking_effort="max",
        thinking_levels_supported=QWEN_LIKE,
        thinking_user_selectable=False,
    )
    token = login(client, "think-picker")
    resp = client.get(
        "/api/router-models",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    row = resp.json()["models"][0]
    assert row["thinking_effort"] == "max"
    assert row["thinking_levels_supported"] == QWEN_LIKE
    assert row["thinking_user_selectable"] is False


def test_model_update_accepts_thinking_user_selectable(client, db):
    model = make_model(db, name="lock-me")
    make_user(db, username="admin-lock", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'admin-lock')}"}
    resp = client.put(
        f"/api/models/{model.id}",
        json={"thinking_user_selectable": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking_user_selectable"] is False
    payload = ModelUpdate(thinking_user_selectable=False)
    assert payload.thinking_user_selectable is False
