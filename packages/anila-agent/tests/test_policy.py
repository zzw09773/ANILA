"""工具政策 DSL 優先序、deny-all 預設、guardrail enforcement、啟動守衛。"""

from __future__ import annotations

import pytest

from anila_agent.policy.dsl import (
    Effect,
    Policy,
    allow,
    ask_user,
    deny,
    load_policy,
)
from anila_agent.policy.guardrail import (
    build_policy_guardrail,
    enforce_privileged_need_explicit_rules,
)
from anila_agent.tools.capabilities import Capability

pytestmark = pytest.mark.unit


# ---- 評估優先序 ----

def test_default_is_deny():
    assert Policy().evaluate("anything").effect is Effect.DENY


def test_specific_deny_beats_specific_allow():
    p = Policy(rules=(allow("t"), deny("t")))
    assert p.evaluate("t").effect is Effect.DENY


def test_specific_beats_wildcard():
    # specific allow 勝過 wildcard deny。
    p = Policy(rules=(deny("*"), allow("t")))
    assert p.evaluate("t").effect is Effect.ALLOW
    assert p.evaluate("other").effect is Effect.DENY  # other 只匹配 wildcard deny


def test_ask_between_deny_and_allow():
    p = Policy(rules=(allow("t"), ask_user("t")))
    assert p.evaluate("t").effect is Effect.ASK  # ask 勝過 allow（同 specific 桶）


def test_predicate_gates_rule():
    p = Policy(rules=(deny("run", when=lambda a: "rm" in a.get("cmd", "")),), default=Effect.ALLOW)
    assert p.evaluate("run", {"cmd": "rm -rf"}).effect is Effect.DENY
    assert p.evaluate("run", {"cmd": "ls"}).effect is Effect.ALLOW  # predicate 不符 → 落到 default


# ---- load_policy ----

def test_load_policy_default_deny_all_allow_read_only(tmp_path):
    caps = {"search_documents": Capability.READ_ONLY, "ingest": Capability.WRITE}
    p = load_policy(caps, tmp_path)  # 無 policy.yaml → 預設
    assert p.evaluate("search_documents").effect is Effect.ALLOW
    assert p.evaluate("ingest").effect is Effect.DENY  # write 工具預設被拒
    assert p.evaluate("unknown_tool").effect is Effect.DENY


def test_load_policy_explicit_rules(tmp_path):
    (tmp_path / "policy.yaml").write_text(
        "default: deny\nallow_read_only: true\nrules:\n"
        "  - { effect: ask_user, tool: ingest }\n  - { effect: deny, tool: purge }\n",
        encoding="utf-8",
    )
    caps = {"search_documents": Capability.READ_ONLY, "ingest": Capability.WRITE}
    p = load_policy(caps, tmp_path)
    assert p.evaluate("ingest").effect is Effect.ASK
    assert p.evaluate("purge").effect is Effect.DENY
    assert p.has_specific_rule("ingest")


# ---- guardrail enforcement ----

class _Ctx:
    def __init__(self, name, args="{}"):
        self.tool_name = name
        self.tool_arguments = args


class _Data:
    def __init__(self, name, args="{}"):
        self.context = _Ctx(name, args)


async def test_guardrail_allows_allowed_tool():
    p = Policy(rules=(allow("search_documents"),))
    g = build_policy_guardrail(p)
    out = await g.run(_Data("search_documents"))
    assert out.behavior["type"] == "allow"


async def test_guardrail_rejects_denied_tool():
    p = Policy()  # deny-all
    g = build_policy_guardrail(p)
    out = await g.run(_Data("rm_everything"))
    assert out.behavior["type"] == "reject_content"


# ---- fail-closed 啟動守衛 ----

class _Tool:
    def __init__(self, name):
        self.name = name


def test_startup_guard_raises_for_unruled_write_tool():
    caps = {"ingest": Capability.WRITE}
    policy = Policy(default=Effect.DENY)  # 無 ingest 的明確規則
    with pytest.raises(ValueError, match="ingest"):
        enforce_privileged_need_explicit_rules([_Tool("ingest")], caps, policy)


def test_startup_guard_passes_for_read_only_tools():
    caps = {"search_documents": Capability.READ_ONLY}
    policy = Policy(rules=(allow("search_documents"),))
    enforce_privileged_need_explicit_rules([_Tool("search_documents")], caps, policy)  # no raise


def test_startup_guard_passes_when_write_tool_has_rule():
    caps = {"ingest": Capability.WRITE}
    policy = Policy(rules=(ask_user("ingest"),))
    enforce_privileged_need_explicit_rules([_Tool("ingest")], caps, policy)  # no raise
