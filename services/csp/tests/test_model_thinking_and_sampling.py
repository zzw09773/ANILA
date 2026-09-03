# -*- coding: utf-8 -*-
"""Per-model thinking_effort + sampling overrides (r1_0038).

Schema rejects bad enums / out-of-range numbers with Traditional Chinese
errors. Proxy merge: caller wins; ``none`` disables thinking kwargs;
NULL leaves the body alone.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from pydantic import ValidationError

from app.api.models import _build_response
from app.models.model_registry import ModelRegistry
from app.models.service_client import ServiceClient
from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.services.proxy.sampling import apply_model_sampling_overrides
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from tests.conftest import login, make_model, make_user


def _create(**kwargs) -> ModelCreate:
    return ModelCreate(
        name="m1",
        display_name="M1",
        model_type="llm",
        endpoint_url="https://api.example.com/v1",
        **kwargs,
    )


def _model(**kwargs) -> SimpleNamespace:
    defaults = dict(
        name="qwen3",
        model_type="llm",
        protocol="openai_compatible",
        thinking_effort=None,
        temperature=None,
        top_p=None,
        presence_penalty=None,
        max_tokens=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ── schema ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("schema_cls", [ModelCreate, ModelUpdate])
@pytest.mark.parametrize("level", ["none", "low", "medium", "high", "xhigh", "max", "off", "default"])
def test_thinking_effort_accepts_known_levels(schema_cls, level):
    payload = (
        _create(thinking_effort=level)
        if schema_cls is ModelCreate
        else ModelUpdate(thinking_effort=level)
    )
    expected = "none" if level in {"off", "default"} else level
    assert payload.thinking_effort == expected


@pytest.mark.parametrize("schema_cls", [ModelCreate, ModelUpdate])
def test_thinking_effort_empty_and_none_are_null(schema_cls):
    def make(**kwargs):
        return _create(**kwargs) if schema_cls is ModelCreate else schema_cls(**kwargs)

    assert make(thinking_effort=None).thinking_effort is None
    assert make(thinking_effort="").thinking_effort is None
    assert make(thinking_effort="  ").thinking_effort is None


@pytest.mark.parametrize("schema_cls", [ModelCreate, ModelUpdate])
def test_thinking_effort_rejects_unknown(schema_cls):
    with pytest.raises(ValidationError) as exc:
        if schema_cls is ModelCreate:
            _create(thinking_effort="turbo")
        else:
            ModelUpdate(thinking_effort="turbo")
    assert "thinking_effort" in str(exc.value)


@pytest.mark.parametrize("schema_cls", [ModelCreate, ModelUpdate])
def test_sampling_ranges_and_chinese_errors(schema_cls):
    def make(**kwargs):
        return _create(**kwargs) if schema_cls is ModelCreate else schema_cls(**kwargs)

    ok = make(temperature=0.6, top_p=0.95, presence_penalty=1.5, max_tokens=2048)
    assert ok.temperature == 0.6
    assert ok.top_p == 0.95
    assert ok.presence_penalty == 1.5
    assert ok.max_tokens == 2048

    with pytest.raises(ValidationError) as exc:
        make(temperature=2.5)
    assert "temperature" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        make(top_p=1.1)
    assert "top_p" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        make(presence_penalty=-2.1)
    assert "presence_penalty" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        make(max_tokens=0)
    assert "max_tokens" in str(exc.value)


def test_model_create_defaults_overrides_to_none():
    payload = _create()
    assert payload.thinking_effort is None
    assert payload.temperature is None
    assert payload.top_p is None
    assert payload.presence_penalty is None
    assert payload.max_tokens is None


def test_model_update_omits_unset_overrides():
    data = ModelUpdate(display_name="only").model_dump(exclude_unset=True)
    assert "thinking_effort" not in data
    assert "temperature" not in data


# ── proxy merge ──────────────────────────────────────────────────────────────


def test_null_thinking_and_sampling_leave_body_alone():
    body = {"model": "qwen3", "messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(body, _model())
    assert out == body
    assert "chat_template_kwargs" not in out
    assert "temperature" not in out


def test_default_thinking_does_not_add_knobs():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(
        body, _model(thinking_effort="default", temperature=0.6)
    )
    assert out["temperature"] == 0.6
    assert "chat_template_kwargs" not in out


def test_none_disables_thinking_kwargs():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(body, _model(thinking_effort="none"))
    assert out["chat_template_kwargs"] == {"enable_thinking": False}
    assert "enable_thinking" not in out
    assert "reasoning_effort" not in out


def test_none_merges_into_existing_chat_template_kwargs():
    body = {"chat_template_kwargs": {"foo": 1}}
    out = apply_model_sampling_overrides(body, _model(thinking_effort="none"))
    assert out["chat_template_kwargs"] == {"foo": 1, "enable_thinking": False}


def test_caller_enable_thinking_wins_inside_kwargs():
    body = {"chat_template_kwargs": {"enable_thinking": True}}
    out = apply_model_sampling_overrides(body, _model(thinking_effort="none"))
    assert out["chat_template_kwargs"] == {"enable_thinking": True}


def test_level_enables_thinking_without_budget():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(body, _model(thinking_effort="high"))
    assert out["chat_template_kwargs"] == {"enable_thinking": True}
    assert "reasoning_effort" not in out


def test_o_style_maps_reasoning_effort_and_clamps_xhigh():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(
        body, _model(name="openai/o3-mini", thinking_effort="xhigh")
    )
    assert out["chat_template_kwargs"] == {"enable_thinking": True}
    assert out["reasoning_effort"] == "high"


def test_caller_reasoning_effort_wins():
    body = {"reasoning_effort": "low"}
    out = apply_model_sampling_overrides(
        body, _model(name="o3", thinking_effort="high")
    )
    assert out["reasoning_effort"] == "low"


def test_sampling_caller_wins():
    body = {"temperature": 0.1, "max_tokens": 16}
    model = _model(temperature=0.6, top_p=0.95, presence_penalty=1.5, max_tokens=2048)
    out = apply_model_sampling_overrides(body, model)
    assert out["temperature"] == 0.1
    assert out["max_tokens"] == 16
    assert out["top_p"] == 0.95
    assert out["presence_penalty"] == 1.5


def test_does_not_mutate_inbound_body():
    body = {"temperature": 0.2}
    apply_model_sampling_overrides(body, _model(temperature=0.9, thinking_effort="none"))
    assert body == {"temperature": 0.2}


def test_skips_embedding_and_agent_rows():
    body = {"input": "hi"}
    out = apply_model_sampling_overrides(
        body, _model(model_type="embedding", temperature=0.6, thinking_effort="none")
    )
    assert out == body


# ── response / HTTP ──────────────────────────────────────────────────────────


def test_build_response_includes_override_fields():
    row = SimpleNamespace(
        id=1,
        name="qwen3",
        display_name="Qwen",
        model_type="llm",
        endpoint_url="http://qwen:8000/v1",
        api_version="v1",
        is_active=True,
        is_router_primary=False,
        is_image_primary=False,
        is_asr_primary=False,
        is_slides_primary=False,
        is_platform_embedding=False,
        embedding_native_dim=None,
        health_status="unknown",
        health_checked_at=None,
        description=None,
        context_window=None,
        base_model_id=None,
        base_model=None,
        is_internal=False,
        protocol="openai_compatible",
        classification_ceiling=None,
        owner_department_id=None,
        supports_streaming=True,
        supports_json_schema=False,
        supports_tools=False,
        api_key_secret_ref=None,
        created_at=None,
        updated_at=None,
        thinking_effort="none",
        temperature=0.6,
        top_p=0.95,
        presence_penalty=1.5,
        max_tokens=2048,
    )
    data = _build_response(row, caller=SimpleNamespace(role="admin"))
    assert data["thinking_effort"] == "none"
    assert data["temperature"] == 0.6
    assert data["top_p"] == 0.95
    assert data["presence_penalty"] == 1.5
    assert data["max_tokens"] == 2048


def test_put_rejects_bad_thinking_effort(client, db):
    model = make_model(db, name="think-put")
    make_user(db, username="admin-think", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'admin-think')}"}
    resp = client.put(
        f"/api/models/{model.id}",
        json={"thinking_effort": "turbo"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "thinking_effort" in resp.text


def test_put_and_list_round_trip_overrides(client, db):
    model = make_model(db, name="think-rt")
    make_user(db, username="admin-think-rt", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'admin-think-rt')}"}
    resp = client.put(
        f"/api/models/{model.id}",
        json={
            "thinking_effort": "off",
            "temperature": 0.6,
            "top_p": 0.95,
            "presence_penalty": 1.5,
            "max_tokens": 2048,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["thinking_effort"] == "none"
    assert body["temperature"] == 0.6
    assert body["top_p"] == 0.95
    assert body["presence_penalty"] == 1.5
    assert body["max_tokens"] == 2048
    listed = client.get("/api/models", headers=headers).json()
    row = next(r for r in listed if r["id"] == model.id)
    assert row["thinking_effort"] == "none"


def test_router_primary_includes_override_fields(client, db):
    model = make_model(db, name="rp-think")
    model.is_router_primary = True
    model.thinking_effort = "low"
    model.temperature = 0.6
    model.top_p = 0.95
    model.presence_penalty = 1.5
    model.max_tokens = 4096
    db.commit()

    token = generate_service_token()
    db.add(
        ServiceClient(
            client_name="rp-think-router",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
        )
    )
    db.commit()
    resp = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "rp-think"
    assert body["thinking_effort"] == "low"
    assert body["temperature"] == 0.6
    assert body["top_p"] == 0.95
    assert body["presence_penalty"] == 1.5
    assert body["max_tokens"] == 4096
