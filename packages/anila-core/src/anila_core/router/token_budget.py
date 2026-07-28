"""Router inference token budgeting driven by the deployed model's真實容量。

為什麼需要這一層
----------------
Router 原本呼叫主模型時**完全不宣告 ``max_tokens``**,把輸出上限整包交給上游
推論伺服器的預設值。這在 llama.cpp / vLLM 這類「context 由 ``-c`` 除以
``--parallel`` 平分到每個 slot」的部署下會直接爆掉:實測以
``-c 8192 --parallel 8`` 起 llama-server 時每個 slot 只有 1024 tokens,使用者
貼一份論文進來,路由決策 100% 失敗,而使用者只看到「目前無法安全判斷是否需要
Agent」——完全無法分辨是模型拒答還是 token 不夠。

所以真正的缺陷不是「reasoning 模型不能當 router」,而是平台沒有把
``model_registry.context_window``(部署時登記的真實容量)納入預算計算。這個模組
就是那條缺掉的線:

1. 由 CSP 治理投影取得目標模型的 ``context_window``(受信任事實,不是猜的)。
2. 用**既有**的 :mod:`anila_core.compact.auto_compact` 算 effective context /
   compact 門檻——不另寫一套公式。
3. 為 reasoning 模型**額外保留思考空間**(實測 gemma 光是路由決策就吃掉 359
   tokens 的 thinking),保留量可由環境變數調整,不寫死魔數。
4. 預算不夠時拋出**可辨識**的例外,讓上層把「token 預算不足」跟「模型拒答」講
   清楚,使用者才知道該縮短輸入還是換模型。

``context_window`` 沒登記時的取捨
---------------------------------
兩個都不能選:靜默套一個猜測值 → 重演「無聲失敗」(猜大了照樣撞牆、猜小了白白
砍掉可用容量);直接讓功能不可用 → 現存部署的 registry 幾乎都沒填這欄,等於一
升級就全站掛掉。

採用的行為是**「維持現況 + 大聲說出來」**:

* **不宣告** ``max_tokens``(退回改動前的行為,上游預設值照舊生效)——寧可什麼
  都不說,也不要說一個編造的數字。
* **不做輸入長度拒絕**——不知道上限就沒有拒絕的正當性,fail-safe 方向是放行而
  非誤殺。
* 但回傳 ``reason_code``(:data:`REASON_CONTEXT_WINDOW_UNDECLARED`)並讓呼叫端
  記一次 warning,讓「這個模型沒填 context_window」變成**可觀測的事實**而不是
  一個要靠猜的謎題。

也就是說:能力宣告缺失只會退化成「跟以前一樣」,不會比以前更糟,而且會留下明確
的診斷線索。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..compact.auto_compact import (
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    get_auto_compact_threshold,
    get_effective_context_window,
    rough_token_count,
    should_compact,
)
from ..models.message import UserMessage


logger = logging.getLogger(__name__)


# ── Reason codes(上層據此產生可診斷的使用者訊息)────────────────────────────

#: 估算輸入超過模型真實容量 → 這是 token 預算問題,不是模型拒答。
REASON_INPUT_EXCEEDS_CONTEXT = "ROUTER_INPUT_EXCEEDS_MODEL_CONTEXT"

#: 模型在 registry 沒登記 ``context_window`` → 退回不宣告 max_tokens(見模組
#: docstring 的取捨說明)。
REASON_CONTEXT_WINDOW_UNDECLARED = "ROUTER_MODEL_CONTEXT_WINDOW_UNDECLARED"

#: registry 的 ``context_window`` 值本身無效(非正整數)→ 一律不當成硬上限用。
REASON_CONTEXT_WINDOW_INVALID = "ROUTER_MODEL_CONTEXT_WINDOW_INVALID"


# ── 可設定的預算旗標(預設值都有實測或結構上的理由,不是隨手挑的)──────────

#: reasoning 模型的思考空間。實測 gemma 光是輸出一個 ``{"decision":...}`` 路由
#: 決策就用掉 359 tokens 的 thinking;1024 給約 3 倍餘裕,同時對 8192 這種小
#: context 也還撐得住(1024 + 256 = 1280,只佔 8192 的 15.6%)。
DEFAULT_REASONING_RESERVE_TOKENS = 1024

#: 最低可用回答空間。低於這個數字連一個完整的 RouteDecision JSON 或一句可讀的
#: 拒答理由都放不下,寧可先擋下來並說清楚原因。
DEFAULT_MIN_ANSWER_TOKENS = 256

#: 回答空間上限。Router 的輸出是結構化決策或一段直答,不需要吃掉整個 window;
#: 封頂可以把省下來的容量還給輸入(使用者貼的論文)。
DEFAULT_MAX_ANSWER_TOKENS = 4096

#: 估算誤差緩衝。這裡用的是 auto_compact 的字元數近似(非真 tokenizer),CJK 與
#: 程式碼會低估,留一段固定緩衝避免剛好壓線。
DEFAULT_ESTIMATE_MARGIN_TOKENS = 256


_ENV_REASONING_RESERVE = "ANILA_ROUTER_REASONING_RESERVE_TOKENS"
_ENV_MIN_ANSWER = "ANILA_ROUTER_MIN_ANSWER_TOKENS"
_ENV_MAX_ANSWER = "ANILA_ROUTER_MAX_ANSWER_TOKENS"
_ENV_ESTIMATE_MARGIN = "ANILA_ROUTER_TOKEN_ESTIMATE_MARGIN"


class RouterTokenBudgetError(RuntimeError):
    """Router token 預算相關的可辨識錯誤基底。"""

    reason_code = "ROUTER_TOKEN_BUDGET_ERROR"


class RouterInputExceedsModelContext(RouterTokenBudgetError):
    """輸入超出模型真實容量 —— 明確是 token 預算問題,不是模型拒答。

    訊息刻意帶上四個數字(容量 / 估算輸入 / 需保留 / 可用),因為使用者要靠它
    決定「縮短輸入」還是「換一個 context_window 更大的模型」。只寫一句
    「無法判斷」等於什麼都沒說。
    """

    reason_code = REASON_INPUT_EXCEEDS_CONTEXT

    def __init__(
        self,
        *,
        context_window: int,
        input_tokens: int,
        reserved_output_tokens: int,
        input_ceiling: int,
    ) -> None:
        self.context_window = context_window
        self.input_tokens = input_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.input_ceiling = input_ceiling
        super().__init__(
            "輸入超出模型 token 預算:模型 context_window="
            f"{context_window} tokens,估算輸入約 {input_tokens} tokens,"
            f"另需保留 {reserved_output_tokens} tokens 給思考與回答,"
            f"因此輸入上限為 {input_ceiling} tokens。"
            "請縮短輸入內容,或改用 context_window 更大的模型。"
        )


@dataclass(frozen=True, slots=True)
class RouterTokenBudgetPolicy:
    """Router 輸出預算的可設定旗標。

    所有欄位都可用環境變數覆寫(見 :meth:`from_env`),因此部署端換模型或改
    llama-server 的 ``-c`` / ``--parallel`` 配置時不需要改碼。
    """

    reasoning_reserve_tokens: int = DEFAULT_REASONING_RESERVE_TOKENS
    min_answer_tokens: int = DEFAULT_MIN_ANSWER_TOKENS
    max_answer_tokens: int = DEFAULT_MAX_ANSWER_TOKENS
    estimate_margin_tokens: int = DEFAULT_ESTIMATE_MARGIN_TOKENS

    def __post_init__(self) -> None:
        for name in (
            "reasoning_reserve_tokens",
            "min_answer_tokens",
            "max_answer_tokens",
            "estimate_margin_tokens",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} 必須是非負整數")
        if self.min_answer_tokens < 1:
            raise ValueError("min_answer_tokens 必須為正整數")
        if self.max_answer_tokens < self.min_answer_tokens:
            raise ValueError("max_answer_tokens 不得小於 min_answer_tokens")
        # auto_compact.get_effective_context_window() 內部會把 reserve 夾在
        # MAX_OUTPUT_TOKENS_FOR_SUMMARY 以下。若這裡的保留量超過那個上限,
        # effective window 會比我們以為的大 → 實際上「少保留了」。與其默默地
        # 被夾掉,不如在建構期就講明白衝突在哪。
        if self.reserved_output_tokens > MAX_OUTPUT_TOKENS_FOR_SUMMARY:
            raise ValueError(
                "reasoning_reserve_tokens + min_answer_tokens 不得超過 auto_compact "
                f"的 MAX_OUTPUT_TOKENS_FOR_SUMMARY({MAX_OUTPUT_TOKENS_FOR_SUMMARY})"
            )

    @property
    def reserved_output_tokens(self) -> int:
        """必須留給模型的最小輸出空間(思考 + 最短可用回答)。"""

        return self.reasoning_reserve_tokens + self.min_answer_tokens

    @property
    def desired_output_tokens(self) -> int:
        """容量充裕時願意宣告的輸出上限(思考 + 完整回答)。"""

        return self.reasoning_reserve_tokens + self.max_answer_tokens

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> "RouterTokenBudgetPolicy":
        """由環境變數建構;無效值退回預設並記 warning(絕不因設定打錯而崩潰)。"""

        source = os.environ if env is None else env

        def _read(key: str, default: int) -> int:
            raw = source.get(key)
            if raw is None or not str(raw).strip():
                return default
            try:
                parsed = int(str(raw).strip())
            except (TypeError, ValueError):
                logger.warning("%s 不是整數(%r),改用預設 %d", key, raw, default)
                return default
            if parsed < 0:
                logger.warning("%s 不得為負(%r),改用預設 %d", key, raw, default)
                return default
            return parsed

        try:
            return cls(
                reasoning_reserve_tokens=_read(
                    _ENV_REASONING_RESERVE, DEFAULT_REASONING_RESERVE_TOKENS
                ),
                min_answer_tokens=_read(_ENV_MIN_ANSWER, DEFAULT_MIN_ANSWER_TOKENS),
                max_answer_tokens=_read(_ENV_MAX_ANSWER, DEFAULT_MAX_ANSWER_TOKENS),
                estimate_margin_tokens=_read(
                    _ENV_ESTIMATE_MARGIN, DEFAULT_ESTIMATE_MARGIN_TOKENS
                ),
            )
        except ValueError as exc:
            logger.warning("Router token 預算環境設定無效(%s),全部改用預設", exc)
            return cls()


@dataclass(frozen=True, slots=True)
class RouterTokenBudget:
    """一次 Router 推論呼叫的 token 預算結論。

    ``max_tokens is None`` 表示**刻意不宣告**(模型未登記容量,見模組
    docstring),呼叫端此時必須把 ``max_tokens`` 整個從 payload 省略,而不是塞
    一個猜測值。
    """

    context_window: int | None
    input_tokens: int
    max_tokens: int | None
    reasoning_reserve_tokens: int
    reserved_output_tokens: int
    input_ceiling: int | None
    auto_compact_threshold: int | None
    compact_recommended: bool
    reason_code: str | None

    @property
    def declares_max_tokens(self) -> bool:
        return self.max_tokens is not None


def _wire_message_for_estimator(message: object) -> UserMessage:
    """把 OpenAI wire 格式的 message 轉成 auto_compact 估算器吃的形狀。

    role 對 :func:`rough_token_count` 的字元計數沒有影響,所以一律裝成
    ``UserMessage``。``content`` 以外的欄位(``tool_calls``、``name`` …)照樣
    會被上游計費,序列化成一個 text block 一起算進去,避免低估。
    """

    if not isinstance(message, Mapping):
        return UserMessage(content=[{"type": "text", "text": str(message)}])

    blocks: list[dict[str, object]] = []
    content = message.get("content")
    if isinstance(content, str):
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        for part in content:
            if isinstance(part, Mapping):
                blocks.append(dict(part))
            else:
                blocks.append({"type": "text", "text": str(part)})
    elif content is not None:
        blocks.append({"type": "text", "text": str(content)})

    overhead = {key: value for key, value in message.items() if key != "content"}
    if overhead:
        try:
            serialized = json.dumps(overhead, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = str(overhead)
        blocks.append({"type": "text", "text": serialized})
    return UserMessage(content=blocks)


def estimate_wire_input_tokens(messages: object) -> int:
    """估算一批 OpenAI wire messages 的輸入 tokens。

    直接複用 :func:`anila_core.compact.auto_compact.rough_token_count`,所以
    Router 的預算與 auto_compact 的門檻用的是同一把尺。
    """

    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return 0
    return rough_token_count(
        [_wire_message_for_estimator(item) for item in messages]
    )


def normalize_context_window(value: object) -> tuple[int | None, str | None]:
    """把 registry 的 ``context_window`` 正規化成 ``(容量, reason_code)``。

    未登記 → ``(None, REASON_CONTEXT_WINDOW_UNDECLARED)``;
    登記了但不是正整數 → ``(None, REASON_CONTEXT_WINDOW_INVALID)``。
    兩種情況都不會被當成硬上限使用(fail-safe:不猜、不誤殺)。
    """

    if value is None:
        return None, REASON_CONTEXT_WINDOW_UNDECLARED
    if isinstance(value, bool) or not isinstance(value, int):
        return None, REASON_CONTEXT_WINDOW_INVALID
    if value <= 0:
        return None, REASON_CONTEXT_WINDOW_INVALID
    return value, None


def plan_router_token_budget(
    *,
    messages: object,
    context_window: object,
    policy: RouterTokenBudgetPolicy | None = None,
) -> RouterTokenBudget:
    """依部署模型的真實容量算出這次呼叫的 token 預算。

    Raises:
        RouterInputExceedsModelContext: 估算輸入已吃掉模型容量,連最小輸出空間
            都留不下來。這條路徑刻意**不**降級成通用錯誤,因為使用者要能分辨
            「該縮短輸入 / 換模型」跟「模型拒答」。
    """

    effective_policy = policy or RouterTokenBudgetPolicy()
    input_tokens = estimate_wire_input_tokens(messages)
    window, reason_code = normalize_context_window(context_window)

    if window is None:
        # Fail-safe:不宣告 max_tokens、不拒絕輸入,但把 reason_code 帶出去。
        return RouterTokenBudget(
            context_window=None,
            input_tokens=input_tokens,
            max_tokens=None,
            reasoning_reserve_tokens=effective_policy.reasoning_reserve_tokens,
            reserved_output_tokens=effective_policy.reserved_output_tokens,
            input_ceiling=None,
            auto_compact_threshold=None,
            compact_recommended=False,
            reason_code=reason_code,
        )

    reserved = effective_policy.reserved_output_tokens
    # 這一行就是接上 auto_compact 的那條線:effective context = 真實容量減掉
    # 保留給輸出(思考 + 回答)的空間,公式只存在於 auto_compact 一處。
    effective = get_effective_context_window(window, max_output_tokens=reserved)
    input_ceiling = effective - effective_policy.estimate_margin_tokens
    threshold = get_auto_compact_threshold(window, max_output_tokens=reserved)
    # 注意:auto_compact 的 buffer 是為 20 萬級 window 訂的常數,對 8192 這類
    # 小 window 會讓門檻掉到負值 → compact_recommended 恆為 True。那並不算錯
    # (這種 window 本來就一直貼著懸崖),而且它只是 advisory metadata,永遠
    # 不會用來擋呼叫;真正的硬判斷用上面的 input_ceiling。
    compact_recommended = should_compact(
        window, input_tokens, max_output_tokens=reserved
    )

    if input_tokens > input_ceiling:
        raise RouterInputExceedsModelContext(
            context_window=window,
            input_tokens=input_tokens,
            reserved_output_tokens=reserved,
            input_ceiling=max(0, input_ceiling),
        )

    remaining = window - input_tokens - effective_policy.estimate_margin_tokens
    max_tokens = min(effective_policy.desired_output_tokens, remaining)
    return RouterTokenBudget(
        context_window=window,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        reasoning_reserve_tokens=effective_policy.reasoning_reserve_tokens,
        reserved_output_tokens=reserved,
        input_ceiling=input_ceiling,
        auto_compact_threshold=threshold,
        compact_recommended=compact_recommended,
        reason_code=None,
    )


__all__ = [
    "DEFAULT_ESTIMATE_MARGIN_TOKENS",
    "DEFAULT_MAX_ANSWER_TOKENS",
    "DEFAULT_MIN_ANSWER_TOKENS",
    "DEFAULT_REASONING_RESERVE_TOKENS",
    "REASON_CONTEXT_WINDOW_INVALID",
    "REASON_CONTEXT_WINDOW_UNDECLARED",
    "REASON_INPUT_EXCEEDS_CONTEXT",
    "RouterInputExceedsModelContext",
    "RouterTokenBudget",
    "RouterTokenBudgetError",
    "RouterTokenBudgetPolicy",
    "estimate_wire_input_tokens",
    "normalize_context_window",
    "plan_router_token_budget",
]
