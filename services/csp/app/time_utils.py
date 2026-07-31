# -*- coding: utf-8 -*-
"""時間正規化的單一來源。

`app/models/` 多數 `DateTime` 欄曾未宣告 `timezone=True`,DB 讀回來就是
naive datetime。程式各處要跟 `datetime.now(timezone.utc)`(aware)比較時,就得
自己補 tzinfo —— 於是 codebase 裡長出多個各自手刻的 `_as_utc` helper。

補丁漏掉的地方就是 bug。`api_key_service.validate_api_key` 就是漏掉的那個:
naive `expires_at` 與 aware `now` 比較直接 TypeError → 任何設了到期日的
API key 第一次驗證都是 500。

本模組用途是「不要再多長一個」。欄位轉成 timestamptz 之後,`as_utc()` 對
aware 輸入是 no-op,呼叫端不需要跟著改。

判讀:naive 值視同 UTC 牆鐘。寫入端是 `datetime.now(timezone.utc)`,裸
`datetime.now()` 為 0;DB `server_default CURRENT_TIMESTAMP` 在 session
TZ=UTC 下也是 UTC 牆鐘。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """現在時間,timezone-aware UTC。"""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """把可能是 naive 的 datetime 正規化成 aware UTC。

    - `None` → `None`
    - naive → 視同 UTC 補上 tzinfo
    - aware → 轉成 UTC
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    """`expires_at` 是否已過期。`None` = 永不過期。"""
    normalized = as_utc(expires_at)
    if normalized is None:
        return False
    return normalized < (as_utc(now) or utcnow())
