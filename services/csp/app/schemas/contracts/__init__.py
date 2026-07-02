# -*- coding: utf-8 -*-
"""app.schemas.contracts — 跨模組共用的契約 schema 集中地。

依 Slice 1B 計畫:契約型別(如分類等級)集中在此 package,先只放
ClassificationLevel,其餘契約隨後續 slice 進場。模組間交換資料一律
透過契約型別,不得直接依賴彼此的內部實作。
"""

from app.schemas.contracts.classification import ClassificationLevel

__all__ = ["ClassificationLevel"]
