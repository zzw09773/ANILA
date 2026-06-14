"""零基建的記憶體內 retriever —— clone-and-run 預設。

以 token 重疊度排序，無外部相依。換成真實後端前，讓樣板「下載即可跑」。
與真實 retriever 不同，``fetch`` 確實能依 id 取回（因為語料在記憶體內）。
"""

from __future__ import annotations

import re

from anila_agent.retrieval.schemas import Document

_TOKEN = re.compile(r"\w+", re.UNICODE)

# 預設樣本語料（zh-TW）。換上自己的 retriever 後即可移除。
_DEFAULT_CORPUS: list[tuple[str, str]] = [
    ("doc-anila-overview", "ANILA 是中科院內網的 air-gapped AI 平台，提供 RAG 問答、文件管理與影像生成。"),
    ("doc-rag", "Agentic RAG 讓 agent 自行決定何時檢索、檢索什麼，並以引用接地回答。"),
    ("doc-airgap", "air-gapped 部署不連外網：模型走本地 vLLM OpenAI-compatible 端點，無外部 CDN。"),
    ("doc-template", "anila-agent 是可下載的官方樣板，填上 retriever 與 prompt 即可成為你的 agent。"),
]


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text)}


class DummyRetriever:
    """token 重疊度檢索；僅供開發與示範。"""

    name = "dummy"

    def __init__(self, corpus: list[tuple[str, str]] | None = None) -> None:
        self._docs: dict[str, str] = dict(corpus if corpus is not None else _DEFAULT_CORPUS)

    @property
    def metadata(self) -> dict:
        return {"backend": "dummy", "size": len(self._docs)}

    async def search(self, query: str, k: int = 5) -> list[Document]:
        q = _tokens(query)
        if not q:
            return []
        scored: list[tuple[float, str, str]] = []
        for doc_id, text in self._docs.items():
            overlap = len(q & _tokens(text))
            if overlap:
                scored.append((overlap / len(q), doc_id, text))
        scored.sort(key=lambda r: r[0], reverse=True)
        return [
            Document(id=doc_id, text=text, score=score, metadata={"backend": "dummy"})
            for score, doc_id, text in scored[: max(k, 0)]
        ]

    async def fetch(self, doc_id: str) -> Document | None:
        text = self._docs.get(doc_id)
        if text is None:
            return None
        return Document(id=doc_id, text=text, metadata={"backend": "dummy"})
