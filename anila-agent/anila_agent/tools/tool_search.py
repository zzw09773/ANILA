"""P1-18 ToolSearch + activate_tool meta-tools。

對應 claude-code-src `src/tools/ToolSearchTool/ToolSearchTool.ts`(471 LOC)。

**問題**:agent expose 100+ tool 時,把所有 tool schema 一次塞進 system prompt
會吃光 input token,且每輪都要重塞(prompt cache 跨對話命中率不高)。

**解法**:把 tool 分成兩半:

* **active** —— 啟動時就放進 prompt 的 tool schema(visible to LLM)。
* **deferred** —— 只在 prompt 內透露 tool **name**(不放 schema);LLM 想用時要先
  呼叫 ``tool_search`` 找,再呼叫 ``activate_tool`` 啟用 schema。

整體流程:

1. ``ToolRegistry`` 啟動時把 tool list 分成 active + deferred(:meth:`add` 或
   :meth:`register(deferred=True)`)。
2. agent prompt builder 只把 ``registry.find_active(session=...)`` 拿來給 LLM。
3. LLM 在 prompt 內看到 deferred tool name list(沒有 schema)。
4. LLM 想用某 deferred tool → call ``tool_search(query="select:Foo,Bar")``。
5. ``tool_search`` 回傳該 tool 的 description + 引導 LLM 呼 ``activate_tool``。
6. LLM call ``activate_tool(name="Foo")`` → registry 把 Foo 移到 active 集合。
7. 之後重組 prompt 時,Foo 的 schema 就會被帶上。

Query 語法(模仿 claude-code-src):

* ``select:Name1,Name2`` —— 精準點名(case-sensitive 比對,後 fallback 不分大小寫)。
* ``+keyword rest`` —— ``+keyword`` 強制要求 substring 命中,``rest`` 為加權字串。
* 一般 query —— 用 :class:`difflib.SequenceMatcher` 對 tool name + description 做
  fuzzy ranking,取 top_k。
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agents import FunctionTool

from anila_agent.core.hook_context import SessionContext
from anila_agent.tools.base import ToolMetadata, _attach_metadata
from anila_agent.tools.registry import ToolRegistry

# meta-tool 用的固定 name(對應 claude-code-src TOOL_SEARCH_TOOL_NAME)。
TOOL_SEARCH_TOOL_NAME = "tool_search"
ACTIVATE_TOOL_NAME = "activate_tool"

# meta-tool 預設不 defer 自己 — 不然 LLM 永遠看不到 ToolSearch 怎麼啟。
_META_TOOL_METADATA = ToolMetadata(
    is_read_only=True,
    category="meta",
    cost_estimate="free",
    is_deferred=False,
)


# Session 取得器 — runner 注入 ToolSearchContext 後,tool callback 內透過
# 這個 callable 取出當下 session。預設 raise 提示使用者必須 build_meta_tools。
SessionResolver = Callable[[], SessionContext]


@dataclass
class ToolSearchResult:
    """單筆 ``tool_search`` 命中結果。"""

    name: str
    description: str
    score: float
    category: str


# ---------------------------------------------------------------------------
# Search 邏輯
# ---------------------------------------------------------------------------


def _parse_select_clause(query: str) -> list[str] | None:
    """解析 ``select:Name1,Name2`` 語法。

    回傳 list[name](保留原大小寫);若 query 不是 select 語法則 None。
    """
    q = query.strip()
    if not q.lower().startswith("select:"):
        return None
    body = q.split(":", 1)[1]
    names = [n.strip() for n in body.split(",") if n.strip()]
    return names or None


def _parse_required_keywords(query: str) -> tuple[list[str], str]:
    """解析 ``+keyword`` 必須詞 + 剩下的 query body。

    Returns:
        (required_keywords, remaining_query) — required 是必須出現 substring,
        remaining 是用於 fuzzy 排序的字串。
    """
    required: list[str] = []
    remaining: list[str] = []
    for token in query.split():
        if token.startswith("+") and len(token) > 1:
            required.append(token[1:].lower())
        else:
            remaining.append(token)
    return required, " ".join(remaining)


def _score(query: str, name: str, description: str) -> float:
    """以 SequenceMatcher 對 name + description 做 fuzzy 比對,取較大值。"""
    if not query:
        return 0.0
    q = query.lower()
    n = name.lower()
    d = description.lower()
    name_score = difflib.SequenceMatcher(None, q, n).ratio()
    # description 通常較長,用 partial match — 若 q 直接 substring 命中,給滿分。
    desc_score = 1.0 if q in d else difflib.SequenceMatcher(None, q, d).ratio()
    # tool name 比 description 更重要,給 1.5 加權後再 normalize。
    return max(name_score * 1.5, desc_score) / 1.5


def search_deferred(
    registry: ToolRegistry,
    query: str,
    *,
    top_k: int = 5,
    session: SessionContext | None = None,
) -> list[ToolSearchResult]:
    """從 registry 的 deferred pool 找出 top_k 命中的 tool。

    支援三種 query 形式:

    1. ``select:Name1,Name2`` —— 精準點名(case-insensitive),取交集。
    2. ``+keyword rest`` —— 必須含 keyword 才入選,rest 用於排序。
    3. 一般 query —— 純 fuzzy ranking。

    Args:
        registry: tool registry。
        query: 搜尋字串。
        top_k: 最多回傳幾筆;<=0 視為「全部」。
        session: 可選 session — 用來排除「該 session 已 activate」的 tool。

    Returns:
        ToolSearchResult list,依 score 由高到低排序。
    """
    deferred_pool = registry.find_deferred(session=session)
    if not deferred_pool:
        return []

    # 1. select: 直接點名
    select_names = _parse_select_clause(query)
    if select_names is not None:
        wanted_lower = {n.lower() for n in select_names}
        hits: list[ToolSearchResult] = []
        for tool in deferred_pool:
            if tool.name.lower() in wanted_lower:
                meta = registry.get_metadata(tool.name)
                hits.append(
                    ToolSearchResult(
                        name=tool.name,
                        description=(tool.description or "").strip(),
                        score=1.0,
                        category=meta.category,
                    )
                )
        return hits if top_k <= 0 else hits[:top_k]

    # 2. +keyword 必須詞 + fuzzy
    required, remaining = _parse_required_keywords(query)
    scored: list[ToolSearchResult] = []
    for tool in deferred_pool:
        desc = (tool.description or "").strip()
        name_lower = tool.name.lower()
        desc_lower = desc.lower()
        if any(req not in name_lower and req not in desc_lower for req in required):
            continue  # 必須詞至少一個沒命中 → 淘汰
        score = _score(remaining or query, tool.name, desc)
        meta = registry.get_metadata(tool.name)
        scored.append(
            ToolSearchResult(
                name=tool.name,
                description=desc,
                score=score,
                category=meta.category,
            )
        )

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored if top_k <= 0 else scored[:top_k]


def format_search_results(results: list[ToolSearchResult]) -> str:
    """把 search 結果格式化成 LLM 可讀字串。

    回傳 JSON-like 結構含使用提示,引導 LLM 接著呼叫 ``activate_tool``。
    """
    if not results:
        return json.dumps(
            {
                "matches": [],
                "hint": (
                    "No deferred tools matched. Either the tool doesn't exist, "
                    "is already active, or your query is too narrow."
                ),
            },
            ensure_ascii=False,
        )
    payload = {
        "matches": [
            {
                "name": r.name,
                "description": r.description,
                "category": r.category,
                "score": round(r.score, 4),
            }
            for r in results
        ],
        "hint": (
            "These tools are deferred (schema not loaded). "
            f"Call {ACTIVATE_TOOL_NAME}(name=<tool_name>) to load schema "
            "into the next turn before invoking."
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Meta-tool 工廠
# ---------------------------------------------------------------------------


def _make_tool_search_tool(
    registry: ToolRegistry,
    session_resolver: SessionResolver,
) -> FunctionTool:
    """建立 ``tool_search`` FunctionTool。

    使用直接構造 :class:`FunctionTool`(而非 ``@function_tool`` 裝飾器)是為了讓
    callback 能 closure 捕捉 ``registry`` 與 ``session_resolver``。
    """

    async def _invoke(_ctx: Any, args_str: str) -> str:
        try:
            args: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            return format_search_results([])
        query = str(args.get("query") or "").strip()
        raw_top_k = args.get("top_k", 5)
        try:
            top_k = int(raw_top_k)
        except (TypeError, ValueError):
            top_k = 5
        session = session_resolver()
        results = search_deferred(
            registry, query, top_k=top_k, session=session
        )
        return format_search_results(results)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query. Supports: 'select:Name1,Name2' for exact lookup, "
                    "'+keyword rest' to require substring, or plain text for fuzzy match."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max number of matches to return (default 5).",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    tool = FunctionTool(
        name=TOOL_SEARCH_TOOL_NAME,
        description=(
            "Search the deferred tool pool for tools that are registered but whose "
            "schemas are not yet loaded into the prompt. Returns ranked candidates "
            f"and a hint to call {ACTIVATE_TOOL_NAME} to load a tool's schema."
        ),
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(tool, _META_TOOL_METADATA)


def _make_activate_tool(
    registry: ToolRegistry,
    session_resolver: SessionResolver,
) -> FunctionTool:
    """建立 ``activate_tool`` FunctionTool — 把某 deferred tool 移到 active 集合。"""

    async def _invoke(_ctx: Any, args_str: str) -> str:
        try:
            args: dict[str, Any] = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "error": "invalid_json"}, ensure_ascii=False
            )
        name = str(args.get("name") or "").strip()
        if not name:
            return json.dumps(
                {"ok": False, "error": "missing_name"}, ensure_ascii=False
            )
        session = session_resolver()
        try:
            tool = registry.activate(name, session=session)
        except KeyError:
            return json.dumps(
                {"ok": False, "error": "not_registered", "name": name},
                ensure_ascii=False,
            )
        except ValueError:
            return json.dumps(
                {"ok": False, "error": "not_deferred", "name": name},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "hint": (
                    "Tool schema will be available on the next turn. "
                    "You may now call it."
                ),
            },
            ensure_ascii=False,
        )

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Exact name of the deferred tool to activate.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    tool = FunctionTool(
        name=ACTIVATE_TOOL_NAME,
        description=(
            "Activate a deferred tool by name, loading its schema into the active "
            "tool set for subsequent turns. Use after tool_search() to enable a tool."
        ),
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
    return _attach_metadata(tool, _META_TOOL_METADATA)


def build_meta_tools(
    registry: ToolRegistry,
    session_resolver: SessionResolver,
) -> list[FunctionTool]:
    """建立 ``tool_search`` + ``activate_tool`` 兩個 meta-tool。

    Args:
        registry: 要被搜尋與啟用 deferred tool 的 :class:`ToolRegistry`。
        session_resolver: callable,呼叫時回傳當下 SessionContext。runner 在切換
            session 時更新 closure 即可,meta-tool 本身不需要重建。

    Returns:
        [tool_search, activate_tool]
    """
    return [
        _make_tool_search_tool(registry, session_resolver),
        _make_activate_tool(registry, session_resolver),
    ]


def register_meta_tools(
    registry: ToolRegistry,
    session_resolver: SessionResolver,
) -> list[FunctionTool]:
    """建立 meta-tool 並直接註冊進 registry(非 deferred,LLM 一啟動就看得到)。

    若 registry 已存在同名 tool 會 raise :class:`ValueError`(對應
    :meth:`ToolRegistry.register` 行為);呼叫端必須保證乾淨 registry。
    """
    meta_tools = build_meta_tools(registry, session_resolver)
    for tool in meta_tools:
        registry.register(tool, deferred=False)
    return meta_tools


# ---------------------------------------------------------------------------
# 便利 helper —— session_resolver factory
# ---------------------------------------------------------------------------


def make_static_session_resolver(session: SessionContext) -> SessionResolver:
    """產生「永遠回傳同一 SessionContext」的 resolver,用於單 session / 測試。

    生產環境通常會用 closure 動態切換 session(例如:每次 turn 從 runner 拿)。
    """

    def _resolve() -> SessionContext:
        return session

    return _resolve


__all__ = [
    "ACTIVATE_TOOL_NAME",
    "TOOL_SEARCH_TOOL_NAME",
    "SessionResolver",
    "ToolSearchResult",
    "build_meta_tools",
    "format_search_results",
    "make_static_session_resolver",
    "register_meta_tools",
    "search_deferred",
]
