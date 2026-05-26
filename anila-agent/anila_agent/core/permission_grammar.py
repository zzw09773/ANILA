"""Permission rule mini DSL — 把 `Verb(arg_pattern)` 字串編譯成 `PolicyRule`。

本模組對應 enhancement roadmap P1-16:在 P0-7 `PolicyEngine` 之上再加一層
**人類可讀的 rule 字串語法**,讓 user / settings.yaml 不必直接撰寫
Python `PolicyRule(tool_pattern=..., condition=..., effect=...)`,改寫
類 Claude Code 的 `Verb(arg_pattern)` 形式::

    "Read(**)"              # 允許讀所有 path
    "Write(/workspace/**)"   # 允許 workspace 內 write
    "Bash(rm -rf *)"  deny  # 禁止 rm -rf
    "Bash(*)"         deny  # 禁止所有 shell

這層只負責「字串 → `PermissionRule` → `PolicyRule`」,policy 評估邏輯與
ToolInputGuardrail adapter 全部走 P0-7 既有路徑,確保兩層 API 對齊。

設計重點
========

* **Grammar(故意保持簡單)**:

  ```
  rule        := VERB | VERB "(" arg_pattern ")"
  VERB        := identifier(英數+底線) 或 "*"
  arg_pattern := 任意字元,支援巢狀括號(由 depth counter 追)及
                 backslash escape(``\\(`` / ``\\)`` / ``\\\\``)
  ```

  決策:**不**支援完整 lexer / AST,只夠應付 Claude Code 上游與 P0-7
  常見 case;若 user 需要進階語法(quoted string、shell ops),請走純
  Python `PolicyRule` API。

* **Verb → tool name 比對**:預設 `Verb` 對應 P0-7 `tool_pattern` 為
  ``re.Pattern``,case-insensitive 匹配 `verb` 與 `verb\\..+`(允許
  `Read` 命中 `read`、`Read.file`、`file.read` 全套寫法,對齊 anila
  tools 的多種命名)。可透過 `verb_to_tool_pattern` 覆寫客製。

* **arg_pattern → condition**:預設用 `fnmatch.translate` 把 glob 轉
  regex,然後 condition 把 args 內 path / cmd 字串撈出來 match。`**`
  視為「跨層級 path」(對齊 git / fnmatch ext 的 `**` 行為),`*`
  限 single segment(`/` 不跨)。Bash verb 走 cmdline 比對,其他走 path。

* **與 P0-7 yaml 的整合**:支援兩種寫法共存,**舊格式不破壞**:

  ```yaml
  rules:
    # 新 sugar:permission 字串直接展開
    - name: allow_read_all
      permission: "Read(**)"
      effect: allow         # optional;預設 allow
      priority: 600

    # 舊格式照常運作
    - name: deny_outside_workspace
      tool_pattern: "file\\.write"
      effect: deny
      priority: 800
  ```

  整合是 **non-invasive** — 透過 `extend_policy_engine_yaml_loader()`
  動態 monkey-patch `PolicyEngine.from_yaml`,不修改 P0-7 原檔。

* **錯誤處理**:`PermissionRuleSyntaxError` 是 `ValueError` 子型別,
  方便上層 broad except 接,也方便 isinstance 判斷。

本檔只依賴 std lib(`re`、`fnmatch`)+ P0-7 `policy.py`。
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from anila_agent.core.policy import (
    PolicyEffect,
    PolicyEngine,
    PolicyRule,
)

# ---------------------------------------------------------------------------
# 例外
# ---------------------------------------------------------------------------


class PermissionRuleSyntaxError(ValueError):
    """permission rule 字串語法錯誤(grammar 不合法)。

    繼承 ``ValueError`` 讓上層可同時 catch 標準 value error;也可單獨
    `isinstance(e, PermissionRuleSyntaxError)` 區分。

    Attributes:
        rule_str: 出問題的原始字串。
        reason: 人類可讀錯誤理由(會自動串成 message)。
        position: 字串中錯誤位置(0-based index;不明時 None)。
    """

    def __init__(
        self,
        rule_str: str,
        reason: str,
        *,
        position: int | None = None,
    ) -> None:
        """建立例外。

        Args:
            rule_str: 原始 rule 字串。
            reason: 錯誤理由(英文 / 中文皆可,只給開發者看)。
            position: 失敗 char 位置(若可定位)。
        """
        self.rule_str = rule_str
        self.reason = reason
        self.position = position
        pos_suffix = f" (at position {position})" if position is not None else ""
        super().__init__(f"invalid permission rule {rule_str!r}: {reason}{pos_suffix}")


# ---------------------------------------------------------------------------
# 常數 / 型別
# ---------------------------------------------------------------------------

EffectStr = Literal["allow", "deny", "disable"]

# verb 合法 charset:英數 + 底線 + dot(允許 `Read.file` 這種)或單純 "*"。
# 第一字必須是字母或 "*"(避免 `(foo)` 這種開頭被當成 valid)。
_VERB_PATTERN = re.compile(r"^(?:\*|[A-Za-z][A-Za-z0-9_.]*)$")

# 預設 verb → tool name pattern 對映;case-insensitive。
# 用 regex 取代精準字串,讓 `Read` 同時 cover `read` / `read_file` / `file.read`。
_DEFAULT_VERB_ALIASES: dict[str, str] = {
    # file ops
    "read": r"(?i)(?:read|read_.*|file\.read|.*\.read|filesystem\.read)",
    "write": r"(?i)(?:write|write_.*|file\.write|.*\.write|filesystem\.write|edit|edit_.*|file\.edit)",
    "edit": r"(?i)(?:edit|edit_.*|file\.edit|.*\.edit)",
    "delete": r"(?i)(?:delete|delete_.*|file\.delete|.*\.delete|filesystem\.delete)",
    "list": r"(?i)(?:list|list_.*|ls|file\.list|.*\.list)",
    # shell
    "bash": r"(?i)(?:bash|shell|shell\..+|run_command|run_shell)",
    "shell": r"(?i)(?:bash|shell|shell\..+|run_command|run_shell)",
    # network
    "fetch": r"(?i)(?:fetch|web_fetch|http\..+|net\..+)",
    "webfetch": r"(?i)(?:fetch|web_fetch|http\..+|net\..+)",
    # agent / sub-routine
    "agent": r"(?i)(?:agent|agent\..+|task|sub_agent)",
    "task": r"(?i)(?:agent|agent\..+|task|sub_agent)",
}

# Bash 類 verb — 看 cmdline 字串而非 path。
_SHELL_VERBS: frozenset[str] = frozenset({"bash", "shell"})

# args 中常見 cmdline key(loose match)。
_CMDLINE_ARG_KEYS: tuple[str, ...] = ("cmd", "command", "shell", "script", "args")

# args 中常見 path key(對齊 P0-7 `_PATH_ARG_KEYS`,本檔不 import 避免耦合)。
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


# ---------------------------------------------------------------------------
# PermissionRule dataclass
# ---------------------------------------------------------------------------


@dataclass
class PermissionRule:
    """parse 後的 permission rule;封裝 verb / arg_pattern / effect 三欄。

    本結構是「mini DSL 的中間表示」,不直接被 PolicyEngine 評估;呼叫
    `to_policy_rule()` 轉成 P0-7 `PolicyRule` 才能加進 engine。

    Attributes:
        verb: 動詞(如 ``"Read"`` / ``"Write"`` / ``"Bash"`` / ``"*"``)。
            一律先 lowercase 化以利後續比對。
        arg_pattern: glob 形式的 argument pattern;``None`` 代表沒帶括號
            的 rule(等同「verb 命中即允許/拒絕」)。
        effect: ``"allow"`` / ``"deny"`` / ``"disable"``。
        priority: 對應 P0-7 `PolicyRule.priority`;預設 100。
        name: rule 名稱,出現在 log 與 deny reason;若 None 自動產出。
        verb_alias_map: 覆寫預設的 verb→regex 對映(進階)。`None` 時走
            模組級 `_DEFAULT_VERB_ALIASES`。
        reason: deny / disable 訊息;None 時自動帶 "blocked by ..."。
    """

    verb: str
    arg_pattern: str | None = None
    effect: EffectStr = "allow"
    priority: int = 100
    name: str | None = None
    verb_alias_map: dict[str, str] | None = None
    reason: str | None = None

    # 內部:後處理結果。
    _normalized_verb: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """規範化 verb(lowercase)並驗證 effect。"""
        if not isinstance(self.verb, str) or not self.verb:
            raise PermissionRuleSyntaxError("", "verb must be a non-empty string")
        if self.effect not in ("allow", "deny", "disable"):
            raise PermissionRuleSyntaxError(
                self.verb,
                f"invalid effect {self.effect!r}; "
                f"must be one of allow / deny / disable",
            )
        # arg_pattern 為空字串 / "*" 視同無限制(對齊 Claude Code 上游
        # `Bash()` / `Bash(*)` 都當成「整個 verb 通用」)。
        if self.arg_pattern == "" or self.arg_pattern == "*":
            self.arg_pattern = None
        self._normalized_verb = self.verb.lower()

    # ---- helpers --------------------------------------------------------

    @property
    def normalized_verb(self) -> str:
        """lowercase 後的 verb,給比對用。"""
        return self._normalized_verb

    def _resolve_tool_pattern(self) -> str | None:
        """把 verb 解析成 P0-7 `tool_pattern`(regex 字串或 None)。

        Returns:
            * ``None`` — verb 為 "*"(全 tool wildcard)。
            * regex 字串 — 其它情境(以 `_DEFAULT_VERB_ALIASES` 為基底,
              fallback 為 `(?i)<verb>(?:\\..+)?` 對齊「verb 命中其本身
              或 `verb.subname`」)。
        """
        verb = self._normalized_verb
        if verb == "*":
            return None
        alias_map = (
            self.verb_alias_map
            if self.verb_alias_map is not None
            else _DEFAULT_VERB_ALIASES
        )
        if verb in alias_map:
            return alias_map[verb]
        # fallback:精準命中 verb 本身或 `verb.subname`(case-insensitive)。
        escaped = re.escape(verb)
        return rf"(?i){escaped}(?:\..+)?"

    def _compile_arg_regex(self) -> re.Pattern[str] | None:
        """把 arg_pattern 從 glob 轉成 regex Pattern。

        ``**`` 視為跨 segment(任意字元,含 ``/``);``*`` 限 single segment
        (不跨 ``/``)。利用兩段 transform 而非直接 `fnmatch.translate`,
        因為 `fnmatch` 沒區分 `*` / `**`。
        """
        if self.arg_pattern is None:
            return None
        # 占位符替換 — 避免 ** / * 交互 transform 出錯。
        # 1. 先把 ** 換成 ASCII 不會用到的 sentinel。
        sentinel = "\x00\x00"
        pat = self.arg_pattern.replace("**", sentinel)
        # 2. fnmatch.translate 把 * 轉成 [^/]*(自行手動,因為 stdlib 是 .*)。
        #    這裡走簡化版:先 fnmatch,再回頭把對應 `.` patch 掉。
        regex = fnmatch.translate(pat)
        # fnmatch.translate 把 `*` 變 `(?s:.*)`;將其改成 `(?s:[^/]*)`。
        # 由於 stdlib 形式可能變(Py 3.12 vs 3.13),只 patch 已知形式。
        regex = regex.replace("(?s:.*)", "(?s:[^/]*)")
        # 把 sentinel 換回真正的「任意字元含 /」。
        regex = regex.replace(sentinel, ".*")
        return re.compile(regex)

    # ---- 轉 PolicyRule ---------------------------------------------------

    def to_policy_rule(self) -> PolicyRule:
        """轉成 P0-7 `PolicyRule`,可直接餵 `PolicyEngine.add_rule`。

        實作細節:
        * `tool_pattern` 由 `_resolve_tool_pattern()` 推得(regex 或 None)。
        * `condition` 由 `arg_pattern` 編譯而成:
            - Bash 類 verb → 撈 args 中 cmdline 字串做 fullmatch。
            - 其它 verb → 撈 args 中 path 字串做 fullmatch。
            - `arg_pattern` 為 None → condition 為 None(verb 命中即生效)。
        * `effect` 直接 map 到 `PolicyEffect`。
        * `priority` / `reason` 直接帶過。

        Returns:
            `PolicyRule` 實例。
        """
        tool_pattern = self._resolve_tool_pattern()
        arg_regex = self._compile_arg_regex()
        is_shell = self._normalized_verb in _SHELL_VERBS
        condition = _build_condition(arg_regex, is_shell)

        effect_enum = {
            "allow": PolicyEffect.ALLOW,
            "deny": PolicyEffect.DENY,
            "disable": PolicyEffect.DISABLE,
        }[self.effect]

        rule_name = self.name or f"perm_{self._normalized_verb}_{self.effect}"

        return PolicyRule(
            name=rule_name,
            tool_pattern=tool_pattern,
            effect=effect_enum,
            priority=self.priority,
            condition=condition,
            reason=self.reason,
        )


def _build_condition(
    arg_regex: re.Pattern[str] | None,
    is_shell: bool,
):
    """把 arg_regex 包成 `PolicyRule.condition` callable。

    Returns:
        Callable `(ctx, tool_name, args) -> bool`;若 arg_regex 為 None
        直接回 None(讓 `PolicyRule` 走「verb 命中即生效」)。
    """
    if arg_regex is None:
        return None

    keys = _CMDLINE_ARG_KEYS if is_shell else _PATH_ARG_KEYS

    def _condition(ctx: Any, tool_name: str, args: dict[str, Any]) -> bool:
        """檢查 args 內任一相關欄位是否被 arg_regex 完整命中。"""
        candidates = _extract_candidate_strings(args, keys)
        if not candidates:
            # 沒明確 path / cmdline → 視為「無從判斷」=不命中
            # (對齊 P0-7 `_extract_paths_from_args` 的保守設計)。
            return False
        for s in candidates:
            if arg_regex.fullmatch(s):
                return True
        return False

    return _condition


def _extract_candidate_strings(
    args: dict[str, Any],
    keys: Sequence[str],
) -> list[str]:
    """從 args 撈出所有可能的字串候選(loose match key 名)。"""
    found: list[str] = []
    keys_set = set(keys)
    for key, value in args.items():
        # 允許 `*_path` 也算 path key(對齊 P0-7)。
        if key in keys_set or (
            not _is_shell_keys(keys) and isinstance(key, str) and key.endswith("_path")
        ):
            if isinstance(value, str):
                found.append(value)
            elif isinstance(value, (list, tuple)):
                found.extend(v for v in value if isinstance(v, str))
    return found


def _is_shell_keys(keys: Sequence[str]) -> bool:
    """判斷 keys 是否為 shell cmdline 組(避免 path 的 `*_path` fuzzy 命中誤套到 cmd)。"""
    return tuple(keys) == _CMDLINE_ARG_KEYS


# ---------------------------------------------------------------------------
# parser — Verb(arg_pattern) 字串
# ---------------------------------------------------------------------------


def parse_permission_rule(
    rule_str: str,
    effect: EffectStr = "allow",
    *,
    priority: int = 100,
    name: str | None = None,
    verb_alias_map: dict[str, str] | None = None,
    reason: str | None = None,
) -> PermissionRule:
    """parse `Verb(arg_pattern)` 字串成 `PermissionRule`。

    支援:
    * `"Verb"` — 無括號(整個 verb 通用)。
    * `"Verb()"` / `"Verb(*)"` — 視同無括號。
    * `"Verb(arg)"` — 一般 arg pattern。
    * 巢狀括號 `"Bash(echo (foo))"` — 用 depth counter 追,允許巢狀。
    * Escape `\\(` / `\\)` / `\\\\` — 在 arg pattern 內按字面處理。

    Args:
        rule_str: 原始 rule 字串。
        effect: ``"allow"`` / ``"deny"`` / ``"disable"``。預設 allow。
        priority: 對應 `PolicyRule.priority`。
        name: 自訂 rule 名稱。
        verb_alias_map: 覆寫 verb → tool regex 對映。
        reason: deny / disable 訊息。

    Returns:
        `PermissionRule`。

    Raises:
        PermissionRuleSyntaxError: rule 語法錯誤(空字串、verb 不合法、
            括號不平衡、verb 缺失等)。
    """
    if not isinstance(rule_str, str):
        raise PermissionRuleSyntaxError(
            repr(rule_str), "rule must be a string"
        )
    stripped = rule_str.strip()
    if not stripped:
        raise PermissionRuleSyntaxError(rule_str, "rule string is empty")

    verb, arg_pattern = _split_verb_and_arg(stripped)

    if not _VERB_PATTERN.match(verb):
        raise PermissionRuleSyntaxError(
            rule_str, f"invalid verb {verb!r}; must be identifier or '*'"
        )

    return PermissionRule(
        verb=verb,
        arg_pattern=arg_pattern,
        effect=effect,
        priority=priority,
        name=name,
        verb_alias_map=verb_alias_map,
        reason=reason,
    )


def _split_verb_and_arg(rule_str: str) -> tuple[str, str | None]:
    """切出 `verb` 與 `arg_pattern`,處理巢狀括號 + escape。

    Returns:
        (verb, arg_pattern) — 沒括號時 arg_pattern 為 None。

    Raises:
        PermissionRuleSyntaxError: 括號不平衡、verb 為空、結尾在閉括號後仍有字。
    """
    # 找第一個未 escape 的 "(" — 用 depth=0 起跳的 scan。
    open_idx = _find_unescaped(rule_str, "(", 0)
    if open_idx < 0:
        # 整串都是 verb,不允許混入 ")"。
        if _find_unescaped(rule_str, ")", 0) >= 0:
            raise PermissionRuleSyntaxError(
                rule_str, "unexpected ')' without matching '('"
            )
        return rule_str, None

    verb = rule_str[:open_idx]
    if not verb:
        raise PermissionRuleSyntaxError(
            rule_str, "missing verb before '('", position=open_idx
        )

    # 從 open_idx+1 開始用 depth counter 找對應 close。
    close_idx = _find_matching_close(rule_str, open_idx)
    if close_idx < 0:
        raise PermissionRuleSyntaxError(
            rule_str, "unbalanced parentheses", position=open_idx
        )

    # 閉括號之後不能再有字元(對齊上游)。
    tail = rule_str[close_idx + 1 :]
    if tail.strip():
        raise PermissionRuleSyntaxError(
            rule_str,
            f"unexpected trailing content {tail!r} after ')'",
            position=close_idx + 1,
        )

    raw_arg = rule_str[open_idx + 1 : close_idx]
    arg_pattern = _unescape_arg(raw_arg)
    return verb, arg_pattern


def _find_unescaped(s: str, ch: str, start: int) -> int:
    """找 s 中從 start 起、第一個未被 backslash escape 的 `ch`;找不到回 -1。

    backslash 奇數個視為 escape;偶數個視為「普通 backslash + 普通 ch」。
    """
    i = start
    while i < len(s):
        if s[i] == ch:
            # 數前面連續 backslash
            backslash = 0
            j = i - 1
            while j >= 0 and s[j] == "\\":
                backslash += 1
                j -= 1
            if backslash % 2 == 0:
                return i
        i += 1
    return -1


def _find_matching_close(s: str, open_idx: int) -> int:
    """從 open_idx(指向 "(")開始找對應的閉括號 index;支援巢狀。

    Returns:
        close_idx — 找不到回 -1。
    """
    depth = 1
    i = open_idx + 1
    while i < len(s):
        ch = s[i]
        if ch == "\\":
            # 跳過下一字元(escape)。
            i += 2
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _unescape_arg(raw: str) -> str:
    """把 escape sequence 還原到字面字元。

    處理順序(對齊上游 `unescapeRuleContent`):
    1. `\\(` → `(`
    2. `\\)` → `)`
    3. `\\\\` → `\\`
    """
    return raw.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\")


def parse_permission_rules(
    rules: Sequence[str],
    effect: EffectStr = "allow",
    *,
    priority: int = 100,
    verb_alias_map: dict[str, str] | None = None,
) -> list[PermissionRule]:
    """批次 parse;同一批 rule 全部套同一個 effect / priority。

    Args:
        rules: rule 字串清單。
        effect: 套用到全部 rule 的 effect。
        priority: 套用到全部 rule 的 priority。
        verb_alias_map: 覆寫 verb→regex 對映。

    Returns:
        `list[PermissionRule]`,長度等於輸入。

    Raises:
        PermissionRuleSyntaxError: 任一 rule 解析失敗。
    """
    return [
        parse_permission_rule(
            r,
            effect=effect,
            priority=priority,
            verb_alias_map=verb_alias_map,
        )
        for r in rules
    ]


# ---------------------------------------------------------------------------
# YAML sugar — 與 P0-7 `PolicyEngine.from_yaml` 整合
# ---------------------------------------------------------------------------


def policy_rule_from_yaml_item(item: dict[str, Any]) -> PolicyRule:
    """把 yaml dict 轉成 `PolicyRule`,支援 P1-16 sugar 與 P0-7 舊格式並存。

    支援欄位:
    * **新 sugar(P1-16)**:
        - ``permission`` (str): `"Verb(arg)"` 字串。
        - ``effect`` (optional, default "allow"): allow / deny / disable。
        - ``priority`` (optional, default 100): rule 優先級。
        - ``name`` (optional): 自訂名稱。
        - ``reason`` (optional): deny / disable 訊息。
    * **舊 P0-7 格式**(`permission` 缺席時走這條):
        - ``name`` (required)
        - ``tool_pattern`` / ``effect`` / ``priority`` / ``reason``

    Args:
        item: 來自 yaml `rules:` list 的單一項目。

    Returns:
        `PolicyRule` 實例。

    Raises:
        PermissionRuleSyntaxError: `permission` sugar 內字串語法錯誤。
        ValueError: 舊格式缺欄位 / effect 非法。
    """
    perm = item.get("permission")
    if perm is not None:
        if not isinstance(perm, str):
            raise PermissionRuleSyntaxError(
                str(perm),
                "'permission' must be a string like 'Verb(arg)'",
            )
        raw_effect = item.get("effect", "allow")
        if raw_effect not in ("allow", "deny", "disable"):
            raise ValueError(
                f"invalid effect {raw_effect!r}; "
                f"must be one of allow / deny / disable"
            )
        perm_rule = parse_permission_rule(
            perm,
            effect=raw_effect,
            priority=int(item.get("priority", 100)),
            name=item.get("name"),
            reason=item.get("reason"),
        )
        return perm_rule.to_policy_rule()

    # fallback:走 P0-7 舊格式(本檔不 import 內部 helper,直接重建)。
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(
            "rule must have 'name' (when not using 'permission' sugar)"
        )
    raw_effect = item.get("effect", "allow")
    try:
        effect_enum = PolicyEffect(raw_effect)
    except ValueError as e:
        raise ValueError(
            f"rule {name!r}: invalid effect {raw_effect!r}; "
            f"must be one of {[e.value for e in PolicyEffect]}"
        ) from e
    return PolicyRule(
        name=name,
        tool_pattern=item.get("tool_pattern"),
        effect=effect_enum,
        priority=int(item.get("priority", 100)),
        reason=item.get("reason"),
    )


def load_policy_engine_from_yaml(path: Any) -> PolicyEngine:
    """從 YAML 載入 PolicyEngine,支援 P1-16 sugar 與 P0-7 舊格式並存。

    這是「擴充版」`PolicyEngine.from_yaml`;不修改 P0-7 原檔,而是另闢一條
    loader,內部走 `policy_rule_from_yaml_item()`。可在需要 sugar 的場合
    用本函式替代 `PolicyEngine.from_yaml`。

    Args:
        path: YAML 檔路徑(str / Path)。

    Returns:
        `PolicyEngine` 實例。

    Raises:
        FileNotFoundError: 檔案不存在。
        ValueError: rules 結構非 list / 單一 rule 欄位錯誤。
        PermissionRuleSyntaxError: `permission` sugar 字串語法錯誤。
        ImportError: 系統未安裝 pyyaml。
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "pyyaml is required for load_policy_engine_from_yaml(); "
            "install via `pip install pyyaml`"
        ) from e

    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}

    rules_data = data.get("rules", [])
    if not isinstance(rules_data, list):
        raise ValueError(
            f"'rules' must be a list in {path!s}; got {type(rules_data).__name__}"
        )

    engine = PolicyEngine()
    for idx, item in enumerate(rules_data):
        if not isinstance(item, dict):
            raise ValueError(
                f"rule #{idx} in {path!s} must be a mapping; "
                f"got {type(item).__name__}"
            )
        engine.add_rule(policy_rule_from_yaml_item(item))
    return engine


__all__ = [
    "EffectStr",
    "PermissionRule",
    "PermissionRuleSyntaxError",
    "load_policy_engine_from_yaml",
    "parse_permission_rule",
    "parse_permission_rules",
    "policy_rule_from_yaml_item",
]
