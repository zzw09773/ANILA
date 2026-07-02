# -*- coding: utf-8 -*-
"""app.schemas.contracts — 跨模組共用的契約 schema 集中地。

依 Slice 1B 計畫:契約型別(如分類等級)集中在此 package;Slice 2a 補
Task / Policy / Trace 契約。模組間交換資料一律透過契約型別,不得直接
依賴彼此的內部實作。子模組:

- ``classification`` — 五級分類等級(ClassificationLevel)
- ``tasks`` — Task / TaskRun / SourceSnapshot / Citation 契約與 enum
- ``policy`` — PolicyDecision 契約(九動作 + 三決策 enum)
- ``traces`` — TraceSpan 契約(doc 09 span event schema)
"""

from app.schemas.contracts.classification import ClassificationLevel

__all__ = ["ClassificationLevel"]
