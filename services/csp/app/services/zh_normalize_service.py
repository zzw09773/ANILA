"""聊天落庫前的 zh-TW 靜默正規化（§6-3）。

只動 assistant；失敗只記 warning、原文落庫；ANILA_ZH_NORMALIZE=0 關閉。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("ANILA_ZH_NORMALIZE", "1") != "0"


def prepare_message_content(role: str, content: Optional[str]) -> tuple[Optional[str], int]:
    """assistant 才正規化；回傳 (content, changed_char_count)。永不拋出。"""
    if role != "assistant" or not _enabled():
        return content, 0
    if content is None or content == "":
        return content, 0
    try:
        from anila_core.text import normalize_report

        return normalize_report(content)
    except Exception:
        logger.warning(
            "zh_normalize failed; storing original text unchanged",
            exc_info=True,
        )
        return content, 0


def log_if_changed(message_id: int, changed_char_count: int) -> None:
    """有變更才 info 一行；log 不含訊息內文。"""
    if changed_char_count > 0:
        logger.info(
            "zh_normalize message_id=%s changed_chars=%s",
            message_id,
            changed_char_count,
        )
