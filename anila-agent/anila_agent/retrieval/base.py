"""Retriever 介面 —— 平台穩定接縫。

任何後端（dummy / ANILA-native pgvector / 通用 pgvector / CSP HTTP）只要符合
這個 Protocol，就能被 RAG 工具與 agent factory 接受。`@runtime_checkable`
讓 ``isinstance`` 在 ``set_retriever`` 時做形狀檢查。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from anila_agent.retrieval.schemas import Document


@runtime_checkable
class Retriever(Protocol):
    """檢索後端契約。實作須為 async，且行為符合下列保證。"""

    @property
    def name(self) -> str:
        """後端識別名（給 log / 稽核用）。"""
        ...

    @property
    def metadata(self) -> dict:
        """後端設定摘要（不得含密鑰）。"""
        ...

    async def search(self, query: str, k: int = 5) -> list[Document]:
        """回傳依相關度遞減排序、長度 <= k 的命中。永不回 None；無結果回 []。"""
        ...

    async def fetch(self, doc_id: str) -> Document | None:
        """以 id 取單筆；找不到回 None。

        注意：對真實 retriever 多半回 None（search 命中已帶全文）——這是契約，非 bug。
        """
        ...
