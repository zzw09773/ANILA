"""員編下傳:單元測試。

鎖住「送訊息時夾帶員編」設計的不變量(spec:
docs/specs/specs/2026-06-23-employee-id-downstream-design.md):

- ``downstream_identity``: 卡登 ``username == 員編`` → 回員編;非卡片帳號
  (admin)/空/格式不符 → None(呼叫端據此**省略**身分 header、不偽造,請求照常)。
- ``build_model_gateway_headers``: 只帶員編,**絕不**帶 ``X-CSP-Service-Token``
  (CRITICAL 回歸鎖:服務憑證不可外流到 .12 模型閘道),也不帶 email/groups。
- ``build_agent_headers`` (P2.1): ``Authorization: Bearer`` 簽章 JWT +
  可選 task/trace;絕不送 ``X-CSP-Service-Token`` 或明文 ``X-ANILA-User-*``。
- ``proxy_stream`` 路由:MODEL 目的地一律走 model-gateway builder,即使設了
  legacy ``CSP_SERVICE_TOKEN`` 也絕不把 service token 送上模型閘道(整合層回歸鎖)。
"""
from __future__ import annotations

import asyncio

from app.services import proxy_service
from app.services.proxy import service as proxy_impl
from app.services.proxy_service import (

    build_agent_headers,
    build_model_gateway_headers,
    downstream_identity,
)

from app.services.proxy.service import ProxyTuning

#: 這些測試量的不是逾時／重試（那四顆在 ``test_settings_takes_effect_ops.py``），
#: 所以把它們釘在登錄表宣告的程式預設值上。``tuning`` 是必填的關鍵字參數：
#: production 的每一個呼叫點都要自己從 session 解一次，漏傳是 TypeError 而不是
#: 靜默凍結在預設值 —— 那個「必填」正是本包不想再出現假控制項的那道保險。
_PROXY_TUNING = ProxyTuning.from_registry_defaults()


class _User:
    """Duck-typed stand-in for the ORM User (only ``username`` is read)."""

    def __init__(self, username):
        self.username = username


class TestDownstreamIdentity:
    def test_card_user_returns_employee_id(self):
        assert downstream_identity(_User("1147259")) == "1147259"
        assert downstream_identity(_User("1234567")) == "1234567"

    def test_admin_non_numeric_fails_closed(self):
        # admin 帳密登入 username='admin' → 不送身分(不偽造 "admin" 當員編)。
        assert downstream_identity(_User("admin")) is None

    def test_blank_and_malformed_fail_closed(self):
        assert downstream_identity(_User("")) is None
        assert downstream_identity(_User("12")) is None          # 太短 (<6)
        assert downstream_identity(_User("1234567890")) is None  # 太長 (>9)
        assert downstream_identity(_User("11a4567")) is None     # 含非數字
        assert downstream_identity(_User(None)) is None


class TestModelGatewayHeaders:
    def test_carries_only_employee_id(self):
        h = build_model_gateway_headers("1147259")
        assert h["X-ANILA-User-Id"] == "1147259"

    def test_never_carries_service_token(self):
        # CRITICAL 回歸鎖:模型閘道(.12)絕不可拿到 CSP service token。
        h = build_model_gateway_headers("1147259")
        assert "X-CSP-Service-Token" not in h

    def test_no_pii_to_model(self):
        h = build_model_gateway_headers("1147259")
        assert "X-ANILA-User-Email" not in h
        assert "X-ANILA-User-Groups" not in h

    def test_none_identity_omits_header(self):
        h = build_model_gateway_headers(None)
        assert "X-ANILA-User-Id" not in h
        assert "X-CSP-Service-Token" not in h


class TestAgentHeaders:
    def test_signed_bearer_no_plaintext_identity(self):
        h = build_agent_headers(user_id=7, department=3, agent_id=42)
        assert h["Authorization"].startswith("Bearer ")
        assert "X-CSP-Service-Token" not in h
        assert "X-ANILA-User-Id" not in h
        assert "X-ANILA-User-Email" not in h
        assert "X-ANILA-User-Groups" not in h

    def test_department_none_still_mints(self):
        # 非部門帳號 (admin): department claim 可為 null,請求照常。
        h = build_agent_headers(user_id=1, department=None, agent_id=9)
        assert h["Authorization"].startswith("Bearer ")
        assert "X-ANILA-User-Id" not in h


class TestServiceTokenScoping:
    def test_model_gateway_never_gets_token_even_when_legacy_configured(self, monkeypatch):
        """Even with a legacy fleet-shared CSP_SERVICE_TOKEN set, the
        model-gateway path never carries it. Agent dispatch uses a signed
        JWT Bearer, not X-CSP-Service-Token (P2.1)."""
        from app.services import proxy_service

        monkeypatch.setattr(
            proxy_service.settings, "CSP_SERVICE_TOKEN", "csk-legacy", raising=False
        )
        agent_h = build_agent_headers(user_id=1, department=None, agent_id=99)
        assert "X-CSP-Service-Token" not in agent_h
        assert agent_h["Authorization"].startswith("Bearer ")
        model_h = build_model_gateway_headers("1147259")
        assert "X-CSP-Service-Token" not in model_h

    def test_agent_dispatch_ignores_legacy_fleet_token(self, monkeypatch):
        """Dispatch identity is the signed JWT; legacy CSP_SERVICE_TOKEN
        must not appear on the wire even when configured."""
        from app.services import proxy_service

        monkeypatch.setattr(
            proxy_service.settings, "CSP_SERVICE_TOKEN", "csk-legacy", raising=False
        )
        h = build_agent_headers(user_id=1, department=2, agent_id=12345)
        assert "X-CSP-Service-Token" not in h
        assert h["Authorization"].startswith("Bearer ")


class _HeaderCapturingStream:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _HeaderCapturingClient:
    """Fake httpx.AsyncClient that records the headers passed to .stream()."""

    last_headers: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json, headers):
        type(self).last_headers = dict(headers)
        return _HeaderCapturingStream(
            [
                'data: {"choices":[{"index":0,"delta":{"content":"hi"},'
                '"finish_reason":"stop"}]}',
                "",
                "data: [DONE]",
                "",
            ]
        )


class TestProxyStreamRoutingNeverLeaksToken:
    """Integration-level regression lock for the CRITICAL invariant: the
    header builder is chosen by DESTINATION (target_agent_id), so a MODEL
    stream never carries the CSP service token even with a legacy
    CSP_SERVICE_TOKEN configured — closing the 'mis-set flag leaks token to
    the model gateway' gap."""

    def test_model_stream_forwards_employee_id_but_never_service_token(self, monkeypatch):
        # http single-label mock target needs the dev SSRF allowances.
        monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
        monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
        monkeypatch.setattr(
            proxy_service.settings, "CSP_SERVICE_TOKEN", "csk-legacy", raising=False
        )
        _HeaderCapturingClient.last_headers = {}
        monkeypatch.setattr(
            proxy_service.httpx,
            "AsyncClient",
            lambda *a, **k: _HeaderCapturingClient(*a, **k),
        )

        async def _fake_enqueue_usage(**kwargs):
            return None

        monkeypatch.setattr(proxy_impl, "enqueue_usage", _fake_enqueue_usage)

        async def _run():
            async for _ in proxy_service.proxy_stream(
                target_url="http://mock-llm/v1/chat/completions",
                api_key_id=1,
                user_id=2,  # DB PK → usage attribution only
                department_id=None,
                usage_model_id=3,
                request_body={
                    "model": "m",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                user_identity="1147259",  # 員編 → wire identity
                model_name="m",
                target_agent_id=None,  # MODEL destination
                tuning=_PROXY_TUNING,
            ):
                pass

        asyncio.run(_run())
        h = _HeaderCapturingClient.last_headers
        assert "X-CSP-Service-Token" not in h  # CRITICAL: no service token to model
        assert h.get("X-ANILA-User-Id") == "1147259"  # 員編 forwarded for traceability
        assert "X-ANILA-User-Email" not in h  # no end-user PII into model logs


class _PostCapturingResponse:
    def __init__(self, payload):
        import json as _json

        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self._payload = payload
        self.text = _json.dumps(payload)

    def json(self):
        return self._payload


class _PostCapturingClient:
    """Fake httpx.AsyncClient recording headers passed to .post()."""

    last_headers: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json, headers):
        type(self).last_headers = dict(headers)
        return _PostCapturingResponse(
            {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


class _FakeModel:
    """Duck-typed ModelRegistry for a non-agent (LLM) destination."""

    model_type = "llm"  # != "agent" → model-gateway routing
    endpoint_url = "http://mock-llm"
    api_version = "v1"
    name = "m"
    id = 3


class TestProxyRequestRoutingNeverLeaksToken:
    """Same CRITICAL invariant as the stream test, but on the NON-stream
    proxy_request branch (model.model_type != 'agent'): the model gateway
    must never receive X-CSP-Service-Token even with a legacy token set."""

    def test_model_request_forwards_employee_id_but_never_service_token(self, monkeypatch):
        monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
        monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
        monkeypatch.setattr(
            proxy_service.settings, "CSP_SERVICE_TOKEN", "csk-legacy", raising=False
        )
        monkeypatch.setattr(
            proxy_service.settings, "MODEL_GATEWAY_API_KEY", "", raising=False
        )
        _PostCapturingClient.last_headers = {}
        monkeypatch.setattr(
            proxy_service.httpx,
            "AsyncClient",
            lambda *a, **k: _PostCapturingClient(*a, **k),
        )

        async def _fake_enqueue_usage(**kwargs):
            return None

        monkeypatch.setattr(proxy_impl, "enqueue_usage", _fake_enqueue_usage)

        async def _run():
            return await proxy_service.proxy_request(
                model=_FakeModel(),
                api_key_id=1,
                user_id=2,  # DB PK → usage attribution only
                department_id=None,
                request_body={
                    "model": "m",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                endpoint_path="/v1/chat/completions",
                user_identity="1147259",  # 員編 → wire identity
                tuning=_PROXY_TUNING,
            )

        asyncio.run(_run())
        h = _PostCapturingClient.last_headers
        assert "X-CSP-Service-Token" not in h  # CRITICAL: no service token to model
        assert h.get("X-ANILA-User-Id") == "1147259"  # 員編 forwarded
        assert "X-ANILA-User-Email" not in h  # no end-user PII into model logs
