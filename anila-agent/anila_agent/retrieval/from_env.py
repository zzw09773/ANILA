"""依環境變數自動挑選 retriever（平台契約：優先序固定）。

優先序：explicit arg（在 build_agent 處理）> csp_http > anila_pgvector >
通用 pgvector > dummy。各後端的 from_env() 在「部分設定」時會 raise（而非靜默
回退），以凸顯部署錯誤。

raise 歸因（保留舊行為）：設了 ANILA_COLLECTION_ID 但忘了 ANILA_CSP_BASE_URL
（本意想用 CSP），csp_http 會回 None，接著 anila_pgvector 因缺 PGVECTOR_URL 而
RAISE pgvector 錯誤——錯誤訊息來源是 pgvector，這是刻意保留的既有行為。

csp_http 勝過 anila_pgvector：兩者都看 ANILA_COLLECTION_ID，但 csp_http 另需
ANILA_CSP_BASE_URL（base-url gate）；同時設齊時 csp_http 先命中。
"""

from __future__ import annotations

from anila_agent.config import AppConfig
from anila_agent.retrieval import anila_pgvector, csp_http, pgvector
from anila_agent.retrieval.base import Retriever
from anila_agent.retrieval.dummy import DummyRetriever


def select_retriever(cfg: AppConfig) -> Retriever:
    """回傳依環境選定的 retriever。皆未設時回 DummyRetriever。"""
    for factory in (csp_http.from_env, anila_pgvector.from_env, pgvector.from_env):
        retriever = factory()
        if retriever is not None:
            return retriever
    return DummyRetriever()
