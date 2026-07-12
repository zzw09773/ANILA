"""ANILA 五級分類的單一權威型別。"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from functools import total_ordering


@total_ordering
class ClassificationLevel(enum.Enum):
    """五級分類；成員定義順序即由低到高的排序契約。

    儲存與 wire 格式沿用 CSP 已上線的繁中字串。未知字串必須
    fail-closed，而不是默認為無機密。
    """

    UNCLASSIFIED = "無機密"
    TRADE_SECRET = "營業秘密"
    CONFIDENTIAL = "機密"
    SECRET = "極機密"
    TOP_SECRET = "絕對機密"

    @property
    def rank(self) -> int:
        """回傳 0–4 的排序值；rank 不作儲存格式。"""
        return _RANKS[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ClassificationLevel):
            return NotImplemented
        return self.rank < other.rank

    @classmethod
    def max_of(cls, levels: Iterable[ClassificationLevel]) -> ClassificationLevel:
        """套用單向閂鎖 max 規則；空集合 fail-closed。"""
        materialized = list(levels)
        if not materialized:
            raise ValueError("max_of() 需要至少一個分類等級,不允許空集合")
        return max(materialized, key=lambda level: level.rank)

    @classmethod
    def from_legacy_classified(cls, classified: bool) -> ClassificationLevel:
        """舊 boolean 的 migration floor：false→無機密、true→機密。"""
        return cls.CONFIDENTIAL if classified else cls.UNCLASSIFIED

    def to_storage(self) -> str:
        """回傳資料庫與 wire contract 使用的繁中字串。"""
        return self.value

    @classmethod
    def from_storage(cls, value: str) -> ClassificationLevel:
        """解析儲存值；未知值 fail-closed。"""
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"未知的分類等級儲存值:{value!r};"
                f"合法值為 {[level.value for level in cls]}"
            ) from None


_RANKS: dict[ClassificationLevel, int] = {
    level: index for index, level in enumerate(ClassificationLevel)
}

# F5 文件使用的領域名稱。這是同一 class 的 alias，不是第二套 enum。
Classification = ClassificationLevel

__all__ = ["Classification", "ClassificationLevel"]
