"""檢索結果的資料形狀。

`Document` 是平台契約的一部分：欄位與型別**不可變動**，否則會破壞既有
retriever / RAG 工具 / prompt 對輸出形狀的假設。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Document(BaseModel):
    """一筆檢索命中。

    - ``id``：來源 chunk 的字串識別碼。
    - ``text``：chunk 全文（命中已帶全文，故 ``fetch`` 對真實 retriever 多回 None）。
    - ``score``：相關度，越大越相關；可為 None（部分後端不回分數）。
    - ``metadata``：來源檔名、document_id、chunk_key 等；恆為 dict。
    """

    id: str
    text: str
    score: float | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
