# -*- coding: utf-8 -*-
"""Per-model thinking_effort + sampling overrides (r1_0038 / r1_0039).

Schema rejects bad enums / out-of-range numbers with Traditional Chinese
errors. Proxy merge: caller wins; ``none`` disables thinking kwargs;
NULL leaves the body alone.

r1_0039 (2026-09-03, measured live): a level now sends the verbatim
``reasoning_effort`` string alongside ``enable_thinking=true`` on every
model, because the Qwen/litellm endpoint reads only the former and gemma
only the latter, and the model name cannot tell them apart. Which levels a
given endpoint accepts is settled by the save-time probe, not guessed —
Qwen answers 400 for ``high`` / ``max`` and takes only low / medium / xhigh.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import httpx
import pytest
from pydantic import ValidationError

from app.api import models as models_api
from app.api.models import _build_response
from app.models.model_registry import ModelRegistry
from app.models.service_client import ServiceClient
from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.services import thinking_probe
from app.services.proxy.sampling import (
    SAMPLING_DEFAULTS_MARKER,
    apply_model_sampling_overrides,
)
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


def test_level_sends_both_vendor_knobs():
    """Qwen reads only reasoning_effort, gemma reads only enable_thinking, and
    no name pattern tells them apart — so a level sends both."""
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(body, _model(thinking_effort="high"))
    assert out["chat_template_kwargs"] == {"enable_thinking": True}
    assert out["reasoning_effort"] == "high"


def test_qwen_row_gets_the_level_the_console_selected():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = apply_model_sampling_overrides(
        body, _model(name="qwen38-flash-next", thinking_effort="medium")
    )
    assert out["reasoning_effort"] == "medium"
    assert out["chat_template_kwargs"] == {"enable_thinking": True}


@pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
def test_level_goes_out_verbatim_and_is_never_clamped(level):
    """``xhigh`` is exactly what the Qwen endpoint wants and exactly what the
    old clamp-to-``high`` table destroyed — ``high`` is a 400 there."""
    out = apply_model_sampling_overrides({}, _model(thinking_effort=level))
    assert out["reasoning_effort"] == level


def test_o_style_name_is_no_longer_special_cased():
    out = apply_model_sampling_overrides(
        {}, _model(name="openai/o3-mini", thinking_effort="xhigh")
    )
    assert out["reasoning_effort"] == "xhigh"
    assert out["chat_template_kwargs"] == {"enable_thinking": True}


def test_none_sends_no_reasoning_effort_even_on_a_qwen_row():
    """gemma ignores ``reasoning_effort=none`` outright and Qwen is already
    silenced by ``enable_thinking=false``, so sending it buys nothing."""
    out = apply_model_sampling_overrides(
        {"messages": []}, _model(name="qwen38-flash-next", thinking_effort="none")
    )
    assert out["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in out


def test_caller_reasoning_effort_wins():
    body = {"reasoning_effort": "low"}
    out = apply_model_sampling_overrides(
        body, _model(name="qwen38-flash-next", thinking_effort="xhigh")
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


# ── router sampling-defaults marker ──────────────────────────────────────────
# The Router cannot omit temperature / max_tokens (its ``router`` sampling row
# rides on every upstream call), so it flags them as its own defaults and the
# model_registry knobs win over just those keys.


def test_marked_router_defaults_lose_to_model_registry():
    body = {
        SAMPLING_DEFAULTS_MARKER: ["temperature", "max_tokens"],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    out = apply_model_sampling_overrides(
        body, _model(temperature=0.6, max_tokens=8192, presence_penalty=1.5)
    )
    assert out["temperature"] == 0.6
    assert out["max_tokens"] == 8192
    assert out["presence_penalty"] == 1.5
    assert SAMPLING_DEFAULTS_MARKER not in out


def test_unmarked_key_still_wins_over_model_registry():
    body = {
        SAMPLING_DEFAULTS_MARKER: ["max_tokens"],
        "temperature": 0.3,
        "max_tokens": 4096,
    }
    out = apply_model_sampling_overrides(body, _model(temperature=0.6, max_tokens=8192))
    assert out["temperature"] == 0.3
    assert out["max_tokens"] == 8192
    assert SAMPLING_DEFAULTS_MARKER not in out


@pytest.mark.parametrize("marker", ["temperature", 7, {"temperature": True}, None])
def test_garbage_marker_is_ignored_but_still_popped(marker):
    body = {SAMPLING_DEFAULTS_MARKER: marker, "temperature": 0.3}
    out = apply_model_sampling_overrides(body, _model(temperature=0.6, max_tokens=8192))
    assert out["temperature"] == 0.3
    assert out["max_tokens"] == 8192
    assert SAMPLING_DEFAULTS_MARKER not in out


def test_marker_is_popped_on_the_early_return_rows():
    """Embedding / agent / triton rows bail out before the merge; the marker is
    a CSP-internal hint and must never reach the upstream body anyway."""
    body = {SAMPLING_DEFAULTS_MARKER: ["temperature", "max_tokens"], "input": "hi"}
    out = apply_model_sampling_overrides(body, _model(model_type="embedding"))
    assert out == {"input": "hi"}
    out = apply_model_sampling_overrides(body, _model(protocol="triton_grpc"))
    assert out == {"input": "hi"}


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
    assert "context_window" in body


# ── save-time probe: does the endpoint accept this level? ────────────────────
# 2026-09-03 live: Qwen behind litellm answers 400 for high / max / minimal and
# names the levels it does take. gemma swallows everything. The name tells you
# nothing, so ask once at save time.

_INNER_VLLM_ERROR = (
    '{"error":{"message":"Unexpected reasoning effort high. '
    'Supported types are xhigh (default), medium, and low.",'
    '"type":"BadRequestError","param":null,"code":400}}'
)
# The wire body verbatim: litellm nests the vLLM error as an escaped JSON
# string and appends its own routing dump, internal host and all.
LITELLM_400_BODY = json.dumps(
    {
        "error": {
            "message": (
                f"litellm.BadRequestError: Hosted_vllmException - "
                f"{_INNER_VLLM_ERROR}. "
                "Received Model Group=qwen38-flash-next\n"
                "Available Model Group Fallbacks=None LiteLLM Retried: 1 times "
                "(endpoint http://172.16.120.38:4000/v1)"
            ),
            "type": "invalid_request_error",
            "code": "400",
        }
    }
)
UPSTREAM_SENTENCE = (
    "Unexpected reasoning effort high. "
    "Supported types are xhigh (default), medium, and low."
)


class _ProbeResp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _install_probe_client(monkeypatch, *, status=200, body="", raise_exc=None) -> list:
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
            if raise_exc is not None:
                raise raise_exc
            return _ProbeResp(status, body)

    monkeypatch.setattr(thinking_probe.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    return calls


def _probe_target(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id=1,
        name="qwen38-flash-next",
        endpoint_url="http://mock-llm:8080/v1",
        api_version="v1",
        api_key_secret_ref=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_probe_sends_the_level_to_the_real_chat_path(monkeypatch):
    calls = _install_probe_client(monkeypatch, status=200, body="{}")
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "medium")
    )
    assert result.status == "ok"
    sent = calls[0]
    assert sent["url"] == "http://mock-llm:8080/v1/chat/completions"
    assert sent["json"]["reasoning_effort"] == "medium"
    assert sent["json"]["max_tokens"] == 1
    assert sent["json"]["model"] == "qwen38-flash-next"


def test_probe_presents_the_models_gateway_key(monkeypatch):
    calls = _install_probe_client(monkeypatch, status=200, body="{}")
    target = _probe_target(
        api_key_secret_ref=encode_service_token_envelope("sk-probe-key")
    )
    asyncio.run(thinking_probe.probe_thinking_effort(target, "low"))
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-probe-key"


def test_probe_reports_rejected_and_returns_only_the_upstream_sentence(monkeypatch):
    _install_probe_client(monkeypatch, status=400, body=LITELLM_400_BODY)
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "high")
    )
    assert result.status == "rejected"
    assert result.detail == UPSTREAM_SENTENCE
    # Kill: the litellm wrapper names the internal gateway. It must not travel.
    assert "172.16.120.38" not in result.detail
    assert "Received Model Group" not in result.detail


def test_probe_reads_an_unwrapped_backend_error_too(monkeypatch):
    body = json.dumps(
        {
            "error": {
                "message": (
                    "Unexpected reasoning effort minimal. "
                    "Supported types are xhigh (default), medium, and low."
                )
            }
        }
    )
    _install_probe_client(monkeypatch, status=400, body=body)
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "minimal")
    )
    assert result.status == "rejected"
    assert result.detail == (
        "Unexpected reasoning effort minimal. "
        "Supported types are xhigh (default), medium, and low."
    )


def test_probe_drops_a_sentence_that_carries_an_address(monkeypatch):
    body = json.dumps(
        {"error": {"message": "reasoning effort rejected by http://10.53.100.12/v1"}}
    )
    _install_probe_client(monkeypatch, status=400, body=body)
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "high")
    )
    assert result.status == "rejected"
    assert result.detail is None


def test_probe_treats_an_unrelated_400_as_unreachable(monkeypatch):
    _install_probe_client(
        monkeypatch, status=400, body='{"error":{"message":"model not found"}}'
    )
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "high")
    )
    assert result.status == "unreachable"


@pytest.mark.parametrize("status", [401, 404, 500, 503])
def test_probe_never_calls_a_non_400_a_rejection(monkeypatch, status):
    _install_probe_client(monkeypatch, status=status, body="")
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "high")
    )
    assert result.status == "unreachable"


def test_probe_reports_unreachable_on_transport_failure(monkeypatch):
    _install_probe_client(
        monkeypatch, raise_exc=httpx.ConnectError("no route to host")
    )
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(_probe_target(), "high")
    )
    assert result.status == "unreachable"
    assert "ConnectError" in (result.detail or "")


def test_probe_does_not_call_an_endpoint_the_guard_refuses(monkeypatch):
    calls = _install_probe_client(monkeypatch, status=200, body="{}")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    result = asyncio.run(
        thinking_probe.probe_thinking_effort(
            _probe_target(endpoint_url="http://169.254.169.254/latest"), "high"
        )
    )
    assert result.status == "unreachable"
    assert calls == []


# ── create / update wire the probe ───────────────────────────────────────────


def _stub_probe(monkeypatch, status: str, detail: str | None = None) -> list:
    seen: list = []

    async def fake_probe(model_like, level):
        seen.append({"name": getattr(model_like, "name", None), "level": level})
        return thinking_probe.ProbeResult(status, detail)

    monkeypatch.setattr(models_api, "probe_thinking_effort", fake_probe)
    return seen


def _admin_headers(client, db, username: str, role: str = "owner") -> dict:
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username)}"}


def test_create_refuses_a_level_the_endpoint_rejects(client, db, monkeypatch):
    seen = _stub_probe(monkeypatch, "rejected", UPSTREAM_SENTENCE)
    headers = _admin_headers(client, db, "owner-probe-rej")
    resp = client.post(
        "/api/models",
        json={
            "name": "probe-rejected",
            "display_name": "Probe Rejected",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
            "thinking_effort": "high",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert UPSTREAM_SENTENCE in resp.json()["detail"]
    assert seen == [{"name": "probe-rejected", "level": "high"}]
    # The row must not exist — a saved level the endpoint refuses 400s on
    # every later chat call.
    assert (
        db.query(ModelRegistry).filter(ModelRegistry.name == "probe-rejected").first()
        is None
    )


def test_create_survives_an_unreachable_endpoint_and_says_so(client, db, monkeypatch):
    _stub_probe(monkeypatch, "unreachable", "無法連線（ConnectError）")
    headers = _admin_headers(client, db, "owner-probe-down")
    resp = client.post(
        "/api/models",
        json={
            "name": "probe-down",
            "display_name": "Probe Down",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
            "thinking_effort": "xhigh",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["thinking_probe"]["status"] == "unreachable"
    assert body["thinking_effort"] == "xhigh"
    assert (
        db.query(ModelRegistry).filter(ModelRegistry.name == "probe-down").first()
        is not None
    )


def test_create_reports_ok_when_the_endpoint_accepts(client, db, monkeypatch):
    _stub_probe(monkeypatch, "ok")
    headers = _admin_headers(client, db, "owner-probe-ok")
    resp = client.post(
        "/api/models",
        json={
            "name": "probe-ok",
            "display_name": "Probe OK",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
            "thinking_effort": "medium",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking_probe"]["status"] == "ok"


def test_create_with_none_never_probes(client, db, monkeypatch):
    seen = _stub_probe(monkeypatch, "rejected", UPSTREAM_SENTENCE)
    headers = _admin_headers(client, db, "owner-probe-none")
    resp = client.post(
        "/api/models",
        json={
            "name": "probe-none",
            "display_name": "Probe None",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
            "thinking_effort": "none",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert seen == []
    assert resp.json().get("thinking_probe") is None


def test_update_refuses_a_rejected_level_and_keeps_the_old_one(client, db, monkeypatch):
    model = make_model(db, name="probe-upd-rej")
    model.thinking_effort = "medium"
    db.commit()
    _stub_probe(monkeypatch, "rejected", UPSTREAM_SENTENCE)
    headers = _admin_headers(client, db, "admin-probe-rej", role="admin")
    resp = client.put(
        f"/api/models/{model.id}",
        json={"thinking_effort": "max"},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert UPSTREAM_SENTENCE in resp.json()["detail"]
    db.expire_all()
    assert (
        db.query(ModelRegistry).filter(ModelRegistry.id == model.id).one().thinking_effort
        == "medium"
    )


def test_update_to_an_unreachable_endpoint_still_saves(client, db, monkeypatch):
    model = make_model(db, name="probe-upd-down")
    _stub_probe(monkeypatch, "unreachable", "無法連線（ConnectTimeout）")
    headers = _admin_headers(client, db, "admin-probe-down", role="admin")
    resp = client.put(
        f"/api/models/{model.id}",
        json={"thinking_effort": "low"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["thinking_probe"]["status"] == "unreachable"
    db.expire_all()
    assert (
        db.query(ModelRegistry).filter(ModelRegistry.id == model.id).one().thinking_effort
        == "low"
    )


def test_update_does_not_probe_an_unchanged_level(client, db, monkeypatch):
    model = make_model(db, name="probe-upd-same")
    model.thinking_effort = "medium"
    db.commit()
    seen = _stub_probe(monkeypatch, "rejected", UPSTREAM_SENTENCE)
    headers = _admin_headers(client, db, "admin-probe-same", role="admin")
    resp = client.put(
        f"/api/models/{model.id}",
        json={"thinking_effort": "medium", "display_name": "Renamed"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert seen == []
    assert resp.json().get("thinking_probe") is None


def test_update_of_an_unrelated_field_never_probes(client, db, monkeypatch):
    model = make_model(db, name="probe-upd-other")
    model.thinking_effort = "medium"
    db.commit()
    seen = _stub_probe(monkeypatch, "rejected", UPSTREAM_SENTENCE)
    headers = _admin_headers(client, db, "admin-probe-other", role="admin")
    resp = client.put(
        f"/api/models/{model.id}",
        json={"display_name": "Only a rename"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert seen == []


def test_embedding_row_is_never_probed(client, db, monkeypatch):
    """Embedding / agent rows never carry reasoning_effort — nothing to ask."""
    seen = _stub_probe(monkeypatch, "rejected", UPSTREAM_SENTENCE)
    headers = _admin_headers(client, db, "owner-probe-embed")
    resp = client.post(
        "/api/models",
        json={
            "name": "probe-embed",
            "display_name": "Probe Embed",
            "model_type": "embedding",
            "endpoint_url": "https://gateway.example.com/v1",
            "thinking_effort": "medium",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert seen == []


def test_list_and_get_never_carry_a_probe_block(client, db):
    model = make_model(db, name="probe-listing")
    model.thinking_effort = "medium"
    db.commit()
    headers = _admin_headers(client, db, "admin-probe-list", role="admin")
    got = client.get(f"/api/models/{model.id}", headers=headers).json()
    assert got["thinking_probe"] is None
    listed = client.get("/api/models", headers=headers).json()
    assert all(row["thinking_probe"] is None for row in listed)
