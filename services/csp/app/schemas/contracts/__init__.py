# -*- coding: utf-8 -*-
"""app.schemas.contracts — 跨模組共用的契約 schema 集中地。

依 Slice 1B 計畫:舊契約 import 集中在此 package;Slice 2a 補
Task / Policy / Trace 契約。模組間交換資料一律透過契約型別,不得直接
依賴彼此的內部實作。子模組:

- ``anila_contracts`` — 五級分類等級(ClassificationLevel) 的 SSOT
- ``classification`` — CSP 舊公共 API 的相容 facade 與降級流程 schema
- ``tasks`` — Task / TaskRun / SourceSnapshot / Citation 契約與 enum
- ``policy`` — PolicyDecision 契約(九動作 + 三決策 enum)
- ``traces`` — TraceSpan 契約(doc 09 span event schema)
"""

from anila_contracts import AgentError, Classification as ClassificationLevel, StepEvent

from app.schemas.contracts.classification import (
    ClassificationEventReason,
    DeclassificationApprovedVia,
    DeclassificationStatus,
)

__all__ = [
    "AgentError",
    "ClassificationEventReason",
    "ClassificationLevel",
    "DeclassificationApprovedVia",
    "DeclassificationStatus",
    "StepEvent",
]
