"""Unit tests:P1-19 「DENY vs DISABLE 二維工具控管」。

涵蓋面
======

* :meth:`PolicyEngine.filter_tool_descriptions`
  - ``DISABLE`` rule 命中 → tool 被移除。
  - ``DENY``    rule 命中 → tool 保留(LLM 仍看得到,call 時才擋)。
  - ``ALLOW``   rule 命中 → tool 保留。
  - 沒 rule match(default open) → tool 保留。
* 支援多種 tool 形態:
  - 有 ``.name`` 屬性的 ``ToolDescriptor``。
  - dict (``{"name": ..., "description": ...}``)。
  - 任意 namedtuple-like 物件。
* :func:`apply_policy_to_system_context` 整合 P1-10
  :class:`SystemContextBuilder`,確 disabled tool 不出現在 system prompt。
* YAML config 範例:同時定義 DISABLE + DENY rule,行為正確。
* docstring example 仍可執行(doc test 兼容)。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from anila_agent.core.policy import (
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    apply_policy_to_system_context,
)
from anila_agent.prompts import SystemContextBuilder, ToolDescriptor

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tool_list() -> list[ToolDescriptor]:
    """代表性 tool list:含 admin tool / shell tool / read tool。"""
    return [
        ToolDescriptor(
            name="admin_panel",
            description="Admin-only panel for managing users.",
        ),
        ToolDescriptor(
            name="shell.run",
            description="Run a shell command.",
        ),
        ToolDescriptor(
            name="read_document",
            description="Read a document by id.",
        ),
    ]


@pytest.fixture
def disable_admin_engine() -> PolicyEngine:
    """engine:把 admin_panel DISABLE,LLM 看不到。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="hide_admin",
            tool_pattern="admin_panel",
            effect=PolicyEffect.DISABLE,
            priority=1000,
            reason="admin tools hidden in user session",
        )
    )
    return engine


@pytest.fixture
def deny_shell_engine() -> PolicyEngine:
    """engine:把 shell.run DENY(可見但 call 會擋)。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="deny_shell",
            tool_pattern="shell.run",
            effect=PolicyEffect.DENY,
            priority=900,
            reason="shell execution denied in read-only mode",
        )
    )
    return engine


@pytest.fixture
def mixed_engine() -> PolicyEngine:
    """engine:admin DISABLE + shell DENY 同時存在,典型 prod-internal 配置。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="hide_admin",
            tool_pattern="admin_panel",
            effect=PolicyEffect.DISABLE,
            priority=1000,
        )
    )
    engine.add_rule(
        PolicyRule(
            name="deny_shell",
            tool_pattern="shell.run",
            effect=PolicyEffect.DENY,
            priority=900,
        )
    )
    return engine


# ---------------------------------------------------------------------------
# PolicyEngine.filter_tool_descriptions
# ---------------------------------------------------------------------------


def test_filter_removes_disabled_tool(
    disable_admin_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """DISABLE 命中的 tool 應被移除。"""
    visible = disable_admin_engine.filter_tool_descriptions(tool_list, ctx=None)
    visible_names = [t.name for t in visible]

    assert "admin_panel" not in visible_names
    assert "shell.run" in visible_names
    assert "read_document" in visible_names


def test_filter_keeps_denied_tool(
    deny_shell_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """DENY 命中的 tool 應**保留**(LLM 仍要看到,call 時才擋)。"""
    visible = deny_shell_engine.filter_tool_descriptions(tool_list, ctx=None)
    visible_names = [t.name for t in visible]

    assert "shell.run" in visible_names, "DENY 不該把 tool 從 prompt 移除"
    assert "admin_panel" in visible_names
    assert "read_document" in visible_names


def test_filter_keeps_allowed_tool(
    tool_list: list[ToolDescriptor],
) -> None:
    """顯式 ALLOW rule 命中應保留。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="allow_all",
            tool_pattern="*",
            effect=PolicyEffect.ALLOW,
            priority=1000,
        )
    )
    visible = engine.filter_tool_descriptions(tool_list, ctx=None)
    assert len(visible) == len(tool_list)


def test_filter_default_open_keeps_unmatched(
    tool_list: list[ToolDescriptor],
) -> None:
    """沒任何 rule match → default ALLOW,tool 保留。"""
    engine = PolicyEngine()  # 完全空
    visible = engine.filter_tool_descriptions(tool_list, ctx=None)
    assert len(visible) == len(tool_list)


def test_filter_mixed_disable_and_deny(
    mixed_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """DISABLE + DENY 並存:DISABLE 那個被移、DENY 那個留。"""
    visible = mixed_engine.filter_tool_descriptions(tool_list, ctx=None)
    visible_names = [t.name for t in visible]

    assert "admin_panel" not in visible_names  # DISABLE → 移除
    assert "shell.run" in visible_names         # DENY → 保留
    assert "read_document" in visible_names


def test_filter_supports_dict_tool(
    disable_admin_engine: PolicyEngine,
) -> None:
    """tool 形態為 dict({'name': ..., 'description': ...})也吃。"""
    dict_tools: list[dict[str, Any]] = [
        {"name": "admin_panel", "description": "admin"},
        {"name": "read_document", "description": "read"},
    ]
    visible = disable_admin_engine.filter_tool_descriptions(dict_tools, ctx=None)
    names = [t["name"] for t in visible]
    assert "admin_panel" not in names
    assert "read_document" in names


def test_filter_supports_object_with_name_attr(
    disable_admin_engine: PolicyEngine,
) -> None:
    """任意有 ``.name`` 屬性的物件都吃(用 SimpleNamespace 代替 FunctionTool)。"""
    from types import SimpleNamespace

    objs = [
        SimpleNamespace(name="admin_panel", description="x"),
        SimpleNamespace(name="read_document", description="y"),
    ]
    visible = disable_admin_engine.filter_tool_descriptions(objs, ctx=None)
    names = [t.name for t in visible]
    assert "admin_panel" not in names
    assert "read_document" in names


def test_filter_preserves_order(
    mixed_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """filter 不改變剩下 tool 的相對順序(穩定)。"""
    # 原順序:admin_panel, shell.run, read_document
    visible = mixed_engine.filter_tool_descriptions(tool_list, ctx=None)
    names = [t.name for t in visible]
    # admin_panel 被 disable;剩下兩個應保持 shell.run, read_document 順序
    assert names == ["shell.run", "read_document"]


def test_filter_with_empty_list_returns_empty(
    disable_admin_engine: PolicyEngine,
) -> None:
    """空 tool list → 空回傳,不 raise。"""
    assert disable_admin_engine.filter_tool_descriptions([], ctx=None) == []


def test_filter_returns_new_list_not_alias(
    tool_list: list[ToolDescriptor],
) -> None:
    """回傳的 list 應是新物件,呼叫端 mutate 不影響原 list。"""
    engine = PolicyEngine()
    visible = engine.filter_tool_descriptions(tool_list, ctx=None)
    visible.clear()
    assert len(tool_list) == 3, "原 tool_list 不該被影響"


# ---------------------------------------------------------------------------
# apply_policy_to_system_context — 與 P1-10 SystemContextBuilder 整合
# ---------------------------------------------------------------------------


def test_apply_policy_filters_before_system_context(
    disable_admin_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """apply_policy_to_system_context 把 DISABLE tool 從 prompt 移掉。"""
    builder = SystemContextBuilder()
    builder.add_role("You are the ANILA assistant.")

    applied = apply_policy_to_system_context(
        builder, disable_admin_engine, tool_list, ctx=None
    )

    prompt = builder.build()

    # admin_panel 不該出現在 prompt 任何地方
    assert "admin_panel" not in prompt
    # 其他兩個 tool 該在 prompt 內
    assert "shell.run" in prompt
    assert "read_document" in prompt
    # 回傳值 = 實際塞進 builder 的 tool list
    assert [t.name for t in applied] == ["shell.run", "read_document"]


def test_apply_policy_keeps_denied_tool_visible(
    deny_shell_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """DENY 的 tool 仍應出現在 system prompt(LLM 該知道有這 tool)。"""
    builder = SystemContextBuilder()
    apply_policy_to_system_context(
        builder, deny_shell_engine, tool_list, ctx=None
    )
    prompt = builder.build()

    assert "shell.run" in prompt, "DENY 不該把 tool 從 prompt 移除"
    assert "admin_panel" in prompt


def test_apply_policy_returns_filtered_list(
    mixed_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """helper 回傳 filter 後 list,供 caller 觀察 / log。"""
    builder = SystemContextBuilder()
    applied = apply_policy_to_system_context(
        builder, mixed_engine, tool_list, ctx=None
    )
    names = [t.name for t in applied]
    assert "admin_panel" not in names
    assert "shell.run" in names
    assert "read_document" in names


def test_apply_policy_deterministic_across_calls(
    disable_admin_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """同 builder state + 同 engine → 多次 build bytes 相同(維 P1-10 deterministic)。"""
    b1 = SystemContextBuilder()
    b1.add_role("X")
    apply_policy_to_system_context(b1, disable_admin_engine, tool_list, ctx=None)
    out1 = b1.build()

    b2 = SystemContextBuilder()
    b2.add_role("X")
    apply_policy_to_system_context(b2, disable_admin_engine, tool_list, ctx=None)
    out2 = b2.build()

    assert out1 == out2


# ---------------------------------------------------------------------------
# YAML config 範例(DISABLE + DENY 同檔)
# ---------------------------------------------------------------------------


def test_yaml_loads_disable_and_deny_together(tmp_path: Path) -> None:
    """YAML 內同時定義 DISABLE / DENY,載入後 effect 對。"""
    yaml = pytest.importorskip("yaml")
    del yaml  # 只是確認有裝 pyyaml,不用 module 本體

    yaml_text = textwrap.dedent(
        """
        rules:
          - name: hide_admin_tools
            tool_pattern: "admin_panel"
            effect: disable
            priority: 1000
            reason: "admin tools hidden in user session"

          - name: deny_shell_in_readonly
            tool_pattern: "shell.run"
            effect: deny
            priority: 900
            reason: "shell execution denied"

          - name: allow_read_tools
            tool_pattern: "read_document"
            effect: allow
            priority: 500
        """
    )
    config_path = tmp_path / "policy.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    engine = PolicyEngine.from_yaml(config_path)

    # 各 rule 行為驗證
    admin_decision = engine.evaluate(None, "admin_panel", {})
    shell_decision = engine.evaluate(None, "shell.run", {})
    read_decision = engine.evaluate(None, "read_document", {})

    assert admin_decision.effect is PolicyEffect.DISABLE
    assert shell_decision.effect is PolicyEffect.DENY
    assert read_decision.effect is PolicyEffect.ALLOW


def test_yaml_disable_filtered_from_prompt(tmp_path: Path) -> None:
    """YAML 載入後接 filter_tool_descriptions,DISABLE tool 不會進 prompt。"""
    pytest.importorskip("yaml")

    yaml_text = textwrap.dedent(
        """
        rules:
          - name: hide_admin
            tool_pattern: "admin_panel"
            effect: disable
            priority: 1000
        """
    )
    config_path = tmp_path / "policy.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    engine = PolicyEngine.from_yaml(config_path)
    tools = [
        ToolDescriptor(name="admin_panel", description="x"),
        ToolDescriptor(name="read_document", description="y"),
    ]
    visible = engine.filter_tool_descriptions(tools, ctx=None)
    assert [t.name for t in visible] == ["read_document"]


# ---------------------------------------------------------------------------
# docstring example 健全性 — 確保未來改 docstring 不會悄悄破範例
# ---------------------------------------------------------------------------


def test_docstring_example_in_filter_tool_descriptions() -> None:
    """policy.py 內 filter_tool_descriptions docstring 的 example 仍可執行。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="hide_admin",
            tool_pattern="admin.*",  # docstring 寫的 regex
            effect=PolicyEffect.DISABLE,
        )
    )
    all_tools = [
        ToolDescriptor(name="admin_panel", description="x"),
        ToolDescriptor(name="admin_users", description="y"),
        ToolDescriptor(name="read_document", description="z"),
    ]
    visible = engine.filter_tool_descriptions(all_tools, ctx=None)
    visible_names = {t.name for t in visible}
    # admin_panel / admin_users 都被 disable;read_document 保留
    assert visible_names == {"read_document"}


def test_docstring_example_in_apply_helper() -> None:
    """apply_policy_to_system_context docstring example 仍可執行。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="hide_admin",
            tool_pattern="admin_panel",
            effect=PolicyEffect.DISABLE,
            reason="admin tools hidden in user session",
        )
    )

    builder = SystemContextBuilder()
    builder.add_role("You are the ANILA assistant.")
    all_tools = [
        ToolDescriptor(name="admin_panel", description="x"),
        ToolDescriptor(name="read_document", description="y"),
    ]
    apply_policy_to_system_context(builder, engine, all_tools, ctx=None)

    prompt = builder.build()
    assert "admin_panel" not in prompt
    assert "read_document" in prompt


# ---------------------------------------------------------------------------
# 確保不破 P0-7 既有 API:filter 不影響 evaluate 行為
# ---------------------------------------------------------------------------


def test_filter_does_not_mutate_engine_state(
    mixed_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """filter_tool_descriptions 不該動 engine 的 rule set。"""
    rules_before = list(mixed_engine.rules)
    mixed_engine.filter_tool_descriptions(tool_list, ctx=None)
    rules_after = list(mixed_engine.rules)
    assert [r.name for r in rules_before] == [r.name for r in rules_after]


def test_evaluate_still_works_after_filter(
    mixed_engine: PolicyEngine,
    tool_list: list[ToolDescriptor],
) -> None:
    """filter 後 evaluate(P0-7 既有 API)行為一致。"""
    mixed_engine.filter_tool_descriptions(tool_list, ctx=None)

    # admin_panel 仍 DISABLE / shell.run 仍 DENY / read_document default ALLOW
    assert mixed_engine.evaluate(None, "admin_panel", {}).effect is PolicyEffect.DISABLE
    assert mixed_engine.evaluate(None, "shell.run", {}).effect is PolicyEffect.DENY
    assert mixed_engine.evaluate(None, "read_document", {}).effect is PolicyEffect.ALLOW
