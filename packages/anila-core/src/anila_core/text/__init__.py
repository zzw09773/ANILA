"""文字後處理共用工具。

- ``zh_normalizer`` / ``domain_terms``：原文搭配固定域內用語，不提供完整簡繁轉換
- ``think_strip``：剝除模型的思考通道,確保它永遠到不了使用者或索引
"""

from anila_core.text.think_strip import strip_inline_think
from anila_core.text.token_count import count_text_tokens, is_cjk
from anila_core.text.zh_normalizer import normalize_report, normalize_zh_tw

__all__ = [
    "count_text_tokens",
    "is_cjk",
    "normalize_zh_tw",
    "normalize_report",
    "strip_inline_think",
]
