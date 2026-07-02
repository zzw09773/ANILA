"""宣告式工具政策 DSL（借自 Antigravity 的優先序桶 + Claude 的 Tool(specifier)）。

純 Python、provider-agnostic。效力依優先序桶評估（specificity + safety 決定先後）：

    Specific Deny > Specific Ask > Specific Allow >
    Wildcard Deny > Wildcard Ask > Wildcard Allow > default

桶內第一個匹配（pattern 命中且 predicate 為真）者勝。**預設 deny**（反轉 Antigravity
的開放預設）——未匹配的工具呼叫一律拒絕，符合軍用/air-gap 強化姿態。

enforcement 餵進單一 SDK tool-input-guardrail（見 policy.guardrail），而非自製 hook runner。
工具能力（read_only/write/admin）來自 tools.capabilities 能力表。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

WILDCARD = "*"


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask_user"


@dataclass(frozen=True)
class Decision:
    effect: Effect
    reason: str


@dataclass(frozen=True)
class Rule:
    effect: Effect
    pattern: str  # 工具名，或 "*"
    when: Callable[[dict], bool] | None = None
    reason: str = ""

    def matches(self, tool_name: str, args: dict, *, wildcard: bool) -> bool:
        is_wild = self.pattern == WILDCARD
        if wildcard != is_wild:
            return False
        if not is_wild and self.pattern != tool_name:
            return False
        return self.when is None or bool(self.when(args))


# 評估順序（桶）：specific 在前、wildcard 在後；桶內 Deny > Ask > Allow。
_BUCKETS: tuple[tuple[bool, Effect], ...] = (
    (False, Effect.DENY),
    (False, Effect.ASK),
    (False, Effect.ALLOW),
    (True, Effect.DENY),
    (True, Effect.ASK),
    (True, Effect.ALLOW),
)


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...] = ()
    default: Effect = Effect.DENY

    def evaluate(self, tool_name: str, args: dict | None = None) -> Decision:
        args = args or {}
        for wildcard, effect in _BUCKETS:
            for rule in self.rules:
                if rule.effect == effect and rule.matches(tool_name, args, wildcard=wildcard):
                    return Decision(effect, rule.reason or f"rule:{rule.pattern}")
        return Decision(self.default, "default")

    def has_specific_rule(self, tool_name: str) -> bool:
        """是否有「明確指名」此工具的規則（非 wildcard）。"""
        return any(r.pattern == tool_name for r in self.rules)


# ---- builders ----

def allow(pattern: str, *, when: Callable[[dict], bool] | None = None, reason: str = "") -> Rule:
    return Rule(Effect.ALLOW, pattern, when, reason)


def deny(pattern: str, *, when: Callable[[dict], bool] | None = None, reason: str = "") -> Rule:
    return Rule(Effect.DENY, pattern, when, reason)


def ask_user(pattern: str, *, when: Callable[[dict], bool] | None = None, reason: str = "") -> Rule:
    return Rule(Effect.ASK, pattern, when, reason)


# ---- loader ----

def load_policy(
    capabilities: dict[str, object],
    config_dir: str | os.PathLike[str] | None = None,
) -> Policy:
    """從 configs/policy.yaml 組 Policy；預設 deny-all + 明列 allow 唯讀工具。"""
    cfg_dir = Path(config_dir) if config_dir is not None else Path("configs")
    path = cfg_dir / "policy.yaml"
    data: dict = {}
    if path.is_file():
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    default = Effect(str(data.get("default", "deny")).strip().lower())
    rules: list[Rule] = []

    # allow_read_only：自動 allow 能力表中標為 read_only 的工具。
    if data.get("allow_read_only", True):
        for name, cap in capabilities.items():
            if getattr(cap, "value", str(cap)) == "read_only":
                rules.append(allow(name, reason="read_only tool"))

    for raw in data.get("rules", []) or []:
        if not isinstance(raw, dict):
            continue
        effect = Effect(str(raw["effect"]).strip().lower())
        pattern = str(raw.get("tool", WILDCARD))
        rules.append(Rule(effect, pattern, None, str(raw.get("reason", ""))))

    return Policy(rules=tuple(rules), default=default)
