"""Policy DSL — 把 tool 控管從「寫 callback」升級成「列宣告」。

本模組對應 enhancement roadmap P0-7:把上游 antigravity SDK
`hooks/policy.py` 的 Policy DSL 拉一層抽象在 P0-6 guardrail 之上,
讓開發者用 declarative rule 設定「哪些 tool 在什麼 ctx 下 allow /
deny / disable」。

設計重點
========

* **三類 effect**(對齊 antigravity APPROVE/DENY 的擴展):
  - ``ALLOW``  — 通過,tool 照常執行。
  - ``DENY``   — 拒絕,attempt 一個 tool call 但回拒絕訊息給 LLM(模型還看得到 tool)。
  - ``DISABLE``— 連讓 LLM 看到 tool 都不要(供 capability filter 用,比 DENY 更強)。

* **優先級**:`PolicyRule.priority`(int,**高 → 低** 排序評估)。
  同 priority 內維持「先註冊先評估」(stable sort)。第一個 match 的 rule
  決定最終結果(short-circuit),沒任何 rule match 時 fallback 為 ALLOW。

* **tool_pattern 三種寫法**:
  - ``None`` 或 ``"*"`` — 全 tool 套用(wildcard)。
  - 純字串(無 regex meta) — 精準字串比對(等同 antigravity ``policy.tool == name``)。
  - ``re.Pattern`` 或字串內含 regex meta(``.``/``*``/``+``/``[`` 等) — regex match。

* **condition**:`Callable[[ctx, tool_name, args], bool]`,真正的 predicate。
  None 表示「name 一旦 match,規則就生效」;非 None 則 ctx 條件也要 True 才算 match。

* **跟 P0-6 guardrail 的關係**:本層是「policy 規則容器」,執行面透過 adapter
  `policy_to_tool_input_guardrail()` 接到 P0-6 的 `ToolInputGuardrail` chain,
  讓 policy engine 結果 map 到 ALLOW / BLOCK / REPLACE_CONTENT。**不修改** P0-6 原檔。

* **跟 P0-3 workspace 邊界的關係**:`workspace_only` factory 用 P0-3 的
  `AnilaToolContext.is_inside_workspace` 或裸 path 判定,做縱深防禦——
  policy 層擋一次,呼叫端 `ctx.safe_path` 再擋一次。

* **YAML 載入**(可選):`PolicyEngine.from_yaml()` 接 5 branch 部署 — 每個
  branch 可有自己 policy yaml(prod-public-passwd 嚴格、dev-public 寬鬆)。

本檔僅依賴 std lib + 既有 P0-3 / P0-6(及可選 pyyaml),不引入 openai-agents
與 antigravity,以維持單元可測。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from anila_agent.core.context import AnilaToolContext
from anila_agent.core.events import EventBus
from anila_agent.core.hooks import HookRegistry, fire_policy_deny
from anila_agent.tools.guardrails import (
    ToolGuardrailResult,
    ToolInputGuardrail,
)

# ---------------------------------------------------------------------------
# 共用型別與常數
# ---------------------------------------------------------------------------

# tool_pattern 可接受字串、編譯後 Pattern、或 None(=全 tool)。
ToolPattern = str | re.Pattern[str] | None

# condition 簽名:(ctx, tool_name, args) -> bool。可為 None。
PolicyCondition = Callable[[Any, str, dict[str, Any]], bool] | None

_WILDCARD = "*"

# 偵測「字串裡是否帶 regex meta 字元」用 — 若帶就視為 regex,否則純字串精準比對。
_REGEX_META_CHARS = re.compile(r"[.\\^$|?*+()\[\]{}]")


# ---------------------------------------------------------------------------
# Effect / Decision 結構
# ---------------------------------------------------------------------------


class PolicyEffect(str, Enum):
    """Policy 評估後可產出的三類結果。

    * ``ALLOW``   — 通過。
    * ``DENY``    — 拒絕但 tool 仍對 LLM 可見;runner 回傳拒絕訊息給模型。
    * ``DISABLE`` — 從 capability list 整個拿掉;LLM 根本看不到該 tool。

    與 P0-6 `ToolGuardrailBehavior` 的對映(由 adapter 處理):
        ALLOW   → ToolGuardrailBehavior.ALLOW
        DENY    → ToolGuardrailBehavior.BLOCK
        DISABLE → ToolGuardrailBehavior.BLOCK + output_info 帶 disabled=True
    """

    ALLOW = "allow"
    DENY = "deny"
    DISABLE = "disable"


# 與 antigravity 的 `Decision` 取相同 name 會跟 hook_taxonomy.Decision 衝突,
# 因此本模組對外用 `PolicyDecision`。
@dataclass(frozen=True)
class PolicyDecision:
    """`PolicyEngine.evaluate()` 的回傳結構。

    Attributes:
        effect:  最終決定的 effect(ALLOW / DENY / DISABLE)。
        reason:  解釋訊息(deny / disable 時必填,供 LLM 與 log)。
        rule_name: 命中的 rule 名稱;`None` 表示沒任何 rule match(走 default)。
    """

    effect: PolicyEffect
    reason: str | None = None
    rule_name: str | None = None

    @property
    def allowed(self) -> bool:
        """是否通過(effect == ALLOW)。"""
        return self.effect is PolicyEffect.ALLOW

    @property
    def denied(self) -> bool:
        """是否被拒絕(DENY 或 DISABLE 都算)。"""
        return self.effect in (PolicyEffect.DENY, PolicyEffect.DISABLE)

    @property
    def disabled(self) -> bool:
        """是否被整個 disable(比 deny 更強的 capability filter)。"""
        return self.effect is PolicyEffect.DISABLE

    @classmethod
    def allow(
        cls, rule_name: str | None = None, reason: str | None = None
    ) -> PolicyDecision:
        """組 ALLOW decision。"""
        return cls(effect=PolicyEffect.ALLOW, reason=reason, rule_name=rule_name)

    @classmethod
    def deny(
        cls, reason: str, rule_name: str | None = None
    ) -> PolicyDecision:
        """組 DENY decision,必帶 reason。"""
        return cls(effect=PolicyEffect.DENY, reason=reason, rule_name=rule_name)

    @classmethod
    def disable(
        cls, reason: str, rule_name: str | None = None
    ) -> PolicyDecision:
        """組 DISABLE decision,必帶 reason。"""
        return cls(effect=PolicyEffect.DISABLE, reason=reason, rule_name=rule_name)


# ---------------------------------------------------------------------------
# PolicyRule
# ---------------------------------------------------------------------------


@dataclass
class PolicyRule:
    """單一 policy rule。對齊 antigravity ``Policy`` 但補上:
    - 三類 effect(多了 ``DISABLE``)
    - 顯式 ``priority``(取代上游 6 個固定 bucket)
    - 接受 `re.Pattern` 直接做 regex match

    Attributes:
        name: 規則名稱,出現在 log、deny reason 內。
        tool_pattern: 對哪些 tool 套用。
            * ``None`` / ``"*"``  → 全 tool。
            * 純字串(無 regex meta) → 精準字串比對。
            * `re.Pattern` 或含 regex meta 的字串 → regex match。
        effect: 命中時的 effect(ALLOW / DENY / DISABLE)。
        priority: 優先級;**數字越大越先評估**。預設 100。
        condition: 額外 predicate `(ctx, tool_name, args) -> bool`;`None` 表示
            只看 tool name match 就算命中。
        reason: deny / disable 時的解釋訊息;ALLOW 可不填。

    註:本 dataclass 不 frozen — `_compiled_pattern` 由 `__post_init__` 寫入快取。
    使用者建立後不應再改其它欄位。
    """

    name: str
    tool_pattern: ToolPattern = None
    effect: PolicyEffect = PolicyEffect.ALLOW
    priority: int = 100
    condition: PolicyCondition = None
    reason: str | None = None

    # 內部:把 tool_pattern compile 成 (mode, value),減少每次 evaluate 都判斷的成本。
    # mode ∈ {"any", "exact", "regex"}。
    _compiled_pattern: tuple[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """把 `tool_pattern` 預先解析,加速後續 match。

        規則:
        1. ``None`` 或字串 ``"*"`` → mode="any"。
        2. `re.Pattern` → mode="regex",原樣保留。
        3. 字串含 regex meta(``.``/``*``/``+``/``[``…) → mode="regex",compile 起來。
        4. 純字串 → mode="exact",精準比對。
        """
        pat = self.tool_pattern
        if pat is None or pat == _WILDCARD:
            self._compiled_pattern = ("any", None)
        elif isinstance(pat, re.Pattern):
            self._compiled_pattern = ("regex", pat)
        elif isinstance(pat, str):
            if _REGEX_META_CHARS.search(pat):
                self._compiled_pattern = ("regex", re.compile(pat))
            else:
                self._compiled_pattern = ("exact", pat)
        else:  # pragma: no cover — type checker 已擋住
            raise TypeError(
                f"tool_pattern must be str, re.Pattern, or None; got {type(pat)!r}"
            )

        # deny / disable 沒給 reason 時補一個預設,避免 LLM 拿到空訊息。
        if self.reason is None and self.effect is not PolicyEffect.ALLOW:
            self.reason = f"blocked by policy rule {self.name!r}"

    def matches_tool(self, tool_name: str) -> bool:
        """檢查 `tool_name` 是否符合本 rule 的 tool_pattern(不看 condition)。"""
        mode, value = self._compiled_pattern
        if mode == "any":
            return True
        if mode == "exact":
            return value == tool_name
        if mode == "regex":
            assert isinstance(value, re.Pattern)
            # 用 fullmatch 比 search/match 嚴格,避免 "file" pattern 命中 "filesys";
            # 呼叫端要寬鬆比對請自行寫 `.*file.*`。
            return value.fullmatch(tool_name) is not None
        return False  # pragma: no cover

    def matches(
        self,
        ctx: Any,
        tool_name: str,
        args: dict[str, Any],
    ) -> bool:
        """完整 match — 先看 tool name,再跑 condition。"""
        if not self.matches_tool(tool_name):
            return False
        if self.condition is None:
            return True
        try:
            return bool(self.condition(ctx, tool_name, args))
        except Exception:
            # 任一 condition 丟例外都 fail-closed:視為「不 match」,讓後續 rule 接手。
            # 不在這層直接 deny,避免一個 bug condition 把整支 agent 卡死;
            # 預設 fallback 為 ALLOW,需要 deny 請另外寫一條 fallback rule。
            return False


# ---------------------------------------------------------------------------
# PolicyEngine
# ---------------------------------------------------------------------------


class PolicyEngine:
    """Policy 規則集容器 + 評估器。

    使用方式::

        engine = PolicyEngine()
        engine.add_rule(workspace_only([Path("/work")]))
        engine.add_rule(read_only_factory := ...)  # see factories below

        decision = engine.evaluate(ctx, "file.write", {"path": "/etc/passwd"})
        if decision.denied:
            ...  # 拒絕並回 reason 給 LLM

    評估順序:
    1. rules 依 priority(高→低)排序;同 priority 內維持註冊順序。
    2. 從第一條 rule 開始 walk,第一個 `matches()` 的 rule 決定 effect(short-circuit)。
    3. 沒任何 rule match → 預設 ALLOW(對應 antigravity 的「default open」)。

    用 ``add_rule`` 加入後會 invalidate 內部排序快取;evaluate 時若 dirty 會再排一次。
    """

    def __init__(self) -> None:
        """建空 engine。"""
        self._rules: list[PolicyRule] = []
        self._sorted_dirty: bool = True

    # ---- 變更介面 --------------------------------------------------------

    def add_rule(self, rule: PolicyRule) -> None:
        """加入一條 rule。後續 evaluate 會重新排序。"""
        self._rules.append(rule)
        self._sorted_dirty = True

    def add_rules(self, rules: Sequence[PolicyRule]) -> None:
        """批次加入多條 rule。等同於 loop 呼叫 ``add_rule``。"""
        for r in rules:
            self.add_rule(r)

    def clear(self) -> None:
        """清空所有 rule,主要供測試。"""
        self._rules.clear()
        self._sorted_dirty = True

    # ---- 查詢 ------------------------------------------------------------

    @property
    def rules(self) -> list[PolicyRule]:
        """回傳目前 rule 列表的副本(依註冊順序;未排序)。"""
        return list(self._rules)

    def sorted_rules(self) -> list[PolicyRule]:
        """回傳依 priority 高→低排序、同 priority 維持註冊序的 rule 列表。

        Python ``sorted`` 是 stable sort,只以 ``-priority`` 為 key 即可保留註冊順序。
        """
        if self._sorted_dirty:
            self._rules.sort(key=lambda r: -r.priority)
            self._sorted_dirty = False
        return list(self._rules)

    # ---- evaluate --------------------------------------------------------

    def evaluate(
        self,
        ctx: Any,
        tool_name: str,
        args: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """評估 (ctx, tool_name, args) 在當前 rule set 下的決定。

        Args:
            ctx: tool 執行情境,通常為 ``AnilaToolContext`` 但任何物件都收。
            tool_name: 要評估的 tool 名稱。
            args: tool args(dict);None 等同 ``{}``。

        Returns:
            ``PolicyDecision`` — effect / reason / rule_name 三欄。

        Semantics:
            walk sorted rules,第一個 ``matches()`` 的 rule 直接決定 effect。
            沒 rule match → 預設 ``ALLOW``(rule_name=None,reason=None)。
        """
        actual_args = args or {}
        for rule in self.sorted_rules():
            if rule.matches(ctx, tool_name, actual_args):
                if rule.effect is PolicyEffect.ALLOW:
                    return PolicyDecision.allow(
                        rule_name=rule.name, reason=rule.reason
                    )
                if rule.effect is PolicyEffect.DENY:
                    return PolicyDecision.deny(
                        reason=rule.reason or f"denied by {rule.name!r}",
                        rule_name=rule.name,
                    )
                # DISABLE
                return PolicyDecision.disable(
                    reason=rule.reason or f"disabled by {rule.name!r}",
                    rule_name=rule.name,
                )
        # default open
        return PolicyDecision.allow()

    async def aevaluate(
        self,
        ctx: Any,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        hook_registry: HookRegistry | None = None,
        event_bus: EventBus | None = None,
    ) -> PolicyDecision:
        """P1-17 async 版本 ``evaluate``:DENY / DISABLE 時 fire ``POLICY_DENY`` hook。

        行為跟 ``evaluate`` 一致(回相同 :class:`PolicyDecision`),差別只在多了
        hook fire 點。``hook_registry=None`` 時退化為等同於 ``evaluate``。
        Hook 不影響 decision,只是觀察點(audit / alerting)。
        """
        decision = self.evaluate(ctx, tool_name, args)
        if decision.effect in (PolicyEffect.DENY, PolicyEffect.DISABLE):
            await fire_policy_deny(
                hook_registry,
                event_bus,
                tool_name=tool_name,
                rule_name=decision.rule_name,
                effect=decision.effect.value,
                reason=decision.reason,
            )
        return decision

    # ---- YAML 載入(可選) -----------------------------------------------

    @classmethod
    def from_yaml(cls, path: Path | str) -> PolicyEngine:
        """從 YAML 設定檔載入 policy。

        Format(對齊 5 branch 部署的單檔結構)::

            rules:
              - name: deny_destructive
                tool_pattern: "*"
                effect: deny
                priority: 1000
                reason: "destructive tools blocked on prod"

              - name: allow_read_only
                tool_pattern: "read_.*"
                effect: allow
                priority: 500

              - name: deny_all_fallback
                effect: deny
                priority: 0
                reason: "default-deny posture"

        Args:
            path: YAML 檔路徑。

        Returns:
            載入完成的 PolicyEngine。

        Raises:
            FileNotFoundError: 檔案不存在。
            ValueError: YAML 結構不正確(缺 `name` / `effect` 等)。
            ImportError: 系統未安裝 pyyaml。
        """
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "pyyaml is required for PolicyEngine.from_yaml(); "
                "install via `pip install pyyaml`"
            ) from e

        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}

        rules_data = data.get("rules", [])
        if not isinstance(rules_data, list):
            raise ValueError(
                f"'rules' must be a list in {path!s}; got {type(rules_data).__name__}"
            )

        engine = cls()
        for idx, item in enumerate(rules_data):
            if not isinstance(item, dict):
                raise ValueError(
                    f"rule #{idx} in {path!s} must be a mapping; got {type(item).__name__}"
                )
            engine.add_rule(_rule_from_dict(item, source=str(path), index=idx))
        return engine


def _rule_from_dict(
    item: dict[str, Any],
    *,
    source: str,
    index: int,
) -> PolicyRule:
    """把 YAML dict 轉成 PolicyRule;欄位驗證集中於此。"""
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"rule #{index} in {source}: missing required field 'name'"
        )

    raw_effect = item.get("effect", "allow")
    try:
        effect = PolicyEffect(raw_effect)
    except ValueError as e:
        raise ValueError(
            f"rule {name!r} in {source}: invalid effect {raw_effect!r}; "
            f"must be one of {[e.value for e in PolicyEffect]}"
        ) from e

    tool_pattern = item.get("tool_pattern")  # 預期是 str 或 None
    priority = int(item.get("priority", 100))
    reason = item.get("reason")
    return PolicyRule(
        name=name,
        tool_pattern=tool_pattern,
        effect=effect,
        priority=priority,
        reason=reason,
        # condition 不從 YAML 載入(不允許執行任意 Python 表達式,安全考量)。
    )


# ---------------------------------------------------------------------------
# 常用 policy factory
# ---------------------------------------------------------------------------


def deny_all(
    *,
    name: str = "deny_all",
    reason: str = "denied by default deny_all policy",
    priority: int = 0,
) -> PolicyRule:
    """組「全部 tool 一律 deny」的 fallback rule。

    priority 預設 ``0`` — 故意設成最低,讓使用者用更高 priority 的 ``allow_*``
    rule override 特定 tool。配合 antigravity 的「deny-by-default + 特定 allow」
    安全姿態。

    Args:
        name: rule 名稱,出現在 deny reason 中。
        reason: 拒絕訊息。
        priority: 預設 0(最低)。一般不需改。

    Returns:
        ``PolicyRule``,effect=DENY,tool_pattern=None(全 tool)。
    """
    return PolicyRule(
        name=name,
        tool_pattern=None,
        effect=PolicyEffect.DENY,
        priority=priority,
        reason=reason,
    )


def allow_all_except(
    *tool_names: str,
    name_prefix: str = "deny",
    reason: str | None = None,
    priority: int = 500,
) -> list[PolicyRule]:
    """對指定 tool name 全部 deny,其他 tool 允許。

    產出兩類 rule:
    1. 每個 ``tool_names`` 各一條 priority=priority 的 ``DENY`` rule(精準字串比對)。
    2. 一條 priority=`priority - 100` 的 wildcard ``ALLOW`` rule(讓沒命中 deny 的 tool 通過)。

    Args:
        *tool_names: 要 deny 的 tool 名稱(精準字串)。
        name_prefix: 產出的 deny rule 名稱前綴。
        reason: deny 時的解釋訊息;None 時自動帶 "tool {name} is not allowed"。
        priority: deny rule 的優先級。allow wildcard 比它低 100。

    Returns:
        rule 列表,可直接餵 ``PolicyEngine.add_rules()``。
    """
    rules: list[PolicyRule] = []
    for tool in tool_names:
        rules.append(
            PolicyRule(
                name=f"{name_prefix}_{tool}",
                tool_pattern=tool,
                effect=PolicyEffect.DENY,
                priority=priority,
                reason=reason or f"tool {tool!r} is not allowed by policy",
            )
        )
    rules.append(
        PolicyRule(
            name=f"{name_prefix}_allow_others",
            tool_pattern=None,
            effect=PolicyEffect.ALLOW,
            priority=priority - 100,
        )
    )
    return rules


# 動到 file system 的 tool 名稱(prefix / 完整名稱)— workspace_only 套用範圍。
# 對齊 antigravity `BuiltinTools.file_tools()` 與 anila tools/filesystem_tools.py。
_DEFAULT_FILE_TOOL_NAMES: tuple[str, ...] = (
    "file.read",
    "file.write",
    "file.edit",
    "file.delete",
    "file.create",
    "file.list",
    "filesystem.read",
    "filesystem.write",
    "filesystem.edit",
    "filesystem.delete",
    "shell.run",
    "shell.exec",
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "list_dir",
    "run_command",
)

# args 中可能裝 path 的 key 名稱(loose match,有命中就抽出來檢查)。
_PATH_ARG_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "filepath",
    "filename",
    "src",
    "dst",
    "source",
    "destination",
    "target",
)


def _extract_paths_from_args(args: dict[str, Any]) -> list[str]:
    """從 args dict 撈出所有可能是 path 的字串值。

    規則:
    - 若 key 在 `_PATH_ARG_KEYS` 內,且 value 是 str / Path,收集起來。
    - 同時撿 list[str](例如 `paths=[...]`)。

    回傳空 list 表示「args 中沒明確的 path」,呼叫端可決定要 allow(無風險)
    還是 deny(保守);本模組 workspace_only condition 採「無 path 視為 allow」,
    對齊 antigravity `_outside_workspace` 在 ``not path`` 時回 False 的設計。
    """
    found: list[str] = []
    for key, value in args.items():
        if key not in _PATH_ARG_KEYS and not key.endswith("_path"):
            continue
        if isinstance(value, (str, Path)):
            found.append(str(value))
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, (str, Path)):
                    found.append(str(v))
    return found


def workspace_only(
    allowed_roots: Sequence[Path | str],
    *,
    name: str = "workspace_only",
    tool_names: Sequence[str] | None = None,
    priority: int = 800,
) -> PolicyRule:
    """組「file system 類 tool 路徑必須在 allowed_roots 之下」的 policy。

    動到 file system 的 tool(write / delete / shell.run 等)會把 args 內的 path
    抽出來檢查;若任一 path **不在** ``allowed_roots`` 任一 root 之下,就 deny。

    與 P0-3 的關係:
        本 policy 是縱深防禦的**外層**——在 tool 還沒被執行前先擋。
        P0-3 `AnilaToolContext.safe_path` 是 tool 內主動防禦的**內層**。
        兩層都要保留,避免單點失效。

    Args:
        allowed_roots: 允許的 workspace root path list(可給 str / Path)。
            空 list 等同「沒任何路徑被允許」→ 等於 deny 所有 file tool。
        name: rule 名稱。
        tool_names: 要套用的 tool 名稱;None 表示用內建 file tool 名單。
        priority: 預設 800(比 deny_all 高很多,確保命中)。

    Returns:
        一條 ``PolicyRule``,effect=DENY,condition 為「path 是否 escape workspace」。
    """
    roots: list[Path] = [Path(r).resolve() for r in allowed_roots]
    target_names: set[str] = set(tool_names) if tool_names else set(_DEFAULT_FILE_TOOL_NAMES)

    def _condition(ctx: Any, tool_name: str, args: dict[str, Any]) -> bool:
        """命中條件 = 「該 tool 有 path 跳脫 allowed_roots」。"""
        # 只對 file 類 tool 套用;其它 tool 一律不命中(讓後續 rule 接手)。
        if tool_name not in target_names:
            return False

        paths = _extract_paths_from_args(args)
        if not paths:
            # 無 path 視為合法(對齊 antigravity 設計),交給 tool 自己處理 cwd default。
            return False

        for raw in paths:
            if not _is_path_in_any_root(ctx, raw, roots):
                return True  # 命中 = 有路徑逃脫
        return False

    return PolicyRule(
        name=name,
        tool_pattern=None,  # 我們在 condition 內篩 tool_name,wildcard 也行
        effect=PolicyEffect.DENY,
        priority=priority,
        condition=_condition,
        reason="path escapes allowed workspace roots",
    )


def _is_path_in_any_root(
    ctx: Any,
    raw_path: str,
    roots: Sequence[Path],
) -> bool:
    """檢查 raw_path 是否在 roots 任一 root 之下。

    優先順序:
    1. 若 ctx 是 `AnilaToolContext` 且 workspace 與 roots 任一相符,用 ctx 自帶
       的 `is_inside_workspace`(可繼承 ctx 的 case-insensitive 處理)。
    2. 否則自行做 `Path.resolve()` + `Path.is_relative_to` 比對。
    3. 任何 OSError(symlink 無權限、disk gone)一律 fail-closed:回 False。
    """
    try:
        candidate = Path(raw_path)
        if not candidate.is_absolute() and isinstance(ctx, AnilaToolContext) and ctx.workspace:
            candidate = ctx.workspace / candidate
        resolved = candidate.resolve()
    except OSError:
        return False

    for root in roots:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except OSError:
            continue
    return False


def read_only(
    *,
    name: str = "read_only",
    metadata_lookup: Callable[[str], Any] | None = None,
    destructive_tool_names: Sequence[str] | None = None,
    priority: int = 700,
    reason: str = "destructive tools are not allowed in read-only mode",
) -> list[PolicyRule]:
    """組「read-only mode」policy:把所有 destructive tool deny。

    判定 destructive 的方式(任一即可):
    1. ``destructive_tool_names`` 顯式列出的 tool 名稱(precision)。
    2. ``metadata_lookup(tool_name)`` 回傳的 metadata `.is_destructive == True`
       (對應 P0-2 ToolMetadata)。可傳入 `registry.get_metadata`。

    Args:
        name: rule 名稱前綴。
        metadata_lookup: 給定 tool_name 回 ToolMetadata 的查詢 callable;通常是
            ``ToolRegistry.get_metadata``。可為 None。
        destructive_tool_names: 顯式 destructive tool 名稱清單。可為 None。
        priority: 預設 700。
        reason: deny 訊息。

    Returns:
        rule 列表(可能多條,每個 destructive tool 一條)。如果 ``metadata_lookup``
        與 ``destructive_tool_names`` 都沒給,就回 wildcard deny rule(全 tool deny),
        因為「read-only 又不告訴我哪些是 destructive」最安全做法是全擋。
    """
    rules: list[PolicyRule] = []
    seen: set[str] = set()

    if destructive_tool_names:
        for tool in destructive_tool_names:
            if tool in seen:
                continue
            seen.add(tool)
            rules.append(
                PolicyRule(
                    name=f"{name}_{tool}",
                    tool_pattern=tool,
                    effect=PolicyEffect.DENY,
                    priority=priority,
                    reason=reason,
                )
            )

    if metadata_lookup is not None:
        # 把 metadata_lookup 包進 condition,讓 wildcard rule 在 evaluate 時動態決定。
        def _is_destructive(ctx: Any, tool_name: str, args: dict[str, Any]) -> bool:
            try:
                meta = metadata_lookup(tool_name)
            except Exception:
                return False
            return bool(getattr(meta, "is_destructive", False))

        rules.append(
            PolicyRule(
                name=f"{name}_metadata",
                tool_pattern=None,
                effect=PolicyEffect.DENY,
                priority=priority,
                condition=_is_destructive,
                reason=reason,
            )
        )

    if not rules:
        # 都沒給 → 全 tool 一律 deny。
        rules.append(
            PolicyRule(
                name=f"{name}_strict",
                tool_pattern=None,
                effect=PolicyEffect.DENY,
                priority=priority,
                reason=reason,
            )
        )
    return rules


# ---------------------------------------------------------------------------
# 與 P0-6 ToolInputGuardrail 的 adapter
# ---------------------------------------------------------------------------


def policy_to_tool_input_guardrail(
    policy_engine: PolicyEngine,
    *,
    name: str = "policy_engine",
    disable_as_block: bool = True,
) -> ToolInputGuardrail[Any]:
    """把 ``PolicyEngine`` 適配成 ``ToolInputGuardrail``,可掛進 P0-6 chain。

    映射規則:
        PolicyEffect.ALLOW   → ``ToolGuardrailResult.allow()``
        PolicyEffect.DENY    → ``ToolGuardrailResult.block({...})``
        PolicyEffect.DISABLE → ``ToolGuardrailResult.block({...,"disabled":True})``
                               (或視 `disable_as_block` 而定 — False 時等同 deny)

    本 adapter **不直接 raise tripwire**;P0-6 `ToolInputGuardrail.enforce()`
    才會在 BLOCK 時 raise,維持單一例外起點。

    Args:
        policy_engine: 設定好 rule 的 engine。
        name: 包出來的 guardrail 名稱(出現在 log / tripwire info)。
        disable_as_block: DISABLE 是否視同 BLOCK。預設 True;若上層想分開處理
            (例如把 DISABLE 變成 capability filter 而不是 BLOCK),設 False。

    Returns:
        ``ToolInputGuardrail`` 實例,可直接加入 P0-6 guardrail chain。
    """

    def _adapter(
        ctx: Any, tool_name: str, args: dict[str, Any]
    ) -> ToolGuardrailResult:
        """ToolInputGuardrail 的 check function — 走 policy engine 後 map 結果。"""
        decision = policy_engine.evaluate(ctx, tool_name, args)
        info: dict[str, Any] = {
            "policy_effect": decision.effect.value,
            "policy_rule": decision.rule_name,
            "tool_name": tool_name,
        }
        if decision.reason:
            info["reason"] = decision.reason

        if decision.effect is PolicyEffect.ALLOW:
            return ToolGuardrailResult.allow(output_info=info)

        if decision.effect is PolicyEffect.DENY:
            return ToolGuardrailResult.block(output_info=info)

        # DISABLE
        info["disabled"] = True
        if disable_as_block:
            return ToolGuardrailResult.block(output_info=info)
        # 視為 allow,讓上層 capability filter 自行處理(不執行 tool 但不丟例外)。
        return ToolGuardrailResult.allow(output_info=info)

    return ToolInputGuardrail(guardrail_function=_adapter, name=name)


__all__ = [
    # 結構
    "PolicyDecision",
    "PolicyEffect",
    "PolicyEngine",
    "PolicyRule",
    # factory
    "allow_all_except",
    "deny_all",
    "read_only",
    "workspace_only",
    # adapter
    "policy_to_tool_input_guardrail",
]
