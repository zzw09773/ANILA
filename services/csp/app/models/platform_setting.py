# -*- coding: utf-8 -*-
"""平台層級設定 —— 一列一個 key，改完立刻生效。

為什麼要新建一張表
==================
這個系統原本**沒有**平台層級的設定儲存。唯一長得像的 ``users.ui_settings``
（``app/models/user.py``）是掛在使用者身上的 JSON blob、整包取代，交接文件
記著「寫第三個 key 會在下次存檔被洗掉」——那不是可以拿來擴充的前例。其餘
可調的東西一律是環境變數，而環境變數要改就得重啟容器，管理員自己動不了。

⚠ **這張表是「設定頁」那件工程的地基，所以它的第一條不變式是「讀不快取」。**
畫面上改了一個數字、後端還在讀舊值，是本平台可能出的最大的假控制項。因此
本模組的 getter 一律是一次主鍵查詢，**不做**模組層快取、不用 ``lru_cache``、
不在啟動時預讀。設定頁後面還有 41 個環境變數要照這個形狀搬進來；在這裡放
一個「只是為了效能」的快取，等於一次替 41 個開關埋下同一個地雷。
（真的量到瓶頸時的正解是在 ``set_*`` 明確失效的快取，並且要有測試證明同一
行程內改完即生效——不是把讀取搬到啟動時。）

value 一律存字串
================
型別由各個 getter 自己解讀。理由是這張表要收的是異質設定（門檻是 float、
開關是 bool、名單是 CSV），為每一種型別開一個欄位會讓表隨設定數量長大，
而 JSON 欄位又會讓「一列一個 key」退化成 ``ui_settings`` 那種整包取代。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Session

from app.database import Base
from app.models.user import User

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    # 主鍵就是設定名。命名空間用點號分段（``institutional_kb.score_threshold``），
    # 讓設定頁可以照前綴分群，而不需要另外一張分類表。
    key = Column(String(120), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    # 誰改的。帳號刪掉時設定要留著（設定不是那個人的財產），所以 SET NULL。
    updated_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ── 院內規章檢索的分數門檻 ──────────────────────────────────────────────────

KB_THRESHOLD_KEY = "institutional_kb.score_threshold"

# ⚠ **這個 0.3 是未校準的猜測。** 它是拿替代嵌入模型量出來的（PLAN.md:77），
# 真正上線的是 nv-embed，分數分布不一樣。所以 ``GET /threshold`` 會把
# ``calibrated: false`` 一起回出去 —— 一個沒有人量過的數字被當成已知數，
# 就再也沒有人會去量它。
KB_THRESHOLD_DEFAULT = 0.3


def get_kb_threshold(db: Session) -> float:
    """讀出目前的分數門檻。**每次呼叫都真的查一次 DB。**

    見模組 docstring：這裡不可以有任何形式的行程生命期快取，否則設定頁上的
    每一個開關都會變成假控制項。一次主鍵查詢的成本遠低於那個風險。

    值壞掉時（不是數字、或落在 [0, 1] 之外）退回預設值並留 log：那種列只可能
    是繞過 API 寫進去的，而讓檢索整個炸掉的代價比退回預設值高。
    """
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    if row is None:
        return KB_THRESHOLD_DEFAULT
    try:
        value = float(row.value)
    except (TypeError, ValueError):
        logger.warning(
            "platform_settings[%s] 不是數字（%r），退回預設值 %s",
            KB_THRESHOLD_KEY,
            row.value,
            KB_THRESHOLD_DEFAULT,
        )
        return KB_THRESHOLD_DEFAULT
    if not 0.0 <= value <= 1.0:
        logger.warning(
            "platform_settings[%s] = %s 落在 [0, 1] 之外，退回預設值 %s",
            KB_THRESHOLD_KEY,
            value,
            KB_THRESHOLD_DEFAULT,
        )
        return KB_THRESHOLD_DEFAULT
    return value


def is_kb_threshold_calibrated(db: Session) -> bool:
    """有沒有人真的量過。

    判準是「這一列存不存在」：沒有列 = 還是那個用替代模型量出來的預設值；
    有列 = 有一個管理員看過 ``POST /preview`` 回的實際分數之後按下儲存。
    即使他存的剛好也是 0.3 也算——差別在於有沒有人看過證據。
    """
    return db.get(PlatformSetting, KB_THRESHOLD_KEY) is not None


def set_kb_threshold(db: Session, value: float, *, actor: User | None = None) -> None:
    """寫入分數門檻。範圍由呼叫端先擋，這裡再擋一次（值域是這個設定的定義）。

    只 ``flush``、不 ``commit``：呼叫端要把設定與稽核事件寫在同一個交易裡，
    不然會出現「門檻改了但沒有人知道是誰改的」。
    """
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"分數門檻必須介於 0.0 與 1.0 之間，收到 {value}")
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    if row is None:
        row = PlatformSetting(key=KB_THRESHOLD_KEY, value=str(float(value)))
        db.add(row)
    else:
        row.value = str(float(value))
        row.updated_at = _utcnow()
    row.updated_by_user_id = actor.id if actor is not None else None
    db.flush()
