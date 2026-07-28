"""Router token 預算 —— 依部署模型的真實容量算 max_tokens。

為什麼有這批測試
----------------
Router 原本呼叫主模型時完全不宣告 ``max_tokens``。以 ``-c 8192 --parallel 8``
起 llama-server 時每個 slot 只有 1024 tokens,使用者貼一份論文進來,路由決策
100% 失敗,而使用者只看到「目前無法安全判斷是否需要 Agent」——分不出是模型拒答
還是 token 不夠。這裡把三件事釘住:

1. 有登記 ``context_window`` 時,實際送出的 ``max_tokens`` 不得超過剩餘預算。
2. 輸入超出容量時,錯誤碼/訊息必須明確指出是 **token 預算** 問題。
3. ``context_window`` 未登記時的 fail-safe 行為(不宣告 max_tokens、不拒絕輸入)
   有被斷言,不是靠猜。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from anila_contracts.classification import ClassificationLevel
from anila_core.compact.auto_compact import MAX_OUTPUT_TOKENS_FOR_SUMMARY
from anila_core.router.csp_registry_client import (
    CspDirectModelGovernanceClient,
    CspInferenceClient,
)
from anila_core.router.policy_gate import DirectModelGovernance
from anila_core.router.token_budget import (
    REASON_CONTEXT_WINDOW_INVALID,
    REASON_CONTEXT_WINDOW_UNDECLARED,
    REASON_INPUT_EXCEEDS_CONTEXT,
    RouterInputExceedsModelContext,
    RouterTokenBudgetPolicy,
    estimate_wire_input_tokens,
    normalize_context_window,
    plan_router_token_budget,
)


CONTEXT_WINDOW = 8192


class _Ctx:
    """RequestContext 替身 —— 只帶 build_request 需要的欄位。"""

    identity = "123"
    owner_id = "123"
    task_id = "1"
    run_id = "1"
    source_snapshot_id = "1"
    trace_id = "trace-1"
    invocation_id = "inv-1"
    session_id = "sess-1"
    task_type = "chat"
    classification = "無機密"
    scopes = ("router:route",)
    required_capabilities = ()
    auth_assurance = {"method": "password", "level": "aal1"}


def _client(
    *, policy: RouterTokenBudgetPolicy | None = None
) -> CspInferenceClient:
    return CspInferenceClient(
        "https://csp.test",
        service_token="csk-router-primary",
        budget_policy=policy or RouterTokenBudgetPolicy(),
    )


def _messages(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


# ── 1. max_tokens 不得超過剩餘預算 ─────────────────────────────────────────


def test_payload_declares_max_tokens_within_remaining_budget() -> None:
    """context_window=8192 時,送出的 max_tokens 必須留得下輸入。"""

    messages = _messages("請幫我判斷這個問題需不需要 Agent。" * 20)
    request = _client().build_request(
        model="google/gemma4",
        messages=messages,
        context=_Ctx(),
        stream=False,
        context_window=CONTEXT_WINDOW,
    )

    assert "max_tokens" in request.payload
    max_tokens = request.payload["max_tokens"]
    input_tokens = estimate_wire_input_tokens(messages)
    # 剩餘預算 = 真實容量 - 估算輸入。宣告的輸出上限絕不能吃掉輸入的空間。
    assert 0 < max_tokens <= CONTEXT_WINDOW - input_tokens
    assert input_tokens + max_tokens <= CONTEXT_WINDOW


def test_max_tokens_shrinks_as_input_grows_and_never_exceeds_budget() -> None:
    """輸入愈長,宣告的 max_tokens 必須跟著縮,而且始終在預算內。"""

    policy = RouterTokenBudgetPolicy()
    client = _client(policy=policy)
    previous: int | None = None
    for repeat in (1, 200, 1000, 1600):
        messages = _messages("台灣海峽的水文資料摘要。" * repeat)
        payload = client.build_request(
            model="google/gemma4",
            messages=messages,
            context=_Ctx(),
            stream=False,
            context_window=CONTEXT_WINDOW,
        ).payload
        input_tokens = estimate_wire_input_tokens(messages)
        assert input_tokens + payload["max_tokens"] <= CONTEXT_WINDOW
        if previous is not None:
            assert payload["max_tokens"] <= previous
        previous = payload["max_tokens"]


def test_max_tokens_keeps_reasoning_headroom() -> None:
    """reasoning 模型要額外保留思考空間(實測 gemma 路由決策吃 359 tokens)。"""

    policy = RouterTokenBudgetPolicy(reasoning_reserve_tokens=1024)
    budget = plan_router_token_budget(
        messages=_messages("短問題"),
        context_window=CONTEXT_WINDOW,
        policy=policy,
    )
    assert budget.reasoning_reserve_tokens == 1024
    # 宣告的輸出上限必須容得下思考 + 回答,否則思考一長就沒空間吐決策。
    assert budget.max_tokens is not None
    assert budget.max_tokens >= 1024 + policy.min_answer_tokens
    assert budget.max_tokens == policy.desired_output_tokens


def test_reasoning_reserve_is_configurable_not_hardcoded() -> None:
    """保留量由環境變數決定,不是寫死的魔數。"""

    policy = RouterTokenBudgetPolicy.from_env(
        {
            "ANILA_ROUTER_REASONING_RESERVE_TOKENS": "2048",
            "ANILA_ROUTER_MIN_ANSWER_TOKENS": "128",
            "ANILA_ROUTER_MAX_ANSWER_TOKENS": "512",
            "ANILA_ROUTER_TOKEN_ESTIMATE_MARGIN": "64",
        }
    )
    assert policy.reasoning_reserve_tokens == 2048
    assert policy.min_answer_tokens == 128
    assert policy.max_answer_tokens == 512
    assert policy.estimate_margin_tokens == 64
    assert policy.desired_output_tokens == 2048 + 512
    assert policy.reserved_output_tokens == 2048 + 128


def test_from_env_falls_back_to_defaults_on_garbage() -> None:
    """設定打錯不得讓 Router 崩掉,退回預設。"""

    policy = RouterTokenBudgetPolicy.from_env(
        {
            "ANILA_ROUTER_REASONING_RESERVE_TOKENS": "not-a-number",
            "ANILA_ROUTER_MIN_ANSWER_TOKENS": "-5",
        }
    )
    assert policy == RouterTokenBudgetPolicy()


def test_policy_rejects_reserve_larger_than_auto_compact_cap() -> None:
    """保留量超過 auto_compact 的 cap 會被那邊默默夾掉 → 建構期就講明白。"""

    with pytest.raises(ValueError, match="MAX_OUTPUT_TOKENS_FOR_SUMMARY"):
        RouterTokenBudgetPolicy(
            reasoning_reserve_tokens=MAX_OUTPUT_TOKENS_FOR_SUMMARY,
            min_answer_tokens=1,
        )


def test_budget_wires_registry_window_into_auto_compact() -> None:
    """registry 的 context_window 真的被餵進既有的 auto_compact 門檻計算。"""

    from anila_core.compact.auto_compact import get_auto_compact_threshold

    policy = RouterTokenBudgetPolicy()
    budget = plan_router_token_budget(
        messages=_messages("短問題"),
        context_window=200_000,
        policy=policy,
    )
    assert budget.auto_compact_threshold == get_auto_compact_threshold(
        200_000, max_output_tokens=policy.reserved_output_tokens
    )
    assert budget.compact_recommended is False


# ── 2. 錯誤必須明確指出是 token 預算問題 ────────────────────────────────────


def test_input_over_context_window_raises_explicit_budget_error() -> None:
    """輸入超過 context_window → 明確的 token 預算錯誤,不是通用的「無法判斷」。"""

    # 每個中文字約 1/3 token(auto_compact 的字元近似),故遠超 8192 的輸入。
    oversized = _messages("論文內容片段。" * 6000)
    with pytest.raises(RouterInputExceedsModelContext) as excinfo:
        plan_router_token_budget(
            messages=oversized, context_window=CONTEXT_WINDOW
        )

    exc = excinfo.value
    assert exc.reason_code == REASON_INPUT_EXCEEDS_CONTEXT
    assert exc.context_window == CONTEXT_WINDOW
    assert exc.input_tokens > CONTEXT_WINDOW
    message = str(exc)
    # 訊息必須帶足以行動的資訊:是預算問題、容量多少、輸入多少、怎麼辦。
    assert "token 預算" in message
    assert str(CONTEXT_WINDOW) in message
    assert str(exc.input_tokens) in message
    assert "縮短輸入" in message
    assert "context_window 更大的模型" in message
    # 絕不能只丟一句通用文案。
    assert "無法安全判斷" not in message


def test_inference_client_refuses_oversized_input_before_sending() -> None:
    """預算不足時在送出前就擋下 —— 不浪費一趟必然失敗的上游呼叫。"""

    with pytest.raises(RouterInputExceedsModelContext) as excinfo:
        _client().build_request(
            model="google/gemma4",
            messages=_messages("論文內容片段。" * 6000),
            context=_Ctx(),
            stream=False,
            context_window=CONTEXT_WINDOW,
        )
    assert excinfo.value.reason_code == REASON_INPUT_EXCEEDS_CONTEXT


def test_router_surfaces_token_budget_reason_code_distinct_from_refusal() -> None:
    """token 預算不足與模型拒答必須是**不同**的 reason_code。

    這是這個 bug 最傷使用者的部分:兩者以前都被壓成
    「目前無法安全判斷是否需要 Agent」,使用者不知道該縮短輸入還是換模型。
    """

    from anila_core.api.router_server import (
        ROUTER_TOKEN_BUDGET_ERROR,
        _call_llm_non_stream,
    )

    class _BudgetBlownClient:
        """真的 CspInferenceClient,但綁一個必然吃不下的容量。"""

        def __init__(self) -> None:
            self._inner = _client()

        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            return await self._inner.complete(**kwargs)

    result = asyncio.run(
        _call_llm_non_stream(
            "sk-unused",
            _messages("論文內容片段。" * 6000),
            formal_context=_Ctx(),
            inference_client=_BudgetBlownClient(),
            context_window=CONTEXT_WINDOW,
        )
    )

    assert result["error"] == ROUTER_TOKEN_BUDGET_ERROR
    assert result["error_reason_code"] == REASON_INPUT_EXCEEDS_CONTEXT
    assert "token 預算" in result["error_message"]
    # 通用的 LLM 故障碼不得被拿來混充預算問題。
    assert not result["error"].startswith("LLM ")


def test_router_generic_llm_failure_keeps_its_own_error_code() -> None:
    """對照組:真的模型/上游故障仍是通用 LLM 錯誤,不會被誤標成預算問題。"""

    from anila_core.api.router_server import (
        ROUTER_TOKEN_BUDGET_ERROR,
        _call_llm_non_stream,
    )

    class _RefusingClient:
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("model refused")

    result = asyncio.run(
        _call_llm_non_stream(
            "sk-unused",
            _messages("短問題"),
            formal_context=_Ctx(),
            inference_client=_RefusingClient(),
            context_window=CONTEXT_WINDOW,
        )
    )
    assert result["error"] != ROUTER_TOKEN_BUDGET_ERROR
    assert result["error"].startswith("LLM ")
    assert "error_reason_code" not in result


# ── 3. context_window 未登記時的 fail-safe 行為 ─────────────────────────────


def test_undeclared_context_window_omits_max_tokens_and_stays_usable() -> None:
    """未登記容量:不宣告 max_tokens(不猜值)、不拒絕輸入(不誤殺),但留線索。

    選這個取捨的理由:靜默套猜測值會重演「無聲失敗」;直接讓功能不可用會讓每個
    尚未補登容量的部署一升級就全站掛掉。所以退回改動前行為 + 可觀測的 reason_code。
    """

    messages = _messages("論文內容片段。" * 6000)  # 對 8192 來說一定爆,但容量未知
    request = _client().build_request(
        model="google/gemma4",
        messages=messages,
        context=_Ctx(),
        stream=False,
        context_window=None,
    )

    # 不猜值 → payload 完全不帶 max_tokens(與改動前的 wire 形狀一致)。
    assert "max_tokens" not in request.payload
    assert set(request.payload) == {
        "model",
        "messages",
        "stream",
        "anila_session_id",
    }

    budget = plan_router_token_budget(messages=messages, context_window=None)
    assert budget.max_tokens is None
    assert budget.declares_max_tokens is False
    assert budget.reason_code == REASON_CONTEXT_WINDOW_UNDECLARED
    assert budget.input_ceiling is None
    # 不知道上限就沒有拒絕的正當性:輸入再長也不擋。
    assert budget.input_tokens > 0


def test_undeclared_capacity_warns_once_per_model(caplog) -> None:
    """未登記容量要「大聲」但不洗 log:每個模型只警告一次。"""

    client = _client()
    with caplog.at_level("WARNING", logger="anila_core.router.csp_registry_client"):
        for _ in range(3):
            client.build_request(
                model="google/gemma4",
                messages=_messages("短問題"),
                context=_Ctx(),
                stream=False,
                context_window=None,
            )
    warnings = [
        record
        for record in caplog.records
        if REASON_CONTEXT_WINDOW_UNDECLARED in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "context_window" in warnings[0].getMessage()


def test_invalid_context_window_is_never_used_as_a_hard_limit() -> None:
    """registry 值壞掉(0/負數/非整數)一律當成未登記,絕不變成硬上限。"""

    for bad in (0, -1, "8192", 8192.0, True):
        window, reason = normalize_context_window(bad)
        assert window is None
        assert reason == REASON_CONTEXT_WINDOW_INVALID

    request = _client().build_request(
        model="google/gemma4",
        messages=_messages("短問題"),
        context=_Ctx(),
        stream=False,
        context_window=0,
    )
    assert "max_tokens" not in request.payload


def test_normalize_distinguishes_undeclared_from_invalid() -> None:
    assert normalize_context_window(None) == (None, REASON_CONTEXT_WINDOW_UNDECLARED)
    assert normalize_context_window(8192) == (8192, None)


# ── 容量事實怎麼從 CSP 送到 Router ─────────────────────────────────────────


def test_governance_projection_carries_context_window() -> None:
    """容量事實與分類上限共用同一條受信任的 service-token 投影。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model_id": "google/gemma4",
                "gateway": "csp",
                "classification_ceiling": "機密",
                "context_window": CONTEXT_WINDOW,
            },
            request=request,
        )

    governance = asyncio.run(
        CspDirectModelGovernanceClient(
            "https://csp.test",
            service_token="csk-router-primary",
            transport=httpx.MockTransport(_handler),
        ).fetch("google/gemma4")
    )
    assert governance.context_window == CONTEXT_WINDOW
    assert governance.classification_ceiling == ClassificationLevel.from_storage("機密")


def test_missing_or_bad_context_window_does_not_fail_governance_closed() -> None:
    """容量缺失/無效只降級成 None,絕不把直答整體打成 fail-closed。

    授權欄位缺失必須拒絕,但容量缺失若也 fail-closed,任何尚未補登容量的 registry
    都會讓直答不可用 —— 那是用一個更大的故障去修一個較小的故障。
    """

    for raw in (None, 0, -1, "8192"):
        body: dict[str, Any] = {
            "model_id": "google/gemma4",
            "gateway": "csp",
            "classification_ceiling": "機密",
        }
        if raw is not None:
            body["context_window"] = raw

        def _handler(request: httpx.Request, payload: dict[str, Any] = body) -> httpx.Response:
            return httpx.Response(200, json=payload, request=request)

        governance = asyncio.run(
            CspDirectModelGovernanceClient(
                "https://csp.test",
                service_token="csk-router-primary",
                transport=httpx.MockTransport(_handler),
            ).fetch("google/gemma4")
        )
        assert governance.context_window is None
        assert governance.classification_ceiling is not None


def test_direct_model_governance_rejects_non_positive_context_window() -> None:
    """dataclass 層面也守住:容量欄位只接受正整數或 None。"""

    with pytest.raises(ValueError, match="context_window"):
        DirectModelGovernance(
            model_id="google/gemma4",
            gateway="csp",
            classification_ceiling=None,
            context_window=0,
        )


# ── 端到端:使用者真正看到的東西 ────────────────────────────────────────────
#
# 上面的單元測試釘住了 seam,但這個 bug 最傷人的地方是**使用者看到的那句話**。
# 這兩個測試走完整的 formal HTTP 路徑,直接比對回傳的 reason_codes 與訊息,
# 證明「token 預算不足」與「模型無法回應」不會再被壓成同一句話。
# 借用 test_router_r3_formal_wiring 的 formal-request 簽章工具(JWKS/JWT/registry
# 替身),不重蓋一份。


def _formal_harness():
    from tests.test_router_r3_formal_wiring import (  # noqa: PLC0415
        _Registry,
        _create_formal_app,
        _headers,
        _snapshot,
    )

    return _Registry, _create_formal_app, _headers, _snapshot


def test_e2e_token_budget_message_is_distinct_from_routing_model_unavailable() -> None:
    from fastapi.testclient import TestClient

    _Registry, _create_formal_app, _headers, _snapshot = _formal_harness()

    long_query = "論文內容片段。" * 6000
    body = {
        "session_id": "session-1",
        "messages": [{"role": "user", "content": long_query}],
    }
    app = _create_formal_app(
        session_factory=lambda _sid: None,
        registry_client=_Registry(_snapshot()),
        # 注入的治理值帶上真實容量,等同 registry 已登記 context_window=8192。
        direct_model_governance=DirectModelGovernance(
            model_id="google/gemma4",
            gateway="csp",
            classification_ceiling=ClassificationLevel.from_storage("機密"),
            context_window=CONTEXT_WINDOW,
        ),
        inference_client=_client(),
    )
    response = TestClient(app).post(
        "/v1/chat/completions", headers=_headers(body=body), json=body
    )

    assert response.status_code == 200
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    reason_codes = payload["anila_meta"]["reason_codes"]

    # 使用者拿到的是可行動的訊息,不是那句什麼都沒說的通用文案。
    assert REASON_INPUT_EXCEEDS_CONTEXT in reason_codes
    assert "ROUTING_MODEL_UNAVAILABLE" not in reason_codes
    assert "token 預算" in content
    assert str(CONTEXT_WINDOW) in content
    assert "無法安全判斷是否需要 Agent" not in content


def test_e2e_model_refusal_still_reports_routing_model_unavailable() -> None:
    """對照組:模型真的拒答/故障時,仍是原本那條通用路徑,沒有被誤標。"""

    from fastapi.testclient import TestClient

    _Registry, _create_formal_app, _headers, _snapshot = _formal_harness()

    class _RefusingClient:
        async def complete(self, **kwargs: Any) -> dict[str, Any]:
            raise ValueError("model refused")

    body = {
        "session_id": "session-1",
        "messages": [{"role": "user", "content": "短問題"}],
    }
    app = _create_formal_app(
        session_factory=lambda _sid: None,
        registry_client=_Registry(_snapshot()),
        direct_model_governance=DirectModelGovernance(
            model_id="google/gemma4",
            gateway="csp",
            classification_ceiling=ClassificationLevel.from_storage("機密"),
            context_window=CONTEXT_WINDOW,
        ),
        inference_client=_RefusingClient(),
    )
    response = TestClient(app).post(
        "/v1/chat/completions", headers=_headers(body=body), json=body
    )

    assert response.status_code == 200
    payload = response.json()
    reason_codes = payload["anila_meta"]["reason_codes"]
    assert reason_codes == ["ROUTING_MODEL_UNAVAILABLE"]
    assert REASON_INPUT_EXCEEDS_CONTEXT not in reason_codes
    assert "無法安全判斷是否需要 Agent" in payload["choices"][0]["message"]["content"]


def test_wire_overhead_is_counted_in_the_input_estimate() -> None:
    """content 以外的 wire 欄位(tool_calls 等)也會被上游計費,不能漏算。"""

    plain = [{"role": "user", "content": "hello"}]
    with_overhead = [
        {
            "role": "assistant",
            "content": "hello",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": json.dumps({"q": "x" * 400})},
                }
            ],
        }
    ]
    assert estimate_wire_input_tokens(with_overhead) > estimate_wire_input_tokens(plain)


def test_estimator_tolerates_non_mapping_messages() -> None:
    """壞形狀的 messages 不得讓預算計算爆掉(輸入驗證在別處,這裡只求不崩)。"""

    assert estimate_wire_input_tokens(["raw string", 42, None]) >= 0
    assert estimate_wire_input_tokens("not a list") == 0
    assert estimate_wire_input_tokens(None) == 0


def test_near_compact_threshold_is_logged_not_silently_swallowed(caplog) -> None:
    """跨過 auto_compact 門檻要留下 INFO 線索(還沒到擋下的程度,但已貼近懸崖)。"""

    # 200k window 的門檻是正值(200000-1280-13000=185720),不會踩到小 window 的
    # 門檻退化問題。3 字元 ≈ 1 token,故 190k 次重複剛好落在門檻之上、上限之下。
    long_enough = _messages("段落。" * 190_000)
    client = _client()
    with caplog.at_level("INFO", logger="anila_core.router.csp_registry_client"):
        payload = client.build_request(
            model="big/model",
            messages=long_enough,
            context=_Ctx(),
            stream=False,
            context_window=200_000,
        ).payload
    assert "max_tokens" in payload
    assert any(
        "auto_compact 門檻" in record.getMessage() for record in caplog.records
    )


def test_slot_too_small_for_the_router_prompt_is_diagnosable() -> None:
    """真實踩過的情境:``-c 8192 --parallel 8`` → 每 slot 只有 1024 tokens。

    這種容量連路由 prompt 都放不下,以前只會回一句「目前無法安全判斷是否需要
    Agent」。現在必須明確講出「上限是多少」,運維才知道要改 --parallel 或換模型。
    """

    with pytest.raises(RouterInputExceedsModelContext) as excinfo:
        plan_router_token_budget(
            messages=_messages("你好，請問這題要不要派 Agent?"),
            context_window=1024,  # 8192 / --parallel 8
        )
    exc = excinfo.value
    assert exc.reason_code == REASON_INPUT_EXCEEDS_CONTEXT
    assert exc.context_window == 1024
    # 上限被夾在 0 而不是變成負數的謎樣數字。
    assert exc.input_ceiling == 0
    assert "1024" in str(exc)
    assert "token 預算" in str(exc)
