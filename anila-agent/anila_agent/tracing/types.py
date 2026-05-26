"""Tracing 核心資料型別:`Trace` 與 `Span`。

設計參考 OpenTelemetry semantic conventions(但不引入 OTel 依賴),屬性鍵
(attribute key)建議採 OTel 慣用點分隔命名,例如:

- ``"http.method"`` / ``"http.status_code"``
- ``"llm.model"`` / ``"llm.tokens.prompt"``
- ``"tool.name"`` / ``"tool.category"``
- ``"db.system"`` / ``"db.statement"``

`Trace` 代表一次完整的 agent run 根區塊;`Span` 為其中的某個 operation
(可巢狀)。兩者皆為純資料容器,生命週期由 :class:`anila_agent.tracing.Tracer`
管理。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# `Span.status` 的可能值。`"in_progress"` 為尚未結束;`"ok"` 為正常結束;
# `"error"` 為異常結束(此時 ``error`` 欄位應有錯誤描述)。
SpanStatus = Literal["ok", "error", "in_progress"]


def _new_uuid() -> str:
    """產生隨機 UUID(hex 字串)作為 trace_id / span_id。"""
    return uuid.uuid4().hex


@dataclass
class Trace:
    """一次完整 agent run 的根區塊(root)。

    屬於純資料容器:start / end 時間與屬性皆由外部 :class:`Tracer` 在進出
    context manager 時填入。對齊 OTel 觀念,trace 為一條 workflow 的根節點,
    底下可掛多個 :class:`Span`(由 ``parent_span_id`` 自然連成 DAG)。

    Attributes:
        trace_id: 此 trace 的全域唯一識別碼(UUID hex)。
        name: 人類可讀的 workflow 名稱,例如 ``"agent.run"``。
        start_time: trace 起始時間(進入 context manager 時填入)。
        end_time: trace 結束時間;進行中時為 ``None``。
        metadata: 自訂中繼資料(如 group_id、user_id 等)。
    """

    trace_id: str = field(default_factory=_new_uuid)
    name: str = ""
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化為 dict(供 processor 寫 JSONL 或上傳 backend)。

        為避免下游程式碼解析 ``datetime`` 物件出錯,時間欄位以 ISO 8601 字串
        呈現(``end_time`` 若為 None 則保留 None)。
        """
        return {
            "object": "trace",
            "trace_id": self.trace_id,
            "name": self.name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "metadata": dict(self.metadata),
        }


@dataclass
class Span:
    """trace 內的單一 operation 區段(可巢狀)。

    每個 span 對應一段可被追蹤的 operation,例如 ``"agent.run"``、
    ``"tool.call.search"``、``"llm.completion"``。`attributes` 採 OTel
    semantic conventions 命名慣例(``"llm.model"`` / ``"tool.name"`` 等),
    讓未來若要轉成 OTel exporter 也能直接對應。

    Attributes:
        span_id: 此 span 的全域唯一識別碼(UUID hex)。
        trace_id: 此 span 所屬 trace 的識別碼。
        parent_span_id: 父 span 識別碼;若為頂層(直接掛在 trace 下)則為 None。
        name: span 名稱,建議用點分隔的命名空間(例如 ``"tool.call.read_file"``)。
        start_time: span 起始時間。
        end_time: span 結束時間;進行中時為 ``None``。
        attributes: 自訂屬性(對齊 OTel semantic conventions 命名)。
        status: span 狀態 — ``"in_progress"`` / ``"ok"`` / ``"error"``。
        error: 當 ``status == "error"`` 時的錯誤描述;否則為 None。
    """

    span_id: str = field(default_factory=_new_uuid)
    trace_id: str = ""
    parent_span_id: str | None = None
    name: str = ""
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: SpanStatus = "in_progress"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化為 dict(供 processor 寫 JSONL 或上傳 backend)。"""
        return {
            "object": "span",
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "attributes": dict(self.attributes),
            "status": self.status,
            "error": self.error,
        }
