# -*- coding: utf-8 -*-
"""時間正規化的單一來源 —— 補救計畫 W1-4。

為什麼需要這支
--------------
`app/models/` 有 118 個 `DateTime` 欄未宣告 `timezone=True`,DB 讀回來就是
naive datetime。程式各處要跟 `datetime.now(timezone.utc)`(aware)比較時,就得
自己補 tzinfo —— 於是 codebase 裡長出 **30 個各自手刻的 `_as_utc` / `_utcnow`
類 helper** 與 **22 個檔案散落的 `replace(tzinfo=timezone.utc)`**。

補丁漏掉的地方就是 bug。`api_key_service.validate_api_key` 就是漏掉的那個:

    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):

`expires_at` 是 naive(`models/api_key.py:28`,由 migration `r1_0017` 建成
`sa.DateTime()`,而同表的 `created_at`/`last_used_at` 在 `0001` 就是
`DateTime(timezone=True)`)→ **naive < aware 直接 TypeError**:

    TypeError: can't compare offset-naive and offset-aware datetimes

也就是說**任何設了到期日的 API key,每一次驗證都是 500**,不是正常運作也不是
正常過期。而 `validate_api_key` 當時有 **0 個測試**。

這支的用途是「以後不要再多第 31 個」。W2-10 把欄位轉成 timestamptz 之後,
`as_utc()` 對 aware 輸入是 no-op,所以呼叫端不需要跟著改。
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """現在時間,timezone-aware UTC。"""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """把可能是 naive 的 datetime 正規化成 aware UTC。

    - `None` → `None`(呼叫端通常用它表示「沒有設定」)
    - naive → 視同 UTC 補上 tzinfo。**這個判讀是有碼證的**:全 CSP 的寫入端是
      210 處 `datetime.now(timezone.utc)` + 1 處 `utcnow()`,裸 `datetime.now()`
      **0 處**;DB 層的 `server_default CURRENT_TIMESTAMP` 在 session TZ=UTC 下
      也是 UTC 牆鐘。所以 naive 值就是 UTC。
    - aware → 轉成 UTC(可能來自 client 傳入的其他時區)
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_expired(expires_at: datetime | None, *, now: datetime | None = None) -> bool:
    """`expires_at` 是否已過期。`None` = 永不過期。

    存在的理由是讓呼叫端**不需要記得正規化** —— 那正是
    `validate_api_key` 出錯的方式。
    """
    normalized = as_utc(expires_at)
    if normalized is None:
        return False
    return normalized < (as_utc(now) or utcnow())
