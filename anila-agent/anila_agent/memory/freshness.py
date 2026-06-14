"""新鮮度標記：相對齡 + verify-before-assert 提醒。

air-gap 友善：不靠 wall-clock 隱性時間；``now_epoch`` 由呼叫端傳入（可重現、可測）。
較舊的記憶／片段附上「使用前請先驗證」的 caveat，避免把過時資訊當現況斷言。
"""

from __future__ import annotations

_DAY = 86_400
STALE_DAYS = 90


def relative_age(mtime_epoch: float, now_epoch: float) -> str:
    """人類可讀的相對齡（繁中）。"""
    delta = max(0.0, now_epoch - mtime_epoch)
    days = delta / _DAY
    if days < 1:
        return "今天"
    if days < 7:
        return f"{int(days)} 天前"
    if days < 30:
        return f"{int(days // 7)} 週前"
    if days < 365:
        return f"{int(days // 30)} 個月前"
    return f"{int(days // 365)} 年前"


def is_stale(mtime_epoch: float, now_epoch: float, *, stale_days: int = STALE_DAYS) -> bool:
    return (now_epoch - mtime_epoch) > stale_days * _DAY


def freshness_caveat(mtime_epoch: float, now_epoch: float) -> str:
    """過時則回傳提醒字串，否則空字串。"""
    if is_stale(mtime_epoch, now_epoch):
        return "（此記憶較舊，使用前請先驗證仍然成立）"
    return ""


def tag(name: str, mtime_epoch: float, now_epoch: float) -> str:
    """組一行帶相對齡 + caveat 的標記。"""
    age = relative_age(mtime_epoch, now_epoch)
    caveat = freshness_caveat(mtime_epoch, now_epoch)
    return f"{name}（{age}）{caveat}".rstrip()
