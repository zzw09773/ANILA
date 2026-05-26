"""Unit tests:P1-15 Cost tracker + USD pricing。

涵蓋面
======

* `ModelPricing` dataclass:
  - 算 cost(prompt + completion 混合)正確
  - immutable(frozen)
  - 非法值(空 model name / 負單價 / 負 token)reject

* `PricingRegistry`:
  - 預設 6 個 model(gpt-4-turbo / gpt-4o / claude-3-5-sonnet /
    claude-3-opus / gemini-1.5-pro / anila-gemma4)都查得到
  - 自定 model `register` 對(含覆寫既有)
  - 未知 model 預設丟 KeyError;`allow_missing=True` 回 $0 fallback
  - `__contains__` / `models()` 行為正確

* `CostTracker`:
  - `record` 累加 USD + token,回傳本次 cost
  - 同 model 多次 record 累加
  - 不同 model 各自累加,`by_model()` 分群正確
  - `total_usd` / `total_tokens` / `total_records` 跨 model 加總
  - on-prem model = $0 但仍記 token
  - 未知 model + `allow_missing_pricing=True`(預設) → $0 但仍記 token
  - 未知 model + `allow_missing_pricing=False` → KeyError
  - `reset` 歸零
  - `summary_str` 含 USD + token 用量 + per-model 細項
  - 空 tracker `summary_str` 回 sentinel 訊息

* `from_budget_tracker` integration:
  - P1-9 BudgetTracker 已累計 token → CostTracker 換算 USD 對

* P0-9 tracing integration:
  - `inject_span_attributes` 寫 `cost.usd` / `cost.prompt_tokens` /
    `cost.completion_tokens` / `cost.model` 到 span attributes
  - 用 capture processor 驗 span end 事件帶上述 attribute
"""

from __future__ import annotations

import pytest

from anila_agent.core.cost_tracker import (
    DEFAULT_MODEL_PRICINGS,
    SPAN_ATTR_COMPLETION_TOKENS,
    SPAN_ATTR_COST_USD,
    SPAN_ATTR_MODEL,
    SPAN_ATTR_PROMPT_TOKENS,
    CostTracker,
    ModelPricing,
    PricingRegistry,
    inject_span_attributes,
)
from anila_agent.core.token_budget import BudgetTracker
from anila_agent.tracing import Span, Tracer
from anila_agent.tracing.types import Trace


# ---------------------------------------------------------------------------
# ModelPricing
# ---------------------------------------------------------------------------


def test_model_pricing_cost_usd_basic() -> None:
    """純 prompt + 純 completion 混合算 cost 應正確。"""
    pricing = ModelPricing(
        model="gpt-4o",
        prompt_usd_per_1k=0.0025,
        completion_usd_per_1k=0.01,
    )
    # 1000 prompt + 500 completion → 1*0.0025 + 0.5*0.01 = 0.0025 + 0.005 = 0.0075
    cost = pricing.cost_usd(prompt_tokens=1000, completion_tokens=500)
    assert cost == pytest.approx(0.0075)


def test_model_pricing_cost_usd_partial_thousand() -> None:
    """非整千 token 應比例計價(浮點 OK)。"""
    pricing = ModelPricing(
        model="gpt-4-turbo",
        prompt_usd_per_1k=0.01,
        completion_usd_per_1k=0.03,
    )
    # 500 prompt + 250 completion → 0.5*0.01 + 0.25*0.03 = 0.005 + 0.0075 = 0.0125
    cost = pricing.cost_usd(prompt_tokens=500, completion_tokens=250)
    assert cost == pytest.approx(0.0125)


def test_model_pricing_cost_usd_zero_tokens() -> None:
    """0 token 應回 $0,不丟例外。"""
    pricing = ModelPricing(
        model="gpt-4o",
        prompt_usd_per_1k=0.0025,
        completion_usd_per_1k=0.01,
    )
    assert pricing.cost_usd(0, 0) == 0.0


def test_model_pricing_cost_usd_on_prem_is_zero() -> None:
    """on-prem ($0 / $0) 任何 token 用量都回 $0。"""
    pricing = ModelPricing(
        model="anila-gemma4",
        prompt_usd_per_1k=0.0,
        completion_usd_per_1k=0.0,
    )
    assert pricing.cost_usd(10_000, 5_000) == 0.0


def test_model_pricing_is_frozen() -> None:
    """`ModelPricing` 應為 frozen dataclass — 不能改欄位。"""
    pricing = ModelPricing(
        model="gpt-4o", prompt_usd_per_1k=0.0025, completion_usd_per_1k=0.01
    )
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        pricing.model = "other"  # type: ignore[misc]


def test_model_pricing_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ModelPricing(model="", prompt_usd_per_1k=0.01, completion_usd_per_1k=0.03)


def test_model_pricing_rejects_negative_prompt_price() -> None:
    with pytest.raises(ValueError, match="prompt_usd_per_1k"):
        ModelPricing(
            model="gpt-4o",
            prompt_usd_per_1k=-0.01,
            completion_usd_per_1k=0.01,
        )


def test_model_pricing_rejects_negative_completion_price() -> None:
    with pytest.raises(ValueError, match="completion_usd_per_1k"):
        ModelPricing(
            model="gpt-4o",
            prompt_usd_per_1k=0.01,
            completion_usd_per_1k=-0.01,
        )


def test_model_pricing_rejects_negative_tokens() -> None:
    pricing = ModelPricing(
        model="gpt-4o", prompt_usd_per_1k=0.0025, completion_usd_per_1k=0.01
    )
    with pytest.raises(ValueError, match=">= 0"):
        pricing.cost_usd(prompt_tokens=-1, completion_tokens=0)
    with pytest.raises(ValueError, match=">= 0"):
        pricing.cost_usd(prompt_tokens=0, completion_tokens=-1)


# ---------------------------------------------------------------------------
# PricingRegistry — 預設 + register
# ---------------------------------------------------------------------------


def test_registry_default_models_all_present() -> None:
    """預設 register 6 個 model 都查得到,且單價符合 spec。"""
    reg = PricingRegistry()
    expected = {
        "gpt-4-turbo": (0.01, 0.03),
        "gpt-4o": (0.0025, 0.01),
        "claude-3-5-sonnet": (0.003, 0.015),
        "claude-3-opus": (0.015, 0.075),
        "gemini-1.5-pro": (0.00125, 0.005),
        "anila-gemma4": (0.0, 0.0),
    }
    for model, (prompt, completion) in expected.items():
        pricing = reg.get(model)
        assert pricing.prompt_usd_per_1k == pytest.approx(prompt)
        assert pricing.completion_usd_per_1k == pytest.approx(completion)


def test_registry_default_pricings_constant_length() -> None:
    """`DEFAULT_MODEL_PRICINGS` 必須有 6 個 entry(spec 數量)。"""
    assert len(DEFAULT_MODEL_PRICINGS) == 6


def test_registry_register_custom_model() -> None:
    """自定 register 應可後續查得到。"""
    reg = PricingRegistry()
    reg.register(
        ModelPricing(
            model="custom-foo",
            prompt_usd_per_1k=0.5,
            completion_usd_per_1k=1.0,
        )
    )
    pricing = reg.get("custom-foo")
    assert pricing.prompt_usd_per_1k == 0.5
    assert pricing.completion_usd_per_1k == 1.0


def test_registry_register_overwrites_existing() -> None:
    """同名 register 應覆寫 — 不丟例外。"""
    reg = PricingRegistry()
    reg.register(
        ModelPricing(
            model="gpt-4o",
            prompt_usd_per_1k=0.999,
            completion_usd_per_1k=1.0,
        )
    )
    assert reg.get("gpt-4o").prompt_usd_per_1k == 0.999


def test_registry_get_unknown_raises_by_default() -> None:
    reg = PricingRegistry()
    with pytest.raises(KeyError, match="unknown model"):
        reg.get("totally-fake-model")


def test_registry_get_unknown_allow_missing_returns_zero() -> None:
    """`allow_missing=True` 應回 $0 fallback,model name 保留。"""
    reg = PricingRegistry()
    fallback = reg.get("totally-fake-model", allow_missing=True)
    assert fallback.model == "totally-fake-model"
    assert fallback.prompt_usd_per_1k == 0.0
    assert fallback.completion_usd_per_1k == 0.0


def test_registry_empty_initial_is_pure_empty() -> None:
    """傳空 list 不應 fallback 到預設 — 真的空。"""
    reg = PricingRegistry(initial=[])
    assert reg.models() == ()
    assert "gpt-4o" not in reg


def test_registry_contains_and_models() -> None:
    reg = PricingRegistry()
    assert "gpt-4o" in reg
    assert "fake" not in reg
    # models() 字典序回 tuple
    models = reg.models()
    assert isinstance(models, tuple)
    assert "anila-gemma4" in models
    assert models == tuple(sorted(models))


# ---------------------------------------------------------------------------
# CostTracker — record / accumulate / by_model
# ---------------------------------------------------------------------------


def test_cost_tracker_initial_state_is_empty() -> None:
    tracker = CostTracker(PricingRegistry())
    assert tracker.total_usd == 0.0
    assert tracker.total_tokens == 0
    assert tracker.total_prompt_tokens == 0
    assert tracker.total_completion_tokens == 0
    assert tracker.total_records == 0
    assert tracker.by_model() == {}


def test_cost_tracker_record_returns_cost() -> None:
    """`record` 應回傳本次的 USD。"""
    tracker = CostTracker(PricingRegistry())
    # gpt-4o: 1000 prompt + 500 completion = 0.0025 + 0.005 = 0.0075
    cost = tracker.record("gpt-4o", prompt_tokens=1000, completion_tokens=500)
    assert cost == pytest.approx(0.0075)


def test_cost_tracker_record_accumulates_same_model() -> None:
    """同 model 多次 record 應累加 token + cost + records 計數。"""
    tracker = CostTracker(PricingRegistry())
    tracker.record("gpt-4o", prompt_tokens=1000, completion_tokens=500)
    tracker.record("gpt-4o", prompt_tokens=2000, completion_tokens=1000)
    detail = tracker.by_model()["gpt-4o"]
    assert detail["prompt_tokens"] == 3000
    assert detail["completion_tokens"] == 1500
    assert detail["total_tokens"] == 4500
    assert detail["records"] == 2
    # 0.0075 + 0.015 = 0.0225
    assert detail["cost_usd"] == pytest.approx(0.0225)


def test_cost_tracker_by_model_separates_models() -> None:
    """不同 model 各自累加,by_model 應分群正確,不會混。"""
    tracker = CostTracker(PricingRegistry())
    tracker.record("gpt-4o", prompt_tokens=1000, completion_tokens=500)
    tracker.record("claude-3-5-sonnet", prompt_tokens=2000, completion_tokens=1000)

    detail = tracker.by_model()
    assert set(detail.keys()) == {"gpt-4o", "claude-3-5-sonnet"}
    assert detail["gpt-4o"]["prompt_tokens"] == 1000
    assert detail["claude-3-5-sonnet"]["prompt_tokens"] == 2000


def test_cost_tracker_totals_across_models() -> None:
    """`total_*` properties 應跨 model 加總。"""
    tracker = CostTracker(PricingRegistry())
    # gpt-4o: 0.0025 + 0.005 = 0.0075
    tracker.record("gpt-4o", 1000, 500)
    # claude-3-5-sonnet: 2*0.003 + 1*0.015 = 0.006 + 0.015 = 0.021
    tracker.record("claude-3-5-sonnet", 2000, 1000)

    assert tracker.total_prompt_tokens == 3000
    assert tracker.total_completion_tokens == 1500
    assert tracker.total_tokens == 4500
    assert tracker.total_records == 2
    assert tracker.total_usd == pytest.approx(0.0075 + 0.021)


def test_cost_tracker_on_prem_model_is_zero_but_tokens_recorded() -> None:
    """on-prem(anila-gemma4)pricing = $0,但 token 用量仍計入。"""
    tracker = CostTracker(PricingRegistry())
    cost = tracker.record("anila-gemma4", prompt_tokens=10_000, completion_tokens=5_000)
    assert cost == 0.0
    assert tracker.total_usd == 0.0
    # token 仍記
    assert tracker.total_tokens == 15_000
    detail = tracker.by_model()["anila-gemma4"]
    assert detail["prompt_tokens"] == 10_000
    assert detail["completion_tokens"] == 5_000
    assert detail["cost_usd"] == 0.0


def test_cost_tracker_unknown_model_allow_missing_default() -> None:
    """預設 `allow_missing_pricing=True`,未知 model → $0 但記 token。"""
    tracker = CostTracker(PricingRegistry())  # 預設 allow=True
    cost = tracker.record("brand-new-model", 1000, 500)
    assert cost == 0.0
    assert tracker.by_model()["brand-new-model"]["prompt_tokens"] == 1000


def test_cost_tracker_unknown_model_strict_raises() -> None:
    """`allow_missing_pricing=False` → 未知 model 應丟 KeyError。"""
    tracker = CostTracker(PricingRegistry(), allow_missing_pricing=False)
    with pytest.raises(KeyError, match="unknown model"):
        tracker.record("brand-new-model", 1000, 500)


def test_cost_tracker_rejects_negative_tokens() -> None:
    tracker = CostTracker(PricingRegistry())
    with pytest.raises(ValueError, match=">= 0"):
        tracker.record("gpt-4o", prompt_tokens=-1, completion_tokens=0)


def test_cost_tracker_reset_clears_state() -> None:
    tracker = CostTracker(PricingRegistry())
    tracker.record("gpt-4o", 1000, 500)
    tracker.record("claude-3-5-sonnet", 2000, 1000)
    tracker.reset()
    assert tracker.total_usd == 0.0
    assert tracker.total_tokens == 0
    assert tracker.by_model() == {}


# ---------------------------------------------------------------------------
# summary_str
# ---------------------------------------------------------------------------


def test_summary_str_empty_tracker() -> None:
    """空 tracker `summary_str` 應給 sentinel 訊息。"""
    tracker = CostTracker(PricingRegistry())
    out = tracker.summary_str()
    assert "Total cost: $0.0000" in out
    assert "no LLM calls" in out


def test_summary_str_contains_usd_and_token_usage() -> None:
    """`summary_str` 應同時帶 USD + token + per-model 細項。"""
    tracker = CostTracker(PricingRegistry())
    tracker.record("gpt-4o", 1000, 500)
    tracker.record("claude-3-5-sonnet", 2000, 1000)

    out = tracker.summary_str()
    # USD 總額
    assert "USD" in out
    # token 字眼
    assert "tokens" in out
    # per-model 細項出現
    assert "gpt-4o" in out
    assert "claude-3-5-sonnet" in out
    # prompt / completion 都出現
    assert "prompt" in out
    assert "completion" in out
    # 多行
    assert out.count("\n") >= 2


def test_summary_str_lists_each_model_once() -> None:
    """每個 model 在 summary 內各出現一次(即使 record 多次)。"""
    tracker = CostTracker(PricingRegistry())
    tracker.record("gpt-4o", 1000, 500)
    tracker.record("gpt-4o", 1000, 500)  # 同 model 再來一次
    out = tracker.summary_str()
    # summary 行內 model 名稱應只在一行(per-model 細項)出現
    detail_lines = [line for line in out.splitlines() if "gpt-4o:" in line]
    assert len(detail_lines) == 1


# ---------------------------------------------------------------------------
# from_budget_tracker integration(P1-9)
# ---------------------------------------------------------------------------


def test_from_budget_tracker_converts_tokens_to_usd() -> None:
    """從 P1-9 BudgetTracker 已累計值換算成 USD。"""
    budget = BudgetTracker(max_tokens=100_000)
    budget.record(prompt_tokens=1000, completion_tokens=500)
    budget.record(prompt_tokens=2000, completion_tokens=1000)
    # 共 3000 prompt + 1500 completion
    assert budget.prompt_tokens == 3000
    assert budget.completion_tokens == 1500

    registry = PricingRegistry()
    tracker = CostTracker.from_budget_tracker(budget, "gpt-4o", registry)

    # gpt-4o: 3*0.0025 + 1.5*0.01 = 0.0075 + 0.015 = 0.0225
    assert tracker.total_usd == pytest.approx(0.0225)
    assert tracker.total_prompt_tokens == 3000
    assert tracker.total_completion_tokens == 1500
    assert tracker.total_records == 1  # 整段 budget 視為一筆換算


def test_from_budget_tracker_on_prem_zero_cost() -> None:
    """on-prem model 換算後 USD = 0,但 token 用量保留(audit 用)。"""
    budget = BudgetTracker(max_tokens=100_000)
    budget.record(prompt_tokens=5000, completion_tokens=2500)

    registry = PricingRegistry()
    tracker = CostTracker.from_budget_tracker(budget, "anila-gemma4", registry)

    assert tracker.total_usd == 0.0
    assert tracker.total_tokens == 7500


def test_from_budget_tracker_unknown_model_default_fallback() -> None:
    """未知 model 預設 `allow_missing_pricing=True` → $0 fallback。"""
    budget = BudgetTracker(max_tokens=100_000)
    budget.record(prompt_tokens=1000, completion_tokens=500)

    registry = PricingRegistry()
    tracker = CostTracker.from_budget_tracker(budget, "brand-new", registry)
    assert tracker.total_usd == 0.0
    assert tracker.total_tokens == 1500


# ---------------------------------------------------------------------------
# P0-9 tracing integration
# ---------------------------------------------------------------------------


def test_inject_span_attributes_sets_all_four_keys() -> None:
    """`inject_span_attributes` 應把 4 個 key 寫進 span.attributes。"""
    span = Span(name="llm.completion")
    inject_span_attributes(
        span,
        model="gpt-4o",
        prompt_tokens=1000,
        completion_tokens=500,
        cost_usd=0.0075,
    )
    assert span.attributes[SPAN_ATTR_MODEL] == "gpt-4o"
    assert span.attributes[SPAN_ATTR_PROMPT_TOKENS] == 1000
    assert span.attributes[SPAN_ATTR_COMPLETION_TOKENS] == 500
    assert span.attributes[SPAN_ATTR_COST_USD] == pytest.approx(0.0075)


def test_inject_span_attributes_does_not_mutate_status() -> None:
    """注入後不可動 span 的 status / error / 時間欄位。"""
    span = Span(name="llm.completion")
    original_status = span.status
    original_start = span.start_time
    inject_span_attributes(
        span,
        model="gpt-4o",
        prompt_tokens=1000,
        completion_tokens=500,
        cost_usd=0.0075,
    )
    assert span.status == original_status
    assert span.start_time == original_start
    assert span.error is None
    assert span.end_time is None


def test_inject_span_attributes_overwrites_on_re_inject() -> None:
    """同一 span 二次 inject 後者覆蓋前者。"""
    span = Span(name="llm.completion")
    inject_span_attributes(
        span, model="gpt-4o", prompt_tokens=100, completion_tokens=50, cost_usd=0.001
    )
    inject_span_attributes(
        span,
        model="claude-3-opus",
        prompt_tokens=200,
        completion_tokens=100,
        cost_usd=0.01,
    )
    assert span.attributes[SPAN_ATTR_MODEL] == "claude-3-opus"
    assert span.attributes[SPAN_ATTR_PROMPT_TOKENS] == 200
    assert span.attributes[SPAN_ATTR_COMPLETION_TOKENS] == 100
    assert span.attributes[SPAN_ATTR_COST_USD] == pytest.approx(0.01)


class _CapturingProcessor:
    """測試用 processor — 把所有 span end 事件存下來,事後檢 attributes。"""

    def __init__(self) -> None:
        self.span_ends: list[Span] = []

    def on_trace_start(self, trace: Trace) -> None:
        pass

    def on_trace_end(self, trace: Trace) -> None:
        pass

    def on_span_start(self, span: Span) -> None:
        pass

    def on_span_end(self, span: Span) -> None:
        self.span_ends.append(span)


def test_cost_tracker_with_tracer_end_to_end() -> None:
    """完整流程:Tracer span 內 record cost + inject span attribute,
    on_span_end 應拿到帶 cost attribute 的 span。"""
    tracer = Tracer()
    capture = _CapturingProcessor()
    tracer.register_processor(capture)

    registry = PricingRegistry()
    tracker = CostTracker(registry)

    with tracer.start_trace("agent.run"):
        with tracer.start_span("llm.completion") as span:
            cost = tracker.record("gpt-4o", prompt_tokens=1000, completion_tokens=500)
            inject_span_attributes(
                span,
                model="gpt-4o",
                prompt_tokens=1000,
                completion_tokens=500,
                cost_usd=cost,
            )

    assert len(capture.span_ends) == 1
    ended = capture.span_ends[0]
    assert ended.attributes[SPAN_ATTR_MODEL] == "gpt-4o"
    assert ended.attributes[SPAN_ATTR_PROMPT_TOKENS] == 1000
    assert ended.attributes[SPAN_ATTR_COMPLETION_TOKENS] == 500
    assert ended.attributes[SPAN_ATTR_COST_USD] == pytest.approx(0.0075)
    # status 流程正常
    assert ended.status == "ok"
