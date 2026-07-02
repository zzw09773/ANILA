"""結構化輸出 schema：接地引用回答。

opt-in（ANILA_CITED=1）。reasoning 模型的結構化輸出可靠性見 util.structured 與
runtime.model 的防護（max_tokens 下限）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Citation(BaseModel):
    source: str  # chunk id 或檔名
    quote: str | None = None  # 支撐該主張的原文片段（可選）


class CitedAnswer(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
