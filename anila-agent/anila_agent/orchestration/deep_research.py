"""多代理深度檢索：planner → 平行檢索 → writer（→ verifier）。

對應 research_bot / financial_research 的形狀。預設不啟用——以 ``/deep-research``
slash 指令觸發，示範多代理模式而不對每次查詢加成本。

planner 與 writer 走本地模型（reasoning 模型：結構化輸出給足 max_tokens）。檢索平行化
且容忍單一子查詢失敗（as_completed/gather + return_exceptions）。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agents import Agent, Runner

from anila_agent.runtime.model import build_model, build_model_settings, json_object_settings
from anila_agent.util.structured import parse_json_object

if TYPE_CHECKING:
    from anila_agent.config import AppConfig
    from anila_agent.retrieval.base import Retriever
    from anila_agent.retrieval.schemas import Document


def _format_context(query: str, blocks: list[tuple[str, list[Document]]]) -> str:
    """把各子查詢的檢索結果組成 writer 的輸入（純函式，可測）。"""
    lines = [f"原始問題：{query}", "", "檢索資料："]
    for subq, docs in blocks:
        lines.append(f"\n## 子問題：{subq}")
        if not docs:
            lines.append("（無檢索結果）")
        for d in docs:
            lines.append(f"- [{d.id}] {d.text}")
    return "\n".join(lines)


async def run_deep_research(
    cfg: AppConfig,
    query: str,
    retriever: Retriever,
    *,
    max_subqueries: int = 4,
    k: int = 5,
) -> str:
    """跑深度檢索管線，回傳綜合後的答案。"""
    model = build_model(cfg.model)
    settings = build_model_settings(cfg.model)

    # planner 走 response_format=json_object + 防禦性解析（自架模型 json_schema 不可靠）。
    planner = Agent(
        name="research-planner",
        instructions=(
            "你是檢索規劃師。把使用者問題拆成 2 到 4 個彼此互補的檢索子問題，涵蓋不同面向。"
            '只輸出 JSON：{"subqueries": ["子問題1", "子問題2"]}。'
        ),
        model=model,
        model_settings=json_object_settings(cfg.model),
    )
    plan_result = await Runner.run(planner, query, max_turns=2)
    raw = plan_result.final_output if isinstance(plan_result.final_output, str) else ""
    parsed = parse_json_object(raw).get("subqueries", [])
    subqueries = [s for s in parsed if isinstance(s, str) and s.strip()][:max_subqueries] or [query]

    async def _retrieve(subq: str) -> tuple[str, list[Document]]:
        try:
            return subq, await retriever.search(subq, k=k)
        except Exception:
            return subq, []

    blocks = await asyncio.gather(*[_retrieve(s) for s in subqueries])

    writer = Agent(
        name="research-writer",
        instructions=(
            "你是綜合撰寫者。只根據提供的檢索資料回答原始問題，"
            "每個主張附上來源 id（如【來源：<id>】）。檢索不到的就明說查無，不臆造。"
            "以繁體中文、台灣用語作答。"
        ),
        model=model,
        model_settings=settings,
    )
    writer_result = await Runner.run(writer, _format_context(query, blocks), max_turns=2)
    return writer_result.final_output or ""
