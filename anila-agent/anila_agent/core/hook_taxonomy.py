"""Hook 三類強型別分類 (Inspect / Decide / Transform) + 執行器。

本模組對應 enhancement roadmap P0-5:把 hook 依「能對 runtime 造成什麼影響」分成
三個彼此正交的語意層級,每層各有獨立的 Protocol / ABC,讓靜態型別檢查能在編譯期就
擋掉「拿 InspectHook 來改 payload」這種誤用。

設計重點:

* 三類各有獨立 Protocol,不共用 base class,以利靜態型別檢查與 IDE 提示。
* `HookExecutor` 強制執行順序:**Inspect -> Decide -> Transform**;這個順序是 sound
  的——觀察必先於決策,決策必先於轉換,避免「DENY 後仍執行 transform」這種浪費或副作用。
* DecideHook chain 內,任一 hook 回 `DENY` 就會 **early-exit**,後續 hook 不再呼叫。
  `SKIP` 代表「本 hook 沒意見,請下一個 hook 接手」。
* TransformHook 是 **monadic chain**:前一個 transform 的 output 直接餵給下一個 transform,
  整條 chain 共用同一個 generic input/output 型別參數。
* 本模組故意不依賴 `openai-agents` / `antigravity`,只用 stdlib + `typing.Protocol`,
  以保持單元可測。

與 P0-1 lifecycle hooks (`anila_agent.core.hooks`) 的關係:

* P0-1 是 **lifecycle layer** —— 描述「什麼時候」呼叫 (on_agent_start / pre_tool_use / ...)。
* P0-5 是 **semantic layer** —— 描述「呼叫進來後能做什麼影響」(觀察 / 決策 / 轉換)。
* 兩者是 **正交維度**,後續 P1 才會做整合:同一個 lifecycle 點上,可註冊任一語意類別的 hook。
* 目前 P0-5 是 standalone module,先給 Policy DSL (P0-7) 與 workspace_only (P0-8) 當基礎。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import (
    Generic,
    Iterable,
    Protocol,
    TypeVar,
    runtime_checkable,
)

# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


class DecisionVerdict(str, enum.Enum):
    """`Decision.verdict` 的列舉值。

    * `ALLOW` —— 允許繼續執行;chain 內遇到 ALLOW 後仍會繼續詢問下一個 DecideHook
      (因為後面可能還有 DENY)。
    * `DENY` —— 拒絕執行;chain 立即停止 (early-exit),整條 pipeline 不再執行 Transform。
    * `SKIP` —— 本 hook 沒意見,交由下一個 DecideHook 決定;若整條 chain 全部 SKIP,
      `HookExecutor.run_decide` 回的最終結果視為 ALLOW (預設放行)。
    """

    ALLOW = "allow"
    DENY = "deny"
    SKIP = "skip"


@dataclass(frozen=True)
class Decision:
    """DecideHook 的回傳型別。

    `Decision` 是 immutable 的——一旦做出決策就不再變更,避免 hook chain 內
    被後續 hook 偷偷改寫 verdict 卻沒人發現。

    Attributes:
        verdict: 三選一決策值,見 `DecisionVerdict`。
        reason: 可選的人類可讀說明,DENY 時建議務必填,以利稽核 / debug。
        source: 可選的 hook 識別字 (例如 hook class name),方便追蹤是「誰」做的決策。
    """

    verdict: DecisionVerdict
    reason: str = ""
    source: str = ""

    @classmethod
    def allow(cls, reason: str = "", source: str = "") -> "Decision":
        """便捷建構子:回傳 ALLOW 決策。"""
        return cls(verdict=DecisionVerdict.ALLOW, reason=reason, source=source)

    @classmethod
    def deny(cls, reason: str = "", source: str = "") -> "Decision":
        """便捷建構子:回傳 DENY 決策 (建議務必帶 reason)。"""
        return cls(verdict=DecisionVerdict.DENY, reason=reason, source=source)

    @classmethod
    def skip(cls, source: str = "") -> "Decision":
        """便捷建構子:回傳 SKIP 決策,讓下一個 hook 接手。"""
        return cls(verdict=DecisionVerdict.SKIP, reason="", source=source)

    @property
    def is_allow(self) -> bool:
        """是否為 ALLOW。"""
        return self.verdict is DecisionVerdict.ALLOW

    @property
    def is_deny(self) -> bool:
        """是否為 DENY (chain 應 early-exit)。"""
        return self.verdict is DecisionVerdict.DENY

    @property
    def is_skip(self) -> bool:
        """是否為 SKIP (本 hook 沒意見)。"""
        return self.verdict is DecisionVerdict.SKIP


# ---------------------------------------------------------------------------
# 三類 Hook Protocol
# ---------------------------------------------------------------------------


# `Ctx` 是 hook context 型別參數,刻意做成 generic 而不綁死成某個具體 class,
# 讓使用者可帶入自己的 `AnilaToolContext` 或測試用的 fake context。
Ctx = TypeVar("Ctx")

# Inspect / Decide 的 payload 型別參數 (read-only)。
T = TypeVar("T")

# Transform 的輸入 / 輸出型別參數;允許 input 與 output 不同型,例如
# `TransformHook[Exception, str]` (把 exception 轉成 error message)。
InT = TypeVar("InT")
OutT = TypeVar("OutT")


@runtime_checkable
class InspectHook(Protocol[Ctx, T]):
    """**觀察類** hook —— pure read-only,只能 log / metrics / audit。

    與另兩類的差別:

    * 與 `DecideHook` 的差別:`InspectHook` **不能阻斷** runtime 流程,
      也不會被 `HookExecutor` 拿來判斷後續要不要繼續執行。
    * 與 `TransformHook` 的差別:`InspectHook` **不能修改 payload**,
      回傳值固定為 `None`,即使 hook 內部誤改了 payload,也不會被下游採用。

    典型使用場景:metrics、structured logging、audit trail、UI notification。
    """

    def inspect(self, ctx: Ctx, payload: T) -> None:
        """觀察 payload,不得 mutate,不得回傳影響 runtime 的值。"""
        ...


@runtime_checkable
class DecideHook(Protocol[Ctx, T]):
    """**決策類** hook —— read + return ALLOW / DENY / SKIP。

    與另兩類的差別:

    * 與 `InspectHook` 的差別:`DecideHook` **可以阻斷** runtime 流程
      (回 `DENY` 就讓 chain early-exit、不執行 Transform)。
    * 與 `TransformHook` 的差別:`DecideHook` **不能修改 payload**,
      只能對「下一步要不要做」表達意見。

    典型使用場景:Policy DSL、permission check、workspace_only 邊界保護、
    confirm_destructive_tools。
    """

    def decide(self, ctx: Ctx, payload: T) -> Decision:
        """檢查 payload 並回傳 `Decision`。

        實作 contract:

        * 不得 mutate `payload`。
        * 回 `Decision.deny(...)` 會立即終止整條 chain,Transform 不會執行。
        * 回 `Decision.skip()` 表示「沒意見」,由下一個 hook 接手。
        * 回 `Decision.allow(...)` 表示本 hook 通過,後續 hook 仍會被呼叫。
        """
        ...


@runtime_checkable
class TransformHook(Protocol[Ctx, InT, OutT]):
    """**轉換類** hook —— read + mutate payload (monadic)。

    與另兩類的差別:

    * 與 `InspectHook` 的差別:`TransformHook` **可以也應該** 回傳新的 payload。
    * 與 `DecideHook` 的差別:`TransformHook` 不負責「要不要做」的決策
      (那是 Decide 的職責),只負責「用什麼 input 做」。

    Monadic 串接:同一個 chain 內,前一個 transform 的 `OutT` 必須等於下一個 transform
    的 `InT`,`HookExecutor.run_transform` 會依序串接。

    典型使用場景:on_tool_error 把 exception 轉成 error string 給 model 看、
    on_interaction 把 ask_question spec 轉成 user 的真實回答。
    """

    def transform(self, ctx: Ctx, payload: InT) -> OutT:
        """讀取 `payload` 並回傳轉換後的新 payload。

        實作 contract:

        * 應視 `payload` 為 immutable,避免 in-place mutation 造成隱藏的 side effect;
          需要修改時建議用 dataclass `replace` / dict copy 等方式產生新物件。
        * 若 transform 失敗,直接拋 exception (由上層 pipeline 處理),不要回 `None`
          當作「沒事發生」的訊號——`None` 是合法的 `OutT`。
        """
        ...


# ---------------------------------------------------------------------------
# HookExecutor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineResult(Generic[OutT]):
    """`HookExecutor.run_pipeline` 的回傳結構。

    Attributes:
        decision: 最終 `Decision` (若 chain 全 SKIP 則為 ALLOW)。
        payload: Transform chain 跑完後的 payload;若 decision 是 DENY,
            則為 `None` (因為 Transform 從未被執行)。
        transform_executed: 是否實際執行過 Transform chain。
    """

    decision: Decision
    payload: OutT | None
    transform_executed: bool = False


class HookExecutor:
    """三類 hook 的執行器,強制 `Inspect -> Decide -> Transform` 的執行順序。

    為什麼要強制順序?

    1. **觀察必先於決策** —— 若決策邏輯依賴某些 metric,Inspect 必須先寫進 ctx。
    2. **決策必先於轉換** —— 若 DENY,Transform 根本不該執行 (浪費、可能有 side effect)。
    3. **Transform 是 monadic chain** —— 前一個 transform 的 output 是下一個的 input,
       chain 內任一 transform 拋例外則整條 pipeline 中止。

    本類為純函式 stateless,可放心多執行緒共用 (前提是 hook 本身 thread-safe)。
    """

    # ---- 單類執行 -------------------------------------------------------

    @staticmethod
    def run_inspect(
        hooks: Iterable[InspectHook[Ctx, T]],
        ctx: Ctx,
        payload: T,
    ) -> None:
        """依序執行所有 InspectHook,皆為 fire-and-forget,**任何一個拋例外都會中斷後續**。

        設計考量:Inspect 雖然不影響 runtime 行為,但如果某個 metric exporter 壞了,
        應立即可見 (fail-fast) 而不是吞掉。若呼叫端希望「壞掉的 inspect 不影響其他」,
        請在 hook 內部自行 try/except。
        """
        for hook in hooks:
            hook.inspect(ctx, payload)

    @staticmethod
    def run_decide(
        hooks: Iterable[DecideHook[Ctx, T]],
        ctx: Ctx,
        payload: T,
    ) -> Decision:
        """依序執行所有 DecideHook,遇到 DENY 立即 early-exit。

        決策合成規則:

        * 任一 hook 回 `DENY` —— 立即回傳該 `Decision`,後續 hook 不執行。
        * 全部 hook 回 `SKIP` —— 回傳 `Decision.allow(reason="all_skip")` (預設放行)。
        * 至少一個 ALLOW、其他混 SKIP —— 回傳「最後一個 ALLOW」的 Decision,
          保留它的 reason / source 以利追蹤。
        """
        last_allow: Decision | None = None
        for hook in hooks:
            decision = hook.decide(ctx, payload)
            if decision.is_deny:
                return decision
            if decision.is_allow:
                last_allow = decision
            # SKIP -> 不動 last_allow,繼續詢問下一個

        if last_allow is not None:
            return last_allow
        return Decision.allow(reason="all_skip")

    @staticmethod
    def run_transform(
        hooks: Iterable[TransformHook[Ctx, InT, InT]],
        ctx: Ctx,
        payload: InT,
    ) -> InT:
        """依序執行 TransformHook chain,前一個的 output 餵給下一個。

        注意型別簽章:`TransformHook[Ctx, InT, InT]` —— 同一條 chain 內所有
        transform 必須有相同的 input/output 型別,否則 mypy 會擋下。若需要
        type-changing chain,呼叫端應拆成多次 `run_transform`。
        """
        current = payload
        for hook in hooks:
            current = hook.transform(ctx, current)
        return current

    # ---- 完整 pipeline --------------------------------------------------

    @classmethod
    def run_pipeline(
        cls,
        ctx: Ctx,
        payload: InT,
        *,
        inspect_hooks: Iterable[InspectHook[Ctx, InT]] = (),
        decide_hooks: Iterable[DecideHook[Ctx, InT]] = (),
        transform_hooks: Iterable[TransformHook[Ctx, InT, InT]] = (),
    ) -> PipelineResult[InT]:
        """跑完整 pipeline:Inspect 全跑 -> Decide -> 若 ALLOW 才 Transform。

        執行步驟:

        1. Inspect chain 全跑 (即便後續會 DENY,觀察仍要完整以便 audit)。
        2. Decide chain 跑:遇 DENY 立即停,回 `PipelineResult(decision, payload=None,
           transform_executed=False)`。
        3. 若 Decide 最終 ALLOW,才執行 Transform chain,回傳轉換後的 payload。
        """
        # Step 1:Inspect 全跑
        cls.run_inspect(inspect_hooks, ctx, payload)

        # Step 2:Decide
        decision = cls.run_decide(decide_hooks, ctx, payload)
        if decision.is_deny:
            return PipelineResult(
                decision=decision,
                payload=None,
                transform_executed=False,
            )

        # Step 3:Transform (僅 ALLOW 才執行)
        final_payload = cls.run_transform(transform_hooks, ctx, payload)
        return PipelineResult(
            decision=decision,
            payload=final_payload,
            transform_executed=True,
        )


__all__ = [
    "Decision",
    "DecisionVerdict",
    "InspectHook",
    "DecideHook",
    "TransformHook",
    "HookExecutor",
    "PipelineResult",
]
