"""組裝可執行的 agent：模型 + ModelSettings + 工具 + 指令 + run-context。

- 單一 retrieval-first agent；工具預設只有 read-only RAG 工具。
- deny-all 政策守衛 + fail-closed 啟動守衛（P2）。
- memdir 長期記憶（ANILA_MEMORY=1）：加入 search_memory 工具、把索引常駐進指令。
- 接地引用模式（ANILA_CITED=1）：output_type=CitedAnswer + grounding output guardrail。
- output style（ANILA_OUTPUT_STYLE=<name>）：切換回應 persona。
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass

from agents import Agent, FunctionTool, OpenAIChatCompletionsModel, Tool

from anila_agent.cli.output_styles import load_output_style
from anila_agent.config import AppConfig
from anila_agent.memory.runtime import MemdirRuntime, build_memdir_runtime
from anila_agent.policy.dsl import load_policy
from anila_agent.policy.guardrail import (
    apply_policy,
    build_policy_guardrail,
    enforce_privileged_need_explicit_rules,
)
from anila_agent.prompts.builder import build_instructions
from anila_agent.retrieval.base import Retriever
from anila_agent.retrieval.from_env import select_retriever
from anila_agent.runtime.model import build_model, build_model_settings
from anila_agent.tools.capabilities import load_capabilities
from anila_agent.tools.context import AnilaRunContext
from anila_agent.tools.memory_tools import search_memory
from anila_agent.tools.rag_tools import DEFAULT_TOOLS

_TRUE = frozenset({"1", "true", "yes", "on"})
_E2E_ALLOWED_PROFILES = frozenset({"dev", "development", "test", "testing"})
_E2E_HARNESS_NAME = "gate5-silver"


def _flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in _TRUE


def validate_e2e_approval_mode() -> bool:
    """Validate the disposable Gate 5 approval switch before service startup.

    The switch is deliberately not a general-purpose runtime hook.  It may
    only be enabled by the named Gate 5 harness while the deployment profile
    is explicitly development/test.  A production process therefore rejects
    an ambient ``ANILA_E2E_REQUIRE_TOOL_APPROVAL=1`` instead of silently
    changing its tool-approval semantics.
    """

    if not _flag("ANILA_E2E_REQUIRE_TOOL_APPROVAL"):
        return False
    profile = os.getenv("ANILA_DEPLOYMENT_PROFILE", "production").strip().lower()
    harness = os.getenv("ANILA_E2E_HARNESS", "").strip()
    if profile not in _E2E_ALLOWED_PROFILES or harness != _E2E_HARNESS_NAME:
        raise ValueError(
            "ANILA_E2E_REQUIRE_TOOL_APPROVAL is test-only: "
            "requires ANILA_DEPLOYMENT_PROFILE=dev/development/test and "
            "ANILA_E2E_HARNESS=gate5-silver"
        )
    return True


def _mark_function_tools_for_approval(tools: list[Tool]) -> list[Tool]:
    """Mark only SDK ``FunctionTool`` instances as requiring approval.

    ``Tool`` is a union that also contains hosted tools without a
    ``needs_approval`` dataclass field.  Keep those entries unchanged rather
    than relying on a union-wide ``dataclasses.replace`` call, which is both
    unsafe at runtime and rejected by strict mypy.
    """

    marked: list[Tool] = []
    for tool in tools:
        if isinstance(tool, FunctionTool):
            marked.append(dataclasses.replace(tool, needs_approval=True))
        else:
            marked.append(tool)
    return marked


@dataclass
class AssembledAgent:
    """build_agent 的產物：agent + run-context + max_turns，一起傳給 Runner。"""

    agent: Agent
    context: AnilaRunContext
    max_turns: int = 10


def build_agent(
    cfg: AppConfig,
    *,
    retriever: Retriever | None = None,
    name: str | None = None,
    model: OpenAIChatCompletionsModel | None = None,
    memory_tenant: str | None = None,
    memory_requires_tenant: bool = False,
) -> AssembledAgent:
    """由設定建構 agent。``retriever`` 為 None 時依環境自動選（csp/pgvector/dummy）。

    ``model`` 可注入既有的 OpenAIChatCompletionsModel（serving 重用同一個底層 httpx
    client，避免每請求新建/洩漏）；None 時即時建一個。

    ``memory_tenant``：多租戶記憶分艙 key（部署時傳 CSP 的 X-ANILA-User-Id）。空白先被
    正規化成 None。None → 單租戶共用 store（CLI）。

    ``memory_requires_tenant``：多租戶部署（service_wrapper）設 True——此時「無可辨識
    身分（tenant 為 None）」一律**不給長期記憶**，而非退回共用 store（否則所有匿名/缺
    身分請求會共用一個 store＝跨租戶洩漏）。CLI 維持 False（單人共用 store 是對的）。
    """
    chosen = retriever if retriever is not None else select_retriever(cfg)
    # 平台契約：retriever 必須符合 Protocol（runtime_checkable）。
    if not isinstance(chosen, Retriever):
        raise TypeError(
            f"retriever 不符合 Retriever Protocol（缺 search/fetch/name/metadata）：{type(chosen).__name__}"
        )

    # memdir 長期記憶（opt-in）。多租戶時依 memory_tenant 分艙。
    # 空白身分正規化成 None；多租戶部署下無身分 → 關閉記憶（不退回共用 store）。
    tenant = (memory_tenant or "").strip() or None
    memory_on = _flag("ANILA_MEMORY") and not (memory_requires_tenant and tenant is None)
    memory: MemdirRuntime | None = build_memdir_runtime(cfg, tenant=tenant) if memory_on else None
    tool_set = [*DEFAULT_TOOLS, search_memory] if memory is not None else list(DEFAULT_TOOLS)

    # deny-all 政策：載入能力表 + 政策，fail-closed 守衛後把 guardrail 掛上每個工具。
    capabilities = load_capabilities()
    policy = load_policy(capabilities)
    enforce_privileged_need_explicit_rules(tool_set, capabilities, policy)
    tools: list[Tool] = []
    tools.extend(apply_policy(tool_set, build_policy_guardrail(policy)))
    # The disposable Gate 5 E2E harness needs one deterministic HITL edge so
    # it can exercise CSP BLOCKED -> restart -> approve/resume without adding
    # a write-capable production tool.  The switch is never enabled by the
    # normal Compose profiles; it only wraps the existing read-only tools with
    # the Agents SDK's native approval flag when explicitly requested.
    if validate_e2e_approval_mode():
        tools = _mark_function_tools_for_approval(tools)

    # 接地引用：ANILA_CITED=1 套 concise-cited output style（行內【來源：id】）。
    # 不用 SDK output_type=CitedAnswer——實測自架 reasoning 模型在「工具使用 + 結構化
    # 最終輸出」會回 markdown 而非 schema JSON。prompt 驅動的行內引用在 gpt-oss 上穩定可用。
    # CitedAnswer schema + grounding guardrail 保留為 json_schema 相容端點的可選元件。
    style_name = os.getenv("ANILA_OUTPUT_STYLE") or ("concise-cited" if _flag("ANILA_CITED") else "")
    instructions = build_instructions(
        memory_index=memory.store.index_text() if memory is not None else None,
        output_style=load_output_style(style_name),
    )

    agent = Agent(
        name=name or cfg.agent.name,
        instructions=instructions,
        model=model if model is not None else build_model(cfg.model),
        model_settings=build_model_settings(cfg.model),
        tools=tools,
    )
    return AssembledAgent(
        agent=agent,
        context=AnilaRunContext(retriever=chosen, memory=memory),
        max_turns=cfg.agent.max_turns,
    )
