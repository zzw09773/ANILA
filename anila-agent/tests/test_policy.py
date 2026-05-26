"""Unit tests:P0-7 Policy DSL(PolicyRule + PolicyEngine + factory + adapter)。

涵蓋面:
* `PolicyRule.matches_tool`:wildcard(None / "*")/ 精準字串 / regex 三種 pattern。
* `PolicyEngine` priority 排序(高優先級先;同優先級維持註冊序)+ short-circuit。
* `PolicyEngine.evaluate` 對 ALLOW / DENY / DISABLE 三 effect 的回傳格式。
* `deny_all` / `allow_all_except` / `workspace_only` / `read_only` 四個 factory。
* `workspace_only` 整合 P0-3 `AnilaToolContext.safe_path`(逃出 workspace 應 deny)。
* adapter `policy_to_tool_input_guardrail` 把 PolicyEffect 對應到 ALLOW/BLOCK 行為。
* YAML 載入 stretch case。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from anila_agent.core.context import AnilaToolContext
from anila_agent.core.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
    allow_all_except,
    deny_all,
    policy_to_tool_input_guardrail,
    read_only,
    workspace_only,
)
from anila_agent.tools.guardrails import (
    ToolGuardrailBehavior,
    ToolGuardrailResult,
    ToolInputGuardrail,
)


# ---------------------------------------------------------------------------
# PolicyRule.matches_tool — pattern 三種寫法
# ---------------------------------------------------------------------------


def test_rule_wildcard_none_matches_any_tool() -> None:
    """tool_pattern=None 應視為 wildcard,任何 tool name 都 match。"""
    rule = PolicyRule(name="any", tool_pattern=None, effect=PolicyEffect.ALLOW)
    assert rule.matches_tool("anything")
    assert rule.matches_tool("file.write")
    assert rule.matches_tool("")


def test_rule_wildcard_star_matches_any_tool() -> None:
    """tool_pattern='*' 應等同於 None,wildcard 套用。"""
    rule = PolicyRule(name="star", tool_pattern="*", effect=PolicyEffect.ALLOW)
    assert rule.matches_tool("read_file")


def test_rule_exact_string_match_strict() -> None:
    """純字串 pattern 應精準比對,不可前綴 / 後綴。"""
    rule = PolicyRule(name="exact", tool_pattern="read_file", effect=PolicyEffect.ALLOW)
    assert rule.matches_tool("read_file")
    assert not rule.matches_tool("read_files")
    assert not rule.matches_tool("read_file_v2")
    assert not rule.matches_tool("")


def test_rule_regex_string_with_meta_compiled() -> None:
    """字串含 regex meta 應自動 compile;fullmatch 語意。"""
    rule = PolicyRule(name="regex", tool_pattern="file\\..*", effect=PolicyEffect.DENY)
    assert rule.matches_tool("file.read")
    assert rule.matches_tool("file.write")
    assert not rule.matches_tool("filesystem")  # 沒 dot


def test_rule_regex_pattern_object_used_directly() -> None:
    """直接傳 re.Pattern 應原樣使用,不重複 compile。"""
    import re

    pattern = re.compile(r"^shell\.(run|exec)$")
    rule = PolicyRule(name="regex-obj", tool_pattern=pattern, effect=PolicyEffect.DENY)
    assert rule.matches_tool("shell.run")
    assert rule.matches_tool("shell.exec")
    assert not rule.matches_tool("shell.list")


# ---------------------------------------------------------------------------
# PolicyEngine — priority 排序與 short-circuit
# ---------------------------------------------------------------------------


def test_engine_default_open_when_no_rule_matches() -> None:
    """沒任何 rule 時 evaluate 應回 ALLOW,rule_name=None。"""
    engine = PolicyEngine()
    decision = engine.evaluate(ctx=None, tool_name="any_tool", args={})
    assert decision.effect is PolicyEffect.ALLOW
    assert decision.rule_name is None
    assert decision.allowed


def test_engine_priority_high_first_short_circuit() -> None:
    """高 priority rule 先評估,first-match 直接決定結果。"""
    engine = PolicyEngine()
    # 低 priority 的 allow_all。
    engine.add_rule(
        PolicyRule(
            name="allow_all", tool_pattern=None, effect=PolicyEffect.ALLOW, priority=0
        )
    )
    # 高 priority 的 deny shell。
    engine.add_rule(
        PolicyRule(
            name="deny_shell",
            tool_pattern="shell.run",
            effect=PolicyEffect.DENY,
            priority=1000,
            reason="shell disallowed",
        )
    )
    decision = engine.evaluate(ctx=None, tool_name="shell.run", args={})
    assert decision.effect is PolicyEffect.DENY
    assert decision.rule_name == "deny_shell"
    assert decision.reason == "shell disallowed"


def test_engine_same_priority_keeps_registration_order() -> None:
    """同 priority 內第一個註冊的 rule 先評估(stable sort)。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(name="first", tool_pattern=None, effect=PolicyEffect.ALLOW)
    )
    engine.add_rule(
        PolicyRule(
            name="second",
            tool_pattern=None,
            effect=PolicyEffect.DENY,
            reason="never reached",
        )
    )
    decision = engine.evaluate(ctx=None, tool_name="any", args={})
    assert decision.effect is PolicyEffect.ALLOW
    assert decision.rule_name == "first"


def test_engine_add_rules_batch() -> None:
    """`add_rules` 批次加入後應全部生效。"""
    engine = PolicyEngine()
    engine.add_rules(
        [
            PolicyRule(name="r1", tool_pattern="a", effect=PolicyEffect.DENY),
            PolicyRule(name="r2", tool_pattern="b", effect=PolicyEffect.ALLOW),
        ]
    )
    assert engine.evaluate(None, "a", {}).denied
    assert engine.evaluate(None, "b", {}).allowed


def test_engine_sorted_rules_returns_high_to_low() -> None:
    """sorted_rules 應回傳高→低 priority。"""
    engine = PolicyEngine()
    engine.add_rule(PolicyRule(name="low", tool_pattern=None, priority=10))
    engine.add_rule(PolicyRule(name="high", tool_pattern=None, priority=1000))
    engine.add_rule(PolicyRule(name="mid", tool_pattern=None, priority=500))
    names = [r.name for r in engine.sorted_rules()]
    assert names == ["high", "mid", "low"]


# ---------------------------------------------------------------------------
# Effect 三種:ALLOW / DENY / DISABLE
# ---------------------------------------------------------------------------


def test_evaluate_allow_effect() -> None:
    """ALLOW effect 命中應回 PolicyDecision.allow。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(name="ok", tool_pattern="ping", effect=PolicyEffect.ALLOW)
    )
    decision = engine.evaluate(None, "ping", {})
    assert decision.effect is PolicyEffect.ALLOW
    assert decision.allowed and not decision.denied


def test_evaluate_deny_effect_with_reason() -> None:
    """DENY effect 命中 reason 必出現。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="no_shell",
            tool_pattern="shell.run",
            effect=PolicyEffect.DENY,
            reason="prod disallows shell",
        )
    )
    decision = engine.evaluate(None, "shell.run", {})
    assert decision.effect is PolicyEffect.DENY
    assert decision.denied
    assert decision.reason == "prod disallows shell"


def test_evaluate_disable_effect_stronger_than_deny() -> None:
    """DISABLE effect 命中 disabled=True、denied=True。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="cap_filter",
            tool_pattern="dangerous_tool",
            effect=PolicyEffect.DISABLE,
            reason="not available on this plan",
        )
    )
    decision = engine.evaluate(None, "dangerous_tool", {})
    assert decision.effect is PolicyEffect.DISABLE
    assert decision.disabled
    assert decision.denied  # disabled 也算 denied


def test_decision_factory_methods() -> None:
    """PolicyDecision.allow / deny / disable factory 行為正確。"""
    a = PolicyDecision.allow(rule_name="r")
    d = PolicyDecision.deny(reason="bad", rule_name="r")
    x = PolicyDecision.disable(reason="off", rule_name="r")
    assert a.allowed and not a.denied
    assert d.denied and not d.disabled
    assert x.denied and x.disabled


# ---------------------------------------------------------------------------
# condition 行為
# ---------------------------------------------------------------------------


def test_rule_condition_predicate_can_filter_match() -> None:
    """condition 為 False 時即使 tool name match 也不命中。"""

    def _is_secret_path(ctx: Any, tool: str, args: dict[str, Any]) -> bool:
        return str(args.get("path", "")).endswith("secrets.txt")

    rule = PolicyRule(
        name="deny_secret",
        tool_pattern="read_file",
        effect=PolicyEffect.DENY,
        condition=_is_secret_path,
        reason="cannot read secrets",
    )
    assert rule.matches(None, "read_file", {"path": "/tmp/secrets.txt"})
    assert not rule.matches(None, "read_file", {"path": "/tmp/log.txt"})


def test_rule_condition_exception_fails_to_match() -> None:
    """condition 噴例外應視為不 match(fail-closed 給後續 rule 接手)。"""

    def _boom(ctx: Any, tool: str, args: dict[str, Any]) -> bool:
        raise RuntimeError("boom")

    rule = PolicyRule(
        name="broken",
        tool_pattern="*",
        effect=PolicyEffect.DENY,
        condition=_boom,
    )
    # 不應 propagate exception。
    assert rule.matches(None, "any", {}) is False


# ---------------------------------------------------------------------------
# Factory: deny_all
# ---------------------------------------------------------------------------


def test_deny_all_factory_blocks_everything() -> None:
    """deny_all 應對所有 tool 回 DENY。"""
    engine = PolicyEngine()
    engine.add_rule(deny_all())
    for tool in ("read_file", "write_file", "shell.run", ""):
        d = engine.evaluate(None, tool, {})
        assert d.effect is PolicyEffect.DENY, f"{tool} should be denied"


def test_deny_all_lowest_priority_so_specific_allow_wins() -> None:
    """deny_all priority 預設最低,讓特定 allow rule override。"""
    engine = PolicyEngine()
    engine.add_rule(deny_all())
    engine.add_rule(
        PolicyRule(
            name="allow_read",
            tool_pattern="read_file",
            effect=PolicyEffect.ALLOW,
            priority=100,
        )
    )
    assert engine.evaluate(None, "read_file", {}).allowed
    assert engine.evaluate(None, "write_file", {}).denied


# ---------------------------------------------------------------------------
# Factory: allow_all_except
# ---------------------------------------------------------------------------


def test_allow_all_except_blocks_listed_and_allows_others() -> None:
    """allow_all_except 應只 deny 列表內 tool,其它一律 allow。"""
    engine = PolicyEngine()
    engine.add_rules(allow_all_except("shell.run", "delete_file"))

    assert engine.evaluate(None, "shell.run", {}).denied
    assert engine.evaluate(None, "delete_file", {}).denied
    assert engine.evaluate(None, "read_file", {}).allowed
    assert engine.evaluate(None, "anything_else", {}).allowed


def test_allow_all_except_deny_reason_present() -> None:
    """allow_all_except 自動附 reason,讓 LLM 能理解。"""
    engine = PolicyEngine()
    engine.add_rules(allow_all_except("shell.run"))
    decision = engine.evaluate(None, "shell.run", {})
    assert decision.denied
    assert decision.reason is not None
    assert "shell.run" in decision.reason


# ---------------------------------------------------------------------------
# Factory: workspace_only(整合 P0-3)
# ---------------------------------------------------------------------------


def _make_ctx(workspace: Path) -> AnilaToolContext:
    return AnilaToolContext(
        session_id="s1",
        turn_id=1,
        tool_call_id="tc1",
        agent_name="test",
        workspace=workspace,
    )


def test_workspace_only_allows_path_inside_root(tmp_path: Path) -> None:
    """path 在 allowed_roots 之下時應 allow(rule 不命中)。"""
    ctx = _make_ctx(tmp_path)
    engine = PolicyEngine()
    engine.add_rule(workspace_only([tmp_path]))
    inside = tmp_path / "foo.txt"
    decision = engine.evaluate(ctx, "file.write", {"path": str(inside)})
    assert decision.allowed


def test_workspace_only_denies_path_escaping_root(tmp_path: Path) -> None:
    """path 跳到 allowed_roots 外應 DENY。"""
    ctx = _make_ctx(tmp_path)
    engine = PolicyEngine()
    engine.add_rule(workspace_only([tmp_path]))
    decision = engine.evaluate(
        ctx, "file.write", {"path": "/etc/passwd"}
    )
    assert decision.denied
    assert decision.effect is PolicyEffect.DENY
    assert decision.reason and "workspace" in decision.reason.lower()


def test_workspace_only_only_applies_to_file_tools(tmp_path: Path) -> None:
    """workspace_only 不該干擾非 file tool(例如 search、HTTP)。"""
    ctx = _make_ctx(tmp_path)
    engine = PolicyEngine()
    engine.add_rule(workspace_only([tmp_path]))
    decision = engine.evaluate(
        ctx, "rag.search", {"path": "/etc/passwd"}  # 故意給逃脫 path
    )
    # rag.search 不在預設 file tool 名單,不命中 → fallback allow。
    assert decision.allowed


def test_workspace_only_no_path_arg_passes(tmp_path: Path) -> None:
    """args 內沒 path 時不命中 deny(對齊 antigravity 設計)。"""
    ctx = _make_ctx(tmp_path)
    engine = PolicyEngine()
    engine.add_rule(workspace_only([tmp_path]))
    decision = engine.evaluate(ctx, "list_dir", {})
    assert decision.allowed


def test_workspace_only_with_relative_path_anchors_to_workspace(
    tmp_path: Path,
) -> None:
    """相對路徑應 anchor 在 ctx.workspace 下評估。"""
    ctx = _make_ctx(tmp_path)
    engine = PolicyEngine()
    engine.add_rule(workspace_only([tmp_path]))
    # 相對路徑 "sub/foo.txt" 解到 tmp_path/sub/foo.txt,合法。
    decision = engine.evaluate(ctx, "file.write", {"path": "sub/foo.txt"})
    assert decision.allowed


def test_workspace_only_custom_tool_names(tmp_path: Path) -> None:
    """允許用 tool_names 自訂套用範圍。"""
    ctx = _make_ctx(tmp_path)
    engine = PolicyEngine()
    engine.add_rule(workspace_only([tmp_path], tool_names=["my.custom.write"]))
    # 預設 file tool 不在自訂名單中 → 不命中。
    assert engine.evaluate(ctx, "file.write", {"path": "/etc/passwd"}).allowed
    # 自訂 tool 名稱命中。
    assert engine.evaluate(ctx, "my.custom.write", {"path": "/etc/passwd"}).denied


# ---------------------------------------------------------------------------
# Factory: read_only
# ---------------------------------------------------------------------------


def test_read_only_with_explicit_tool_names() -> None:
    """明確列出 destructive tool name,該 tool 應 DENY。"""
    engine = PolicyEngine()
    engine.add_rules(read_only(destructive_tool_names=["delete_file", "shell.run"]))
    assert engine.evaluate(None, "delete_file", {}).denied
    assert engine.evaluate(None, "shell.run", {}).denied
    # 非 destructive 的 tool 走 fallback ALLOW。
    assert engine.evaluate(None, "read_file", {}).allowed


def test_read_only_with_metadata_lookup() -> None:
    """metadata_lookup 回 is_destructive=True 的 tool 應 DENY。"""

    class _FakeMeta:
        def __init__(self, destructive: bool) -> None:
            self.is_destructive = destructive

    def _lookup(name: str) -> Any:
        return _FakeMeta(destructive=(name == "rm_rf"))

    engine = PolicyEngine()
    engine.add_rules(read_only(metadata_lookup=_lookup))
    assert engine.evaluate(None, "rm_rf", {}).denied
    assert engine.evaluate(None, "read_file", {}).allowed


def test_read_only_no_metadata_no_names_fail_closed() -> None:
    """兩個來源都沒給 → 全 tool 一律 deny(保守)。"""
    engine = PolicyEngine()
    engine.add_rules(read_only())
    assert engine.evaluate(None, "anything", {}).denied


# ---------------------------------------------------------------------------
# Adapter: policy_to_tool_input_guardrail
# ---------------------------------------------------------------------------


def test_adapter_allow_maps_to_tool_guardrail_allow() -> None:
    """policy ALLOW 應 map 到 ToolGuardrailBehavior.ALLOW。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(name="ok", tool_pattern="ping", effect=PolicyEffect.ALLOW)
    )
    guard = policy_to_tool_input_guardrail(engine)
    assert isinstance(guard, ToolInputGuardrail)
    result = guard.check(ctx=None, tool_name="ping", args={})
    assert isinstance(result, ToolGuardrailResult)
    assert result.behavior is ToolGuardrailBehavior.ALLOW
    assert result.tripwire_triggered is False


def test_adapter_deny_maps_to_tool_guardrail_block() -> None:
    """policy DENY 應 map 到 BLOCK 且 output_info 帶 reason。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="no",
            tool_pattern="shell.run",
            effect=PolicyEffect.DENY,
            reason="banned",
        )
    )
    guard = policy_to_tool_input_guardrail(engine)
    result = guard.check(ctx=None, tool_name="shell.run", args={})
    assert result.behavior is ToolGuardrailBehavior.BLOCK
    assert result.tripwire_triggered is True
    assert result.output_info.get("policy_effect") == "deny"
    assert result.output_info.get("reason") == "banned"


def test_adapter_disable_default_maps_to_block_with_flag() -> None:
    """policy DISABLE 預設 map 到 BLOCK,output_info 含 disabled=True。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(
            name="off", tool_pattern="x", effect=PolicyEffect.DISABLE, reason="capdown"
        )
    )
    guard = policy_to_tool_input_guardrail(engine)
    result = guard.check(ctx=None, tool_name="x", args={})
    assert result.behavior is ToolGuardrailBehavior.BLOCK
    assert result.output_info.get("disabled") is True
    assert result.output_info.get("policy_effect") == "disable"


def test_adapter_disable_as_block_false_maps_to_allow() -> None:
    """disable_as_block=False 時 DISABLE 不 raise tripwire,讓 capability filter 處理。"""
    engine = PolicyEngine()
    engine.add_rule(
        PolicyRule(name="off", tool_pattern="x", effect=PolicyEffect.DISABLE)
    )
    guard = policy_to_tool_input_guardrail(engine, disable_as_block=False)
    result = guard.check(ctx=None, tool_name="x", args={})
    assert result.behavior is ToolGuardrailBehavior.ALLOW
    assert result.output_info.get("disabled") is True


def test_adapter_enforce_block_raises_tripwire() -> None:
    """走 P0-6 `enforce()` 路徑時 BLOCK 應 raise GuardrailTripwireTriggered。"""
    from anila_agent.core.guardrails import GuardrailTripwireTriggered

    engine = PolicyEngine()
    engine.add_rule(deny_all())
    guard = policy_to_tool_input_guardrail(engine)
    with pytest.raises(GuardrailTripwireTriggered) as excinfo:
        guard.enforce(ctx=None, tool_name="anything", args={})
    # tripwire 帶 policy info,方便上層 trace。
    assert excinfo.value.info.get("policy_effect") == "deny"


# ---------------------------------------------------------------------------
# YAML 載入 — stretch goal
# ---------------------------------------------------------------------------


def test_yaml_loader_parses_basic_rule_set(tmp_path: Path) -> None:
    """YAML 載入應正確產生對應 rule;預設值與 priority 都要正確。"""
    yaml_text = textwrap.dedent(
        """
        rules:
          - name: deny_shell
            tool_pattern: shell.run
            effect: deny
            priority: 1000
            reason: "shell disallowed on prod"

          - name: allow_read
            tool_pattern: "read_.*"
            effect: allow
            priority: 500

          - name: default_deny
            effect: deny
            priority: 0
            reason: "default-deny"
        """
    ).strip()
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")

    engine = PolicyEngine.from_yaml(yaml_path)
    # shell.run → deny by deny_shell。
    d1 = engine.evaluate(None, "shell.run", {})
    assert d1.denied
    assert d1.rule_name == "deny_shell"
    # read_anything → allow by allow_read。
    d2 = engine.evaluate(None, "read_file", {})
    assert d2.allowed
    assert d2.rule_name == "allow_read"
    # 沒任何 specific rule 命中 → default_deny。
    d3 = engine.evaluate(None, "write_file", {})
    assert d3.denied
    assert d3.rule_name == "default_deny"


def test_yaml_loader_rejects_invalid_effect(tmp_path: Path) -> None:
    """非法 effect 應 raise ValueError。"""
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text(
        "rules:\n  - name: x\n    effect: maybe\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid effect"):
        PolicyEngine.from_yaml(yaml_path)


def test_yaml_loader_requires_name_field(tmp_path: Path) -> None:
    """rule 缺 name 應 raise ValueError。"""
    yaml_path = tmp_path / "no_name.yaml"
    yaml_path.write_text(
        "rules:\n  - effect: allow\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="name"):
        PolicyEngine.from_yaml(yaml_path)
