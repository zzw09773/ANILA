"""聊天落庫前的 zh-TW 靜默正規化（§6-3）。

只動 assistant；失敗只記 warning、原文落庫；``intl.zh_normalize`` 關閉時整段跳過。

開關**每一次呼叫都重新解一次**（DB 那一列 → ``ANILA_ZH_NORMALIZE`` → 程式預設）。
以前只讀 env：值本身是每則訊息重讀沒錯，但管理員從畫面改不到它，要改就得重啟
容器。讀取點搬到 ``get_setting`` 之後，畫面上那個開關才不是裝飾。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.platform_setting import get_setting

logger = logging.getLogger(__name__)

#: 登錄表裡這一顆的 key。env 回退（``ANILA_ZH_NORMALIZE``，判準 ``!= "0"``）
#: 由 ``get_setting`` 那一層負責，這裡不再自己讀 env —— 兩邊各讀一次就會有
#: 「畫面說開、實際是關」的空間。
SETTING_KEY = "intl.zh_normalize"


def _enabled(db: Session) -> bool:
    return bool(get_setting(db, SETTING_KEY))


def prepare_message_content(
    db: Session, role: str, content: Optional[str]
) -> tuple[Optional[str], int]:
    """assistant 才正規化；回傳 (content, changed_char_count)。永不拋出。"""
    if role != "assistant" or not _enabled(db):
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
