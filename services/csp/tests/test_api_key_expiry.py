# -*- coding: utf-8 -*-
"""API key 到期驗證 —— 補救計畫 W1-4。

修的缺陷
--------
`api_key_service.validate_api_key` 原本寫:

    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):

`ApiKey.expires_at` 是 **naive**(`models/api_key.py:28`,由 migration
`r1_0017_gate2_pg_atomicity` 建成 `sa.DateTime()`;而同一張表的 `created_at`
與 `last_used_at` 在 `0001` 就已經是 `DateTime(timezone=True)`)。naive 與
aware 相比會直接:

    TypeError: can't compare offset-naive and offset-aware datetimes

也就是說**任何設了到期日的 API key,每一次驗證都是 HTTP 500** —— 既不是正常
運作,也不是正常過期。而 `validate_api_key` 在此之前有 **0 個測試**,所以這條
路徑壞了多久沒人知道。

為什麼會發生
------------
`app/models/` 有 118 個未宣告 `timezone=True` 的 `DateTime` 欄,於是程式各處要
自己補 tzinfo,codebase 長出 **30 個各自手刻的 `_as_utc` 類 helper** 與 22 個
檔案散落的 `replace(tzinfo=...)`。**補丁漏掉的地方就是 bug**,這裡就是漏掉的
那個。修法是把正規化收進 `app/time_utils.is_expired()`,讓呼叫端不需要記得。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.api_key import ApiKey
from app.services import api_key_service
from app.time_utils import as_utc, is_expired, utcnow

from .conftest import make_user


def _make_key(db, user, *, expires_at, naive: bool) -> tuple[ApiKey, str]:
    """用正式的 `create_api_key` 建一把 key,回傳 (物件, 明文 key)。

    `naive=True` 會把 `expires_at` 存成無 tzinfo —— 那是**真實 DB 讀回來的形狀**
    (`ApiKey.expires_at` 是 `Column(DateTime)`,PG 端是
    `timestamp without time zone`),也就是這個 bug 的觸發條件。
    """
    stored = expires_at
    if stored is not None and naive:
        stored = as_utc(stored).replace(tzinfo=None)
    key, raw = api_key_service.create_api_key(
        db, user_id=user.id, name="expiry-test", model_ids=[], expires_at=stored
    )
    return key, raw


# ── time_utils 本身 ────────────────────────────────────────────────────────
def test_is_expired_treats_none_as_never_expiring():
    assert is_expired(None) is False


def test_is_expired_accepts_naive_without_raising():
    """這是核心迴歸:naive 輸入不得再拋 TypeError。"""
    past_naive = (utcnow() - timedelta(days=1)).replace(tzinfo=None)
    future_naive = (utcnow() + timedelta(days=1)).replace(tzinfo=None)
    assert is_expired(past_naive) is True
    assert is_expired(future_naive) is False


def test_is_expired_accepts_aware_including_other_timezones():
    """W2-10 把欄位轉成 timestamptz 之後,呼叫端不需要跟著改。"""
    tz8 = timezone(timedelta(hours=8))
    past = (utcnow() - timedelta(hours=2)).astimezone(tz8)
    future = (utcnow() + timedelta(hours=2)).astimezone(tz8)
    assert is_expired(past) is True
    assert is_expired(future) is False


def test_as_utc_reads_naive_as_utc_not_local():
    """naive 一律視同 UTC —— 全 CSP 寫入端是 211 處 UTC-aware、裸 now() 0 處。"""
    naive = datetime(2026, 7, 26, 10, 0, 0)
    assert as_utc(naive) == datetime(2026, 7, 26, 10, 0, 0, tzinfo=timezone.utc)


# ── validate_api_key 端到端 ────────────────────────────────────────────────
def test_expired_naive_key_is_rejected_without_typeerror(db):
    """先前這條會 TypeError → HTTP 500。現在必須乾淨地回 None。"""
    user = make_user(db, username="expiry-owner-expired")
    _, raw = _make_key(db, user, expires_at=utcnow() - timedelta(days=1), naive=True)
    assert api_key_service.validate_api_key(db, raw) is None


def test_unexpired_naive_key_still_validates(db):
    """修 bug 不可把「還沒過期」也一起拒掉。"""
    user = make_user(db, username="expiry-owner-valid")
    key, raw = _make_key(db, user, expires_at=utcnow() + timedelta(days=30), naive=True)
    got = api_key_service.validate_api_key(db, raw)
    assert got is not None and got.id == key.id


def test_key_without_expiry_still_validates(db):
    user = make_user(db, username="expiry-owner-none")
    key, raw = _make_key(db, user, expires_at=None, naive=False)
    got = api_key_service.validate_api_key(db, raw)
    assert got is not None and got.id == key.id


def test_boundary_just_expired_is_rejected(db):
    """剛好過期一秒也要算過期 —— 邊界不可因正規化而偏移。"""
    user = make_user(db, username="expiry-owner-boundary")
    _, raw = _make_key(db, user, expires_at=utcnow() - timedelta(seconds=1), naive=True)
    assert api_key_service.validate_api_key(db, raw) is None


def test_old_comparison_would_have_raised(db):
    """把舊寫法重演一次,證明它真的會炸 —— 這條是「為什麼要修」的存證。

    若哪天有人把 `is_expired()` 改回直接比較,這支測試不會紅(它測的是舊寫法
    本身),但上面 `test_expired_naive_key_is_rejected_without_typeerror` 會紅。
    這裡的用途是讓後人一眼看懂當初的失敗模式,不必去翻 git blame。
    """
    naive = (utcnow() - timedelta(days=1)).replace(tzinfo=None)
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        _ = naive < datetime.now(timezone.utc)
