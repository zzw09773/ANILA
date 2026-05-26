"""Unit tests:P1-16 Permission rule grammar mini DSL。

涵蓋面:
* `parse_permission_rule` 各種 valid rule(基本 / 巢狀 paren / 萬用字元 /
  escape)。
* invalid rule 觸發 `PermissionRuleSyntaxError`。
* `PermissionRule.to_policy_rule` 對齊 P0-7 `PolicyRule`。
* 整合 P0-7 `PolicyEngine`(parse → add → evaluate 行為正確)。
* `parse_permission_rules` 批次 parse。
* YAML sugar(`permission: "Read(**)"`)+ 舊 P0-7 yaml 格式相容。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from anila_agent.core.permission_grammar import (
    PermissionRule,
    PermissionRuleSyntaxError,
    load_policy_engine_from_yaml,
    parse_permission_rule,
    parse_permission_rules,
    policy_rule_from_yaml_item,
)
from anila_agent.core.policy import (
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
)


# ---------------------------------------------------------------------------
# parse_permission_rule — valid grammar
# ---------------------------------------------------------------------------


def test_parse_verb_only() -> None:
    """`"Read"` 無括號 → arg_pattern=None,effect=allow(預設)。"""
    rule = parse_permission_rule("Read")
    assert rule.verb == "Read"
    assert rule.arg_pattern is None
    assert rule.effect == "allow"


def test_parse_verb_with_double_star() -> None:
    """`"Read(**)"` → arg_pattern="**",對齊 Claude Code 上游語意。"""
    rule = parse_permission_rule("Read(**)")
    assert rule.verb == "Read"
    assert rule.arg_pattern == "**"


def test_parse_verb_with_path_pattern() -> None:
    """`"Write(/workspace/**)"` → arg_pattern 保留完整 glob 形式。"""
    rule = parse_permission_rule("Write(/workspace/**)")
    assert rule.verb == "Write"
    assert rule.arg_pattern == "/workspace/**"


def test_parse_bash_wildcard_treated_as_none() -> None:
    """`"Bash(*)"` 視同 `"Bash"` — arg_pattern 規範化為 None。"""
    rule = parse_permission_rule("Bash(*)")
    assert rule.verb == "Bash"
    assert rule.arg_pattern is None


def test_parse_empty_paren_treated_as_none() -> None:
    """`"Bash()"` 空括號等同無括號。"""
    rule = parse_permission_rule("Bash()")
    assert rule.verb == "Bash"
    assert rule.arg_pattern is None


def test_parse_nested_paren() -> None:
    """`"Bash(echo (foo))"` 巢狀括號保留完整內容。"""
    rule = parse_permission_rule("Bash(echo (foo))")
    assert rule.verb == "Bash"
    assert rule.arg_pattern == "echo (foo)"


def test_parse_deeply_nested_paren() -> None:
    """三層巢狀也要 depth counter 算對。"""
    rule = parse_permission_rule("Bash(((deep)))")
    assert rule.arg_pattern == "((deep))"


def test_parse_escaped_paren() -> None:
    """`\\(` / `\\)` 在 arg_pattern 內按字面字元處理。"""
    rule = parse_permission_rule(r'Bash(python -c "print\(1\)")')
    assert rule.verb == "Bash"
    assert rule.arg_pattern == 'python -c "print(1)"'


def test_parse_escaped_backslash() -> None:
    """`\\\\` 還原成單一 backslash。"""
    rule = parse_permission_rule(r"Write(C:\\path)")
    # `\\\\` → `\\`
    assert rule.arg_pattern == r"C:\path"


def test_parse_star_verb() -> None:
    """`"*"` verb 視為全 tool wildcard。"""
    rule = parse_permission_rule("*", effect="deny")
    assert rule.verb == "*"
    assert rule.arg_pattern is None
    assert rule.effect == "deny"


def test_parse_verb_with_dot() -> None:
    """`"file.read"` 這種帶 dot 的 verb 也是合法 identifier。"""
    rule = parse_permission_rule("file.read(**)")
    assert rule.verb == "file.read"
    assert rule.arg_pattern == "**"


def test_parse_custom_effect_and_priority() -> None:
    """關鍵參數可被覆寫。"""
    rule = parse_permission_rule(
        "Bash(rm -rf *)",
        effect="deny",
        priority=999,
        name="kill_switch",
        reason="rm -rf is destructive",
    )
    assert rule.effect == "deny"
    assert rule.priority == 999
    assert rule.name == "kill_switch"
    assert rule.reason == "rm -rf is destructive"


def test_parse_strips_outer_whitespace() -> None:
    """前後空白應被 strip。"""
    rule = parse_permission_rule("  Read(**)  ")
    assert rule.verb == "Read"
    assert rule.arg_pattern == "**"


# ---------------------------------------------------------------------------
# parse_permission_rule — invalid grammar 觸發 syntax error
# ---------------------------------------------------------------------------


def test_parse_empty_string_raises() -> None:
    """空字串無法 parse。"""
    with pytest.raises(PermissionRuleSyntaxError, match="empty"):
        parse_permission_rule("")


def test_parse_whitespace_only_raises() -> None:
    """純空白也算空。"""
    with pytest.raises(PermissionRuleSyntaxError, match="empty"):
        parse_permission_rule("   ")


def test_parse_missing_verb_raises() -> None:
    """`"(foo)"` 沒 verb,raise。"""
    with pytest.raises(PermissionRuleSyntaxError, match="missing verb"):
        parse_permission_rule("(foo)")


def test_parse_unbalanced_paren_raises() -> None:
    """`"Read(unbalanced"` 缺右括號。"""
    with pytest.raises(PermissionRuleSyntaxError, match="unbalanced"):
        parse_permission_rule("Read(unbalanced")


def test_parse_close_without_open_raises() -> None:
    """`")"` 單獨閉括號是錯的。"""
    with pytest.raises(PermissionRuleSyntaxError, match="unexpected"):
        parse_permission_rule("Read)")


def test_parse_trailing_content_raises() -> None:
    """`"Read(**)extra"` 閉括號後不該有內容。"""
    with pytest.raises(PermissionRuleSyntaxError, match="trailing content"):
        parse_permission_rule("Read(**)extra")


def test_parse_invalid_verb_charset_raises() -> None:
    """verb 不能以數字開頭。"""
    with pytest.raises(PermissionRuleSyntaxError, match="invalid verb"):
        parse_permission_rule("1Bad(**)")


def test_parse_invalid_effect_raises() -> None:
    """effect 字串必須是 allow / deny / disable。"""
    with pytest.raises(PermissionRuleSyntaxError, match="invalid effect"):
        parse_permission_rule("Read(**)", effect="permit")  # type: ignore[arg-type]


def test_parse_non_string_raises() -> None:
    """非字串輸入直接 raise。"""
    with pytest.raises(PermissionRuleSyntaxError):
        parse_permission_rule(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PermissionRule.to_policy_rule — 對齊 P0-7 PolicyRule
# ---------------------------------------------------------------------------


def test_to_policy_rule_returns_policy_rule_instance() -> None:
    """轉出來必須是 P0-7 `PolicyRule`。"""
    rule = parse_permission_rule("Read(**)")
    pr = rule.to_policy_rule()
    assert isinstance(pr, PolicyRule)


def test_to_policy_rule_effect_mapping() -> None:
    """`"allow"` / `"deny"` / `"disable"` 三 effect 都對。"""
    assert (
        parse_permission_rule("Read", effect="allow").to_policy_rule().effect
        is PolicyEffect.ALLOW
    )
    assert (
        parse_permission_rule("Bash(*)", effect="deny").to_policy_rule().effect
        is PolicyEffect.DENY
    )
    assert (
        parse_permission_rule("Write(*)", effect="disable").to_policy_rule().effect
        is PolicyEffect.DISABLE
    )


def test_to_policy_rule_wildcard_verb_has_no_tool_pattern() -> None:
    """`"*"` verb → tool_pattern=None(全 tool wildcard)。"""
    rule = parse_permission_rule("*", effect="deny")
    pr = rule.to_policy_rule()
    assert pr.tool_pattern is None


def test_to_policy_rule_priority_passed_through() -> None:
    """priority 不該在 to_policy_rule() 過程中丟失。"""
    rule = parse_permission_rule("Read(**)", priority=555)
    assert rule.to_policy_rule().priority == 555


def test_to_policy_rule_reason_passed_through() -> None:
    """reason 應透傳到 PolicyRule。"""
    rule = parse_permission_rule(
        "Bash(rm -rf *)",
        effect="deny",
        reason="too dangerous",
    )
    assert rule.to_policy_rule().reason == "too dangerous"


def test_to_policy_rule_no_arg_means_no_condition() -> None:
    """`"Read"` 無括號 → condition=None(verb 命中即生效)。"""
    rule = parse_permission_rule("Read")
    pr = rule.to_policy_rule()
    assert pr.condition is None


def test_to_policy_rule_with_arg_has_condition() -> None:
    """有 arg_pattern → condition 不為 None。"""
    rule = parse_permission_rule("Read(/workspace/**)")
    pr = rule.to_policy_rule()
    assert pr.condition is not None


# ---------------------------------------------------------------------------
# 整合 PolicyEngine — parse 一條 rule → add → evaluate 行為對
# ---------------------------------------------------------------------------


def test_engine_allow_read_matches_read_file_tool() -> None:
    """`"Read(**)"` 允許 → `read_file` tool 評估為 ALLOW。"""
    rule = parse_permission_rule("Read(**)")
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    decision = engine.evaluate(None, "read_file", {"path": "/anywhere"})
    assert decision.allowed


def test_engine_allow_read_matches_canonical_name() -> None:
    """`"Read(**)"` 也命中 canonical name `read`。"""
    rule = parse_permission_rule("Read(**)")
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    decision = engine.evaluate(None, "read", {"path": "/anywhere"})
    assert decision.allowed


def test_engine_deny_bash_blocks_all_shell() -> None:
    """`"Bash(*)"` deny → bash / shell 任何 cmd 都被擋。"""
    rule = parse_permission_rule("Bash(*)", effect="deny")
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    decision = engine.evaluate(None, "bash", {"cmd": "ls"})
    assert decision.denied
    assert decision.effect is PolicyEffect.DENY


def test_engine_deny_rm_rf_only_blocks_matching_cmd() -> None:
    """`"Bash(rm -rf *)"` 只擋 rm -rf,其他 cmd 通過。"""
    deny_rule = parse_permission_rule(
        "Bash(rm -rf *)",
        effect="deny",
        priority=1000,
    )
    engine = PolicyEngine()
    engine.add_rule(deny_rule.to_policy_rule())

    blocked = engine.evaluate(None, "bash", {"cmd": "rm -rf /etc"})
    assert blocked.denied

    # `ls /tmp` 不該命中 deny rule;沒其他 rule → fallback ALLOW。
    allowed = engine.evaluate(None, "bash", {"cmd": "ls /tmp"})
    assert allowed.allowed


def test_engine_priority_deny_overrides_allow() -> None:
    """高 priority deny 應 short-circuit 比 低 priority allow 先評估。"""
    allow = parse_permission_rule("Bash(*)", effect="allow", priority=100)
    deny = parse_permission_rule(
        "Bash(rm -rf *)", effect="deny", priority=999
    )
    engine = PolicyEngine()
    engine.add_rule(allow.to_policy_rule())
    engine.add_rule(deny.to_policy_rule())

    d1 = engine.evaluate(None, "bash", {"cmd": "rm -rf /home"})
    assert d1.denied

    d2 = engine.evaluate(None, "bash", {"cmd": "ls"})
    assert d2.allowed


def test_engine_write_workspace_only_path_glob() -> None:
    """`"Write(/workspace/**)"` 允許 /workspace 下任意檔。"""
    allow = parse_permission_rule("Write(/workspace/**)", effect="allow", priority=600)
    engine = PolicyEngine()
    engine.add_rule(allow.to_policy_rule())

    # 命中 — workspace 內。
    inside = engine.evaluate(None, "file.write", {"path": "/workspace/a/b/c.txt"})
    assert inside.allowed
    assert inside.rule_name is not None

    # 不命中 — workspace 外路徑;沒命中 rule → fallback ALLOW(default open)。
    # 但 rule_name 應為 None(沒 rule 命中)。
    outside = engine.evaluate(None, "file.write", {"path": "/etc/passwd"})
    assert outside.allowed
    assert outside.rule_name is None


def test_engine_disable_effect_propagates() -> None:
    """`effect="disable"` 在 engine 內維持 DISABLE。"""
    rule = parse_permission_rule("Write(**)", effect="disable")
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    decision = engine.evaluate(None, "write_file", {"path": "/tmp/x"})
    assert decision.disabled
    assert decision.effect is PolicyEffect.DISABLE


def test_engine_star_verb_blocks_everything() -> None:
    """`"*"` verb deny → 任何 tool 都被擋。"""
    rule = parse_permission_rule("*", effect="deny", priority=999)
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    for tool in ("file.read", "bash", "random_tool", ""):
        decision = engine.evaluate(None, tool, {})
        assert decision.denied, f"{tool!r} 應被 deny"


def test_engine_verb_with_no_arg_matches_any_args() -> None:
    """`"Read"` 無括號 → 任何 args 都命中(verb-only rule)。"""
    rule = parse_permission_rule("Read", effect="allow")
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    assert engine.evaluate(None, "read_file", {}).allowed
    assert engine.evaluate(None, "read", {"foo": "bar"}).allowed


def test_engine_arg_pattern_no_path_in_args_falls_through() -> None:
    """有 arg_pattern 但 args 沒 path key → rule 不命中,fallback 預設 ALLOW。"""
    rule = parse_permission_rule("Read(/workspace/**)", effect="deny", priority=999)
    engine = PolicyEngine()
    engine.add_rule(rule.to_policy_rule())
    # 沒 path key,deny rule 不命中 → engine default ALLOW。
    decision = engine.evaluate(None, "read_file", {"other_arg": 1})
    assert decision.allowed
    assert decision.rule_name is None


# ---------------------------------------------------------------------------
# parse_permission_rules — 批次 parse
# ---------------------------------------------------------------------------


def test_parse_permission_rules_batch() -> None:
    """批次 parse N 條 rule,長度與順序對齊。"""
    rules = parse_permission_rules(
        ["Read(**)", "Write(/workspace/**)", "Bash(ls)"],
        effect="allow",
        priority=500,
    )
    assert len(rules) == 3
    assert [r.verb for r in rules] == ["Read", "Write", "Bash"]
    assert all(r.effect == "allow" for r in rules)
    assert all(r.priority == 500 for r in rules)


def test_parse_permission_rules_propagates_syntax_error() -> None:
    """任一條失敗整批就 raise。"""
    with pytest.raises(PermissionRuleSyntaxError):
        parse_permission_rules(["Read(**)", "(broken)"])


# ---------------------------------------------------------------------------
# YAML sugar — policy_rule_from_yaml_item + load_policy_engine_from_yaml
# ---------------------------------------------------------------------------


def test_yaml_item_with_permission_sugar() -> None:
    """`permission: "Read(**)"` 自動展開成 PolicyRule。"""
    pr = policy_rule_from_yaml_item(
        {"name": "allow_read", "permission": "Read(**)", "priority": 600}
    )
    assert isinstance(pr, PolicyRule)
    assert pr.priority == 600
    assert pr.effect is PolicyEffect.ALLOW


def test_yaml_item_with_permission_sugar_deny() -> None:
    """sugar 也能配 deny。"""
    pr = policy_rule_from_yaml_item(
        {
            "permission": "Bash(rm -rf *)",
            "effect": "deny",
            "priority": 999,
            "reason": "rm -rf banned",
        }
    )
    assert pr.effect is PolicyEffect.DENY
    assert pr.reason == "rm -rf banned"
    assert pr.priority == 999


def test_yaml_item_legacy_format_still_works() -> None:
    """舊 P0-7 yaml 格式(無 `permission` key)仍能載入。"""
    pr = policy_rule_from_yaml_item(
        {
            "name": "legacy_deny",
            "tool_pattern": "file\\.write",
            "effect": "deny",
            "priority": 800,
            "reason": "legacy",
        }
    )
    assert pr.name == "legacy_deny"
    assert pr.effect is PolicyEffect.DENY
    assert pr.priority == 800


def test_yaml_item_legacy_missing_name_raises() -> None:
    """舊格式 name 缺失應 raise ValueError。"""
    with pytest.raises(ValueError, match="name"):
        policy_rule_from_yaml_item({"tool_pattern": "*", "effect": "deny"})


def test_yaml_item_sugar_invalid_permission_raises() -> None:
    """sugar 內字串語法錯誤 raise PermissionRuleSyntaxError。"""
    with pytest.raises(PermissionRuleSyntaxError):
        policy_rule_from_yaml_item({"permission": "(broken)"})


def test_yaml_item_sugar_invalid_effect_raises() -> None:
    """sugar effect 不合法 raise ValueError。"""
    with pytest.raises(ValueError, match="invalid effect"):
        policy_rule_from_yaml_item(
            {"permission": "Read(**)", "effect": "permit"}
        )


def test_load_yaml_engine_with_sugar_and_legacy(tmp_path: Path) -> None:
    """整檔 YAML 含 sugar + legacy 兩種 rule,都要載入成功。"""
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(
        textwrap.dedent(
            """
            rules:
              - name: allow_read
                permission: "Read(**)"
                priority: 500

              - permission: "Bash(rm -rf *)"
                effect: deny
                priority: 999
                reason: "destructive"

              - name: legacy_disable_write
                tool_pattern: "file\\\\.write"
                effect: disable
                priority: 800
            """
        ).strip(),
        encoding="utf-8",
    )
    engine = load_policy_engine_from_yaml(yaml_path)
    rules = engine.sorted_rules()
    assert len(rules) == 3
    # 高 priority 先
    assert rules[0].priority == 999
    assert rules[0].effect is PolicyEffect.DENY
    assert rules[1].priority == 800
    assert rules[1].effect is PolicyEffect.DISABLE
    assert rules[2].priority == 500


def test_load_yaml_engine_rules_not_list_raises(tmp_path: Path) -> None:
    """`rules` 不是 list → ValueError。"""
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("rules: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a list"):
        load_policy_engine_from_yaml(yaml_path)


def test_load_yaml_engine_item_not_mapping_raises(tmp_path: Path) -> None:
    """rule item 不是 mapping → ValueError。"""
    yaml_path = tmp_path / "bad2.yaml"
    yaml_path.write_text("rules:\n  - just-a-string\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_policy_engine_from_yaml(yaml_path)


# ---------------------------------------------------------------------------
# Verb alias map 覆寫
# ---------------------------------------------------------------------------


def test_custom_verb_alias_map() -> None:
    """user 可給 verb_alias_map 覆寫 verb→tool regex 對映。"""
    alias = {"custom": r"(?i)my_custom_tool"}
    rule = parse_permission_rule(
        "custom(**)", verb_alias_map=alias
    )
    pr = rule.to_policy_rule()
    engine = PolicyEngine()
    engine.add_rule(pr)
    assert engine.evaluate(None, "my_custom_tool", {"path": "/x"}).allowed
    # 未在 alias 中的 tool 不命中。
    decision = engine.evaluate(None, "other_tool", {"path": "/x"})
    assert decision.rule_name is None


# ---------------------------------------------------------------------------
# PermissionRule dataclass 直接構造的 invariant
# ---------------------------------------------------------------------------


def test_permission_rule_normalizes_verb_to_lowercase() -> None:
    """`normalized_verb` 一律 lowercase。"""
    rule = PermissionRule(verb="READ", arg_pattern="**")
    assert rule.normalized_verb == "read"


def test_permission_rule_empty_arg_pattern_normalizes_to_none() -> None:
    """`arg_pattern=""` 等同 None。"""
    rule = PermissionRule(verb="Read", arg_pattern="")
    assert rule.arg_pattern is None


def test_permission_rule_star_arg_pattern_normalizes_to_none() -> None:
    """`arg_pattern="*"` 等同 None。"""
    rule = PermissionRule(verb="Read", arg_pattern="*")
    assert rule.arg_pattern is None


def test_permission_rule_direct_invalid_effect_raises() -> None:
    """直接建構 PermissionRule 也要驗 effect。"""
    with pytest.raises(PermissionRuleSyntaxError):
        PermissionRule(verb="Read", effect="permit")  # type: ignore[arg-type]


def test_permission_rule_direct_empty_verb_raises() -> None:
    """verb 空字串直接 raise。"""
    with pytest.raises(PermissionRuleSyntaxError):
        PermissionRule(verb="", arg_pattern="**")
