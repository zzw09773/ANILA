"""Cost tracker + USD pricing — token 用量 → USD 成本估算。

本模組對應 enhancement roadmap §4.14 / P1-15,把上游 ``claude-code-src/src/cost-tracker.ts``
的「per-model usage + USD 估算」概念 port 成 Python,並擴成獨立可重用的子系統:

* **ModelPricing** — 一個 model 的單價 dataclass(prompt + completion 每千 token USD)。
* **PricingRegistry** — 預設註冊常見 model 的 pricing;支援自定 register。
* **CostTracker** — 多 model 跨輪累加 token 用量 + USD 成本,提供 per-model 細項與
  human-friendly summary。
* **integration with P1-9 BudgetTracker** — `from_budget_tracker` factory,把純 token
  tracker 的累計值一次換算成單一 model 的 USD。
* **integration with P0-9 tracing** — `inject_span_attributes` 把 cost / token 寫到當下
  span 的 attributes(對齊 OTel ``llm.*`` 命名)。
* **on-prem 模式** — ANILA on-prem 跑自家 model(vLLM / Triton)無 API 成本,pricing
  寫 $0;但仍記 token 用量(audit / 性能監控)。

設計重點
========

1. **零外部相依**:只用 std lib。pricing 表寫在程式碼裡(預設 6 個 model),caller 可
   自由 `register` 補新 model 或從 yaml 讀。

2. **不 mutate 既有 tracker / span**:`from_budget_tracker` 只 *讀* BudgetTracker 的
   累計值,自己另起一個 CostTracker;`inject_span_attributes` 只寫指定的 key,不動 span
   其他狀態。

3. **on-prem fallback**:on-prem model 找 pricing 永遠回到 $0,**不**丟例外
   (`get(allow_missing=True)`),讓「不知名 / 自家 model」也能照記 token,只是 USD = 0。

4. **immutable pricing** —``ModelPricing`` 是 `@dataclass(frozen=True)`,避免使用者
   半途偷改全域 pricing 表(test isolation / 多 thread 友善)。

5. **threading**:CostTracker 本身不加 lock;預期在「單一對話迴圈」sequence 使用,
   並發 record 不在此 task scope。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from anila_agent.core.token_budget import BudgetTracker
    from anila_agent.tracing.types import Span


# ---------------------------------------------------------------------------
# Pricing 資料型別
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    """單一 model 的定價。

    Attributes
    ----------
    model:
        Model 識別字(例如 ``"gpt-4-turbo"`` / ``"claude-3-5-sonnet"`` /
        ``"anila-gemma4"``)。
    prompt_usd_per_1k:
        輸入(prompt)token 每 1000 個的 USD 單價。on-prem model 寫 0.0。
    completion_usd_per_1k:
        輸出(completion)token 每 1000 個的 USD 單價。on-prem model 寫 0.0。

    Raises
    ------
    ValueError
        單價為負時。on-prem 用 0.0(非負)合法。
    """

    model: str
    prompt_usd_per_1k: float
    completion_usd_per_1k: float

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model name must be a non-empty string")
        if self.prompt_usd_per_1k < 0:
            raise ValueError(
                f"prompt_usd_per_1k must be >= 0, got {self.prompt_usd_per_1k}"
            )
        if self.completion_usd_per_1k < 0:
            raise ValueError(
                "completion_usd_per_1k must be >= 0, "
                f"got {self.completion_usd_per_1k}"
            )

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """算出一次 LLM call 的 USD 成本。

        ``cost = prompt_tokens / 1000 * prompt_usd_per_1k +
                 completion_tokens / 1000 * completion_usd_per_1k``

        Raises
        ------
        ValueError
            傳入負 token 數時。
        """
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError(
                "token counts must be >= 0, got "
                f"prompt_tokens={prompt_tokens}, "
                f"completion_tokens={completion_tokens}"
            )
        prompt_cost = prompt_tokens / 1000.0 * self.prompt_usd_per_1k
        completion_cost = completion_tokens / 1000.0 * self.completion_usd_per_1k
        return prompt_cost + completion_cost


# ---------------------------------------------------------------------------
# 預設 pricing 表
# ---------------------------------------------------------------------------


#: 預設 register 的 model pricing — roadmap §4.14 條列六項。
#:
#: 數字以 2025/Q1 公開定價為基準(GPT-4-Turbo / GPT-4o / Claude 3.5 Sonnet /
#: Claude 3 Opus / Gemini 1.5 Pro / ANILA on-prem)。新 model 進場請走
#: ``PricingRegistry.register(...)`` 動態擴充,避免改本 module。
DEFAULT_MODEL_PRICINGS: tuple[ModelPricing, ...] = (
    ModelPricing(
        model="gpt-4-turbo",
        prompt_usd_per_1k=0.01,
        completion_usd_per_1k=0.03,
    ),
    ModelPricing(
        model="gpt-4o",
        prompt_usd_per_1k=0.0025,
        completion_usd_per_1k=0.01,
    ),
    ModelPricing(
        model="claude-3-5-sonnet",
        prompt_usd_per_1k=0.003,
        completion_usd_per_1k=0.015,
    ),
    ModelPricing(
        model="claude-3-opus",
        prompt_usd_per_1k=0.015,
        completion_usd_per_1k=0.075,
    ),
    ModelPricing(
        model="gemini-1.5-pro",
        prompt_usd_per_1k=0.00125,
        completion_usd_per_1k=0.005,
    ),
    # ANILA on-prem:自家 vLLM 跑,無 API 成本。仍走 record 流程,USD = 0。
    ModelPricing(
        model="anila-gemma4",
        prompt_usd_per_1k=0.0,
        completion_usd_per_1k=0.0,
    ),
)


# ---------------------------------------------------------------------------
# PricingRegistry
# ---------------------------------------------------------------------------


class PricingRegistry:
    """Model name → ModelPricing 的查詢表。

    建構時預先 register `DEFAULT_MODEL_PRICINGS` 六個常見 model;caller 可呼叫
    :meth:`register` 補新 model(覆寫既有同名 entry 合法,不丟例外,方便整合測試)。

    Notes
    -----
    * 查詢用 :meth:`get` — 找不到時根據 `allow_missing` 旗標決定丟 KeyError 還是
      回 fallback(on-prem 零成本)。
    * registry 本身可 freeze 嗎? 不採 frozen — caller 可能在 runtime register 新 model
      (從 yaml 載入 / from API)。
    """

    def __init__(
        self,
        initial: Iterable[ModelPricing] | None = None,
    ) -> None:
        """建構 registry。

        Args
        ----
        initial:
            初始 pricing 清單。預設(`None`)填入 :data:`DEFAULT_MODEL_PRICINGS`;
            傳空 list 則為純空 registry(測試 / 自訂 yaml 用)。
        """
        if initial is None:
            initial = DEFAULT_MODEL_PRICINGS
        self._table: dict[str, ModelPricing] = {}
        for pricing in initial:
            self.register(pricing)

    def register(self, pricing: ModelPricing) -> None:
        """註冊 / 覆寫一個 model 的 pricing。"""
        self._table[pricing.model] = pricing

    def get(
        self,
        model_name: str,
        *,
        allow_missing: bool = False,
    ) -> ModelPricing:
        """查 model 的 pricing。

        Args
        ----
        model_name:
            Model 識別字。
        allow_missing:
            找不到時的行為。預設 False → 丟 ``KeyError``;設 True → 回一個
            $0 / $0 的 fallback(on-prem 或未知 model 用)。

        Raises
        ------
        KeyError
            `model_name` 不在 registry 且 `allow_missing=False`。
        """
        if model_name in self._table:
            return self._table[model_name]
        if allow_missing:
            return ModelPricing(
                model=model_name,
                prompt_usd_per_1k=0.0,
                completion_usd_per_1k=0.0,
            )
        raise KeyError(
            f"unknown model '{model_name}'; available: "
            f"{sorted(self._table.keys())}"
        )

    def __contains__(self, model_name: object) -> bool:
        return isinstance(model_name, str) and model_name in self._table

    def models(self) -> tuple[str, ...]:
        """已註冊 model 名稱(字典序);測試 / debug 用。"""
        return tuple(sorted(self._table.keys()))


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


@dataclass
class _ModelUsage:
    """單一 model 的累計 — CostTracker 內部用,不對外 export。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    records: int = 0

    def to_dict(self) -> dict[str, Any]:
        """轉成 plain dict 給 :meth:`CostTracker.by_model` 使用。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "cost_usd": self.cost_usd,
            "records": self.records,
        }


class CostTracker:
    """跨多 model 累加 token + USD,提供 per-model 細項與 summary。

    用法
    ----
    ```python
    registry = PricingRegistry()
    tracker = CostTracker(registry)

    tracker.record("gpt-4o", prompt_tokens=500, completion_tokens=200)
    tracker.record("claude-3-5-sonnet", prompt_tokens=1000, completion_tokens=300)

    print(tracker.total_usd)         # USD 總額
    print(tracker.by_model())        # 每 model 細項
    print(tracker.summary_str())     # human-friendly 報告
    ```

    Notes
    -----
    * `record` 同 model 多次呼叫會累加;不同 model 之間互不干擾。
    * `allow_missing_pricing=True` 時遇到未知 model 走 $0 fallback(仍記 token 用量,
      但 USD = 0)。預設 True,讓 anila on-prem 等不在預設表內的 model 不會打斷流程。
    """

    def __init__(
        self,
        pricing_registry: PricingRegistry,
        *,
        allow_missing_pricing: bool = True,
    ) -> None:
        """建構 cost tracker。

        Args
        ----
        pricing_registry:
            pricing 查詢來源。常見做法是 process 內共享一個 registry singleton。
        allow_missing_pricing:
            遇到未知 model 時是否回 $0 fallback。預設 True;設 False 會在 record
            時直接丟 KeyError(嚴格模式,for chargeback billing)。
        """
        self._pricing = pricing_registry
        self._allow_missing = allow_missing_pricing
        self._per_model: dict[str, _ModelUsage] = {}

    # ------------------------------------------------------------------ record

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """記錄一次 LLM call:累加 token 用量 + 算 USD。

        Returns
        -------
        float
            本次 call 的 USD 成本(已加進累計裡)。caller 可立即拿去寫 log / span。

        Raises
        ------
        ValueError
            token 數 < 0。
        KeyError
            `allow_missing_pricing=False` 且 model 不在 registry。
        """
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError(
                "token counts must be >= 0, got "
                f"prompt_tokens={prompt_tokens}, "
                f"completion_tokens={completion_tokens}"
            )
        pricing = self._pricing.get(model, allow_missing=self._allow_missing)
        cost = pricing.cost_usd(prompt_tokens, completion_tokens)

        usage = self._per_model.setdefault(model, _ModelUsage())
        usage.prompt_tokens += prompt_tokens
        usage.completion_tokens += completion_tokens
        usage.cost_usd += cost
        usage.records += 1
        return cost

    # ---------------------------------------------------------------- views

    @property
    def total_usd(self) -> float:
        """累計 USD 總成本(所有 model 加總)。"""
        return sum(u.cost_usd for u in self._per_model.values())

    @property
    def total_tokens(self) -> int:
        """累計 token 用量(所有 model 的 prompt + completion 加總)。"""
        return sum(
            u.prompt_tokens + u.completion_tokens
            for u in self._per_model.values()
        )

    @property
    def total_prompt_tokens(self) -> int:
        """累計 prompt(input)token 用量(所有 model 加總)。"""
        return sum(u.prompt_tokens for u in self._per_model.values())

    @property
    def total_completion_tokens(self) -> int:
        """累計 completion(output)token 用量(所有 model 加總)。"""
        return sum(u.completion_tokens for u in self._per_model.values())

    @property
    def total_records(self) -> int:
        """累計 record 輪數(所有 model 加總)。"""
        return sum(u.records for u in self._per_model.values())

    def by_model(self) -> dict[str, dict[str, Any]]:
        """每 model 的細項 dict(model_name → {prompt, completion, total, cost, records})。

        Returns
        -------
        dict[str, dict[str, Any]]
            每 model 細項 — caller 可拿去做 chargeback / 報表 / 統計。
        """
        return {model: usage.to_dict() for model, usage in self._per_model.items()}

    # ---------------------------------------------------------------- summary

    def summary_str(self) -> str:
        """Human-friendly 多行報告(總成本 + per-model 細項)。

        範例
        ----
        ```
        Total cost:   $0.0085 USD (1700 tokens, 2 calls across 2 models)
          gpt-4o:           500 prompt + 200 completion = 700 tokens, $0.0033
          claude-3-5-sonnet: 1000 prompt + 300 completion = 1300 tokens, $0.0075
        ```
        """
        if not self._per_model:
            return "Total cost: $0.0000 USD (no LLM calls recorded)"

        lines: list[str] = [
            f"Total cost: ${self.total_usd:.4f} USD "
            f"({self.total_tokens} tokens, "
            f"{self.total_records} calls across {len(self._per_model)} models)",
        ]
        for model, usage in sorted(self._per_model.items()):
            lines.append(
                f"  {model}: "
                f"{usage.prompt_tokens} prompt + {usage.completion_tokens} completion "
                f"= {usage.prompt_tokens + usage.completion_tokens} tokens, "
                f"${usage.cost_usd:.4f}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ reset

    def reset(self) -> None:
        """歸零所有 model 累計。pricing registry 不動。"""
        self._per_model.clear()

    # -------------------------------------------------------- factory: budget

    @classmethod
    def from_budget_tracker(
        cls,
        budget: BudgetTracker,
        model: str,
        pricing_registry: PricingRegistry,
        *,
        allow_missing_pricing: bool = True,
    ) -> CostTracker:
        """P1-9 BudgetTracker → CostTracker 換算工廠。

        把 budget tracker 累計的 prompt + completion tokens 一次性換算成單一 model
        的 USD,建出新的 CostTracker。

        典型情境
        --------
        對話跑到一半想知道「目前 token 用量等於多少錢」:

        ```python
        budget = BudgetTracker(max_tokens=100_000)
        # ... 跑一段對話,budget 已累積 ...
        cost = CostTracker.from_budget_tracker(budget, "gpt-4o", registry)
        print(cost.total_usd)
        ```

        Args
        ----
        budget:
            P1-9 已累計過的 BudgetTracker。
        model:
            要把 budget 用量歸給哪個 model(BudgetTracker 不分 model,假設整段對話
            單一 model)。
        pricing_registry:
            pricing 查詢來源。
        allow_missing_pricing:
            同 :class:`CostTracker` 建構參數。

        Returns
        -------
        CostTracker
            已 record 過一次的新 tracker;`total_usd` 即為換算結果。
        """
        tracker = cls(
            pricing_registry,
            allow_missing_pricing=allow_missing_pricing,
        )
        tracker.record(
            model=model,
            prompt_tokens=budget.prompt_tokens,
            completion_tokens=budget.completion_tokens,
        )
        return tracker


# ---------------------------------------------------------------------------
# P0-9 tracing integration
# ---------------------------------------------------------------------------


#: 注入到 span attributes 的標準 key 集(對齊 OTel ``llm.*`` semantic conventions
#: 的延伸 — 用 ``cost.*`` 命名空間以求清楚)。
SPAN_ATTR_COST_USD = "cost.usd"
SPAN_ATTR_PROMPT_TOKENS = "cost.prompt_tokens"
SPAN_ATTR_COMPLETION_TOKENS = "cost.completion_tokens"
SPAN_ATTR_MODEL = "cost.model"


def inject_span_attributes(
    span: Span,
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> None:
    """把一次 LLM call 的 cost / token / model 寫進 span 的 attributes。

    P0-9 tracing 端只認 ``span.attributes`` dict,直接寫四個固定 key:

    * ``cost.usd`` — 本次 call 的 USD 成本(float)
    * ``cost.prompt_tokens`` — 本次 prompt tokens(int)
    * ``cost.completion_tokens`` — 本次 completion tokens(int)
    * ``cost.model`` — 本次 model name(str)

    用法
    ----
    ```python
    with tracer.start_span("llm.completion") as span:
        response = await client.messages.create(...)
        cost = tracker.record(model, response.usage.input_tokens, response.usage.output_tokens)
        inject_span_attributes(
            span,
            model=model,
            prompt_tokens=response.usage.input_tokens,
            completion_tokens=response.usage.output_tokens,
            cost_usd=cost,
        )
    ```

    Notes
    -----
    * 不會 mutate span 的其他欄位(status / error / start_time / end_time)。
    * 同一 span 多次 inject 後寫,後者覆蓋前者 — caller 自己決定是否拆 span。
    """
    span.attributes[SPAN_ATTR_COST_USD] = cost_usd
    span.attributes[SPAN_ATTR_PROMPT_TOKENS] = prompt_tokens
    span.attributes[SPAN_ATTR_COMPLETION_TOKENS] = completion_tokens
    span.attributes[SPAN_ATTR_MODEL] = model


# ---------------------------------------------------------------------------
# 顯式對外 API list
# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_MODEL_PRICINGS",
    "SPAN_ATTR_COMPLETION_TOKENS",
    "SPAN_ATTR_COST_USD",
    "SPAN_ATTR_MODEL",
    "SPAN_ATTR_PROMPT_TOKENS",
    "CostTracker",
    "ModelPricing",
    "PricingRegistry",
    "inject_span_attributes",
]
