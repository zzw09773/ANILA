"""文字後處理共用工具。

兩批工作同時建立本模組,合併時聯集:
- ``zh_normalizer`` / ``domain_terms``：assistant 訊息落庫前的 zh-TW 正規化(s2twp＋域內用語)
- ``think_strip``：剝除模型的思考通道,確保它永遠到不了使用者或索引
"""

from anila_core.text.think_strip import strip_inline_think
from anila_core.text.zh_normalizer import normalize_report, normalize_zh_tw

__all__ = ["normalize_zh_tw", "normalize_report", "strip_inline_think"]
