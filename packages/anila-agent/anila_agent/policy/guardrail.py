"""把政策 DSL 餵進單一 SDK tool-input-guardrail，並提供 fail-closed 啟動守衛。

enforcement 走 SDK 原生入口（ToolInputGuardrail），而非自製 hook runner。
被拒的工具以 ``reject_content`` 回覆（工具不執行，模型收到拒絕訊息可改採他法），
不 ``raise_exception`` 中斷整個 run——對 deny-all 姿態仍安全（工具沒跑）。
"""

from __future__ import annotations

import dataclasses

from agents import (
    FunctionTool,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
)

from anila_agent.policy.dsl import Decision, Effect, Policy
from anila_agent.tools.capabilities import Capability, capability_of
from anila_agent.tools.context import AnilaRunContext
from anila_agent.util.structured import parse_json_object


def build_policy_guardrail(policy: Policy) -> ToolInputGuardrail[AnilaRunContext]:
    """建構一個評估 policy 的 tool-input-guardrail。"""

    async def _enforce(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        tool_name = data.context.tool_name
        args = parse_json_object(data.context.tool_arguments)
        decision: Decision = policy.evaluate(tool_name, args)
        if decision.effect is Effect.ALLOW:
            return ToolGuardrailFunctionOutput.allow()
        if decision.effect is Effect.ASK:
            return ToolGuardrailFunctionOutput.reject_content(
                f"工具 {tool_name} 需人工核准（{decision.reason}）；此 agent 未啟用互動核准。",
                output_info={"tool": tool_name, "decision": "ask_user", "reason": decision.reason},
            )
        return ToolGuardrailFunctionOutput.reject_content(
            f"工具 {tool_name} 被政策拒絕（{decision.reason}）。",
            output_info={"tool": tool_name, "decision": "deny", "reason": decision.reason},
        )

    return ToolInputGuardrail(guardrail_function=_enforce, name="anila-policy")


def apply_policy(
    tools: list[FunctionTool], guardrail: ToolInputGuardrail[AnilaRunContext]
) -> list[FunctionTool]:
    """回傳掛上政策 guardrail 的新工具清單（immutable：不改原工具物件）。"""
    out: list[FunctionTool] = []
    for tool in tools:
        existing = list(getattr(tool, "tool_input_guardrails", None) or [])
        out.append(dataclasses.replace(tool, tool_input_guardrails=[guardrail, *existing]))
    return out


def enforce_privileged_need_explicit_rules(
    tools: list[FunctionTool], capabilities: dict[str, Capability], policy: Policy
) -> None:
    """fail-closed 啟動守衛：write/admin 工具必須有「明確指名」的政策規則，否則 raise。

    強迫操作者對每個特權工具有意識地表態（allow/ask/deny），不讓它只靠 default/wildcard
    悄悄被治理。read_only 工具不需明列（由 allow_read_only 涵蓋）。
    """
    for tool in tools:
        name = getattr(tool, "name", "")
        cap = capability_of(name, capabilities)
        if cap in (Capability.WRITE, Capability.ADMIN) and not policy.has_specific_rule(name):
            raise ValueError(
                f"工具 {name!r} 能力為 {cap.value}，但 configs/policy.yaml 沒有明確指名的規則。"
                f"請在 policy.yaml 為它加上 allow/ask_user/deny 規則（fail-closed 啟動守衛）。"
            )
