"""Tool registry — 將 tool name 對應到 FunctionTool 並去重。

P0-2 後支援 metadata 查詢:
    registry.find_by_metadata(is_read_only=True)
    registry.find_by_metadata(category="filesystem", is_destructive=True)

供 hook / guardrail / concurrency partitioner 過濾 tool 用。

P1-18 後支援 deferred tools / ToolSearch:
    registry.add(tool, deferred=True)            # 註冊但預設不放進 prompt
    registry.find_active(session=session_ctx)    # 給 LLM 看的 visible 集合
    registry.find_deferred()                     # 還沒被啟用的 deferred 集合
    registry.activate(name, session=session_ctx) # 把某 deferred tool 移到 active

per-session active 透過 SessionContext.metadata['_anila_active_deferred'] 存放,
session 結束後狀態自然消失(SessionContext 本身有生命週期管理)。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agents import FunctionTool

from anila_agent.tools.base import CostEstimate, ToolMetadata, get_metadata, import_tool

if TYPE_CHECKING:
    from anila_agent.core.hook_context import SessionContext


# SessionContext.metadata 內存放「已 activate 的 deferred tool name set」的 key。
# 用 `_` 前綴避免跟 user metadata 撞 key。
_ACTIVE_DEFERRED_KEY = "_anila_active_deferred"


def _session_active_set(session: SessionContext) -> set[str]:
    """回傳 session 上「已啟用 deferred tool name」的可變 set。

    若 metadata 尚未初始化,自動建立一個空 set 並寫回。這樣 caller 可以直接
    ``s = _session_active_set(sess); s.add(name)``,不必擔心 KeyError。
    """
    bucket = session.metadata.get(_ACTIVE_DEFERRED_KEY)
    if not isinstance(bucket, set):
        bucket = set()
        session.metadata[_ACTIVE_DEFERRED_KEY] = bucket
    return bucket


def reset_active_deferred(session: SessionContext) -> None:
    """清空 session 上的「已啟用 deferred tool」狀態。

    session 結束或顯式重置時呼叫。SessionContext 本身被丟掉時這個狀態就消失了,
    但若呼叫端想在同一 session 內重啟 tool 探索流程,可顯式呼叫此 helper。
    """
    session.metadata.pop(_ACTIVE_DEFERRED_KEY, None)


@dataclass
class ToolRegistry:
    """tool registry。

    Attributes:
        tools: tool name → FunctionTool 映射。
        deferred_names: 標為 deferred 的 tool name set。**不**依賴 tool metadata 本身
            的 ``is_deferred`` 旗標 — registry 層額外保留一份 set,讓呼叫端可以對
            「同一個 tool 在不同 registry 內 defer 與否」做覆寫(例如測試環境想關閉
            deferred 機制)。為了一致性,``register`` 同時會檢查 metadata,若有 conflict
            以 ``deferred`` 參數為準(metadata 的 ``is_deferred`` 視為 hint)。
    """

    tools: dict[str, FunctionTool] = field(default_factory=dict)
    deferred_names: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # 註冊
    # ------------------------------------------------------------------

    def add(self, tool: FunctionTool) -> None:
        """加入 tool。若 tool metadata 標 ``is_deferred=True``,自動列入 deferred。"""
        self.register(tool, deferred=get_metadata(tool).is_deferred)

    def register(self, tool: FunctionTool, *, deferred: bool = False) -> None:
        """顯式註冊 tool,可指定是否 deferred。

        Args:
            tool: 要註冊的 FunctionTool。
            deferred: True 時加入 deferred 集合,預設不放進 prompt。
        """
        if tool.name in self.tools:
            raise ValueError(f"Duplicate tool name: {tool.name!r}")
        self.tools[tool.name] = tool
        if deferred:
            self.deferred_names.add(tool.name)

    def add_many(self, tools: Iterable[FunctionTool]) -> None:
        for t in tools:
            self.add(t)

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def as_list(self) -> list[FunctionTool]:
        """回傳所有 tool(含 deferred)。

        注意:這個方法**不**做 deferred 過濾,給 hook / guardrail / 內省工具用。
        要拿給 LLM 的請用 :meth:`find_active`。
        """
        return list(self.tools.values())

    def get_metadata(self, name: str) -> ToolMetadata:
        """回傳指定 tool 的 metadata;未註冊則 raise KeyError。"""
        if name not in self.tools:
            raise KeyError(f"Tool not in registry: {name!r}")
        return get_metadata(self.tools[name])

    def is_deferred(self, name: str) -> bool:
        """tool 是否仍處於 deferred(尚未被任何 session 啟用)。"""
        return name in self.deferred_names

    def find_active(
        self, *, session: SessionContext | None = None
    ) -> list[FunctionTool]:
        """回傳 LLM 應該看到的 active tool 集合。

        Active 集合 = (非 deferred tool) + (該 session 已 activate 的 deferred tool)。

        Args:
            session: 可選 session context。若提供,該 session 上已 ``activate`` 的
                deferred tool 也會被加入結果;若 None,只回非 deferred tool。
        """
        active_names = (
            _session_active_set(session) if session is not None else set()
        )
        out: list[FunctionTool] = []
        for name, tool in self.tools.items():
            if name not in self.deferred_names or name in active_names:
                out.append(tool)
        return out

    def find_deferred(
        self, *, session: SessionContext | None = None
    ) -> list[FunctionTool]:
        """回傳「仍 deferred、尚未啟用」的 tool 集合。

        Args:
            session: 可選 session context。若提供,「該 session 已 activate」的 tool
                會從結果中排除;若 None,回傳所有 deferred tool。
        """
        active_names = (
            _session_active_set(session) if session is not None else set()
        )
        out: list[FunctionTool] = []
        for name in self.deferred_names:
            if name in active_names:
                continue
            if name in self.tools:  # 防呆:tool 被外部 pop 掉時跳過
                out.append(self.tools[name])
        return out

    def activate(self, name: str, *, session: SessionContext) -> FunctionTool:
        """把 deferred tool 移入該 session 的 active 集合,回傳該 tool。

        Raises:
            KeyError: tool name 不在 registry。
            ValueError: tool 不是 deferred(沒必要 activate)。
        """
        if name not in self.tools:
            raise KeyError(f"Tool not in registry: {name!r}")
        if name not in self.deferred_names:
            raise ValueError(
                f"Tool {name!r} is not deferred — already active by default"
            )
        _session_active_set(session).add(name)
        return self.tools[name]

    def deactivate(self, name: str, *, session: SessionContext) -> None:
        """從該 session 移除已啟用的 deferred tool;若不存在則 no-op。"""
        active = _session_active_set(session)
        active.discard(name)

    # ------------------------------------------------------------------
    # metadata filter
    # ------------------------------------------------------------------

    def find_by_metadata(
        self,
        *,
        is_read_only: bool | None = None,
        is_destructive: bool | None = None,
        concurrency_safe: bool | None = None,
        cost_estimate: CostEstimate | None = None,
        requires_approval: bool | None = None,
        is_open_world: bool | None = None,
        category: str | None = None,
        is_deferred: bool | None = None,
    ) -> list[FunctionTool]:
        """依 metadata 條件過濾 tool。

        `None` 表示不過濾該欄位;非 `None` 則需精準匹配。多個條件以 AND 串接。

        範例:
            # 所有唯讀且屬於 retrieval 類別的 tool
            registry.find_by_metadata(is_read_only=True, category="retrieval")

            # 所有破壞性 tool(供 guardrail 列管)
            registry.find_by_metadata(is_destructive=True)

            # 所有 deferred tool(對應 registry.deferred_names 但走 metadata 路徑)
            registry.find_by_metadata(is_deferred=True)
        """
        filters: dict[str, Any] = {
            "is_read_only": is_read_only,
            "is_destructive": is_destructive,
            "concurrency_safe": concurrency_safe,
            "cost_estimate": cost_estimate,
            "requires_approval": requires_approval,
            "is_open_world": is_open_world,
            "category": category,
            "is_deferred": is_deferred,
        }
        active = {k: v for k, v in filters.items() if v is not None}

        result: list[FunctionTool] = []
        for tool in self.tools.values():
            meta = get_metadata(tool)
            if all(getattr(meta, k) == v for k, v in active.items()):
                result.append(tool)
        return result


def load_tools(qualified_paths: Iterable[str]) -> list[FunctionTool]:
    """以 fully-qualified attribute path 解析 FunctionTool。

    透過 registry 去重,重複 tool name(例如 config + code 雙重註冊)會直接失敗
    而非默默覆蓋。
    """
    registry = ToolRegistry()
    for path in qualified_paths:
        registry.add(import_tool(path))
    return registry.as_list()
