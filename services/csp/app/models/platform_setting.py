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
import os
from datetime import datetime, timezone
from typing import Any

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

# 相似度分數的定義域，**兩端都含**。
#
# ⚠ 1.0 是刻意收的：校準中的管理員把門檻拉到 1.0（＝只認完全相同、實質上什麼都
# 不收）是合法的動作，跟拉到 0.0（全收）是同一件事的兩端。要看某個庫到底能回
# 出什麼分數的人，需要這兩個極端都按得動。
KB_THRESHOLD_MIN = 0.0
KB_THRESHOLD_MAX = 1.0


def _is_usable_kb_threshold(value: float) -> bool:
    """收得下來的值域判準。**寫入與讀取共用這一個函式，不可以各寫一份。**

    ⚠ 這不是為了少打幾個字。兩端各寫一次 ``0.0 <= v <= 1.0``，其中一邊哪天被改成
    ``<`` ，就會出現這種狀態：``PUT {"value": 1.0}`` 回 **200**、值真的存進 DB，
    而解析端判定它不可用 → 檢索跑的是預設值 0.3、畫面還告訴管理員「沒有人校準過」。
    他存的那個數字被靜默丟掉了，只留下一行 log。驗收就是這樣把 19 支測試全部
    繞過去的。**收得下來的、與算得出來的，必須是同一條規則。**
    """
    return KB_THRESHOLD_MIN <= value <= KB_THRESHOLD_MAX


def resolve_kb_threshold(db: Session) -> tuple[float, bool]:
    """一次解出「實際生效的門檻」與「有沒有人真的量過」。

    ⚠ **這兩個答案一定要出自同一次解析。** 拆成兩份各自讀 DB 的邏輯，就會有
    「畫面顯示 A、檢索用 B」或「顯示已校準、跑的卻是預設值」的空間，而那兩種
    分歧都不會有錯誤訊息。設定頁後面還有 41 個開關要照這個形狀寫。

    值壞掉時（不是數字、或落在 [0, 1] 之外）退回預設值並留 log：那種列只可能
    是繞過 API 寫進去的，而讓檢索整個炸掉的代價比退回預設值高。**但這時
    calibrated 必須是 false** —— 跑的既然是那個沒有人量過的預設值，就不可以
    跟管理員說有人量過。回 true 才是這裡真正會騙到人的地方：他會以為這個
    數字被人挑過，而畫面上那個 0.3 其實是退回來的。
    """
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    if row is None:
        return KB_THRESHOLD_DEFAULT, False
    try:
        value = float(row.value)
    except (TypeError, ValueError):
        logger.warning(
            "platform_settings[%s] 不是數字（%r），退回預設值 %s（視為未校準）",
            KB_THRESHOLD_KEY,
            row.value,
            KB_THRESHOLD_DEFAULT,
        )
        return KB_THRESHOLD_DEFAULT, False
    if not _is_usable_kb_threshold(value):
        logger.warning(
            "platform_settings[%s] = %s 落在 [0, 1] 之外，退回預設值 %s（視為未校準）",
            KB_THRESHOLD_KEY,
            value,
            KB_THRESHOLD_DEFAULT,
        )
        return KB_THRESHOLD_DEFAULT, False
    return value, True


def get_kb_threshold(db: Session) -> float:
    """讀出目前的分數門檻。**每次呼叫都真的查一次 DB。**

    見模組 docstring：這裡不可以有任何形式的行程生命期快取，否則設定頁上的
    每一個開關都會變成假控制項。一次主鍵查詢的成本遠低於那個風險。

    ⚠ **顯示與生效必須是同一個數字**，所以 API 的讀取端也走 ``resolve_kb_threshold``，
    不可以自己 `float(row.value)` 一次。差一個字，畫面上的數字就不再是檢索用的那個。

    只要 float 的呼叫端（檢索）用這一支；同時要 calibrated 的呼叫端請直接用
    ``resolve_kb_threshold``，一次解析拿兩個答案。
    """
    return resolve_kb_threshold(db)[0]


def is_kb_threshold_calibrated(db: Session) -> bool:
    """有沒有人真的量過。

    判準是「有一列**而且那一列的值是可用的**」：

    * 沒有列 → false。還是那個用替代模型量出來的預設值。
    * 有列、值可用 → true。有一個管理員看過 ``POST /preview`` 回的實際分數之後
      按下儲存。**即使他存的剛好也是預設值 0.3 也算**——差別不在數字，在有沒有
      人看過證據。
    * 有列、值不可用（繞過 API 寫進來的壞值）→ false。實際跑的是預設值，
      這時說「已校準」就是騙人。
    """
    return resolve_kb_threshold(db)[1]


def set_kb_threshold(db: Session, value: float, *, actor: User | None = None) -> None:
    """寫入分數門檻。範圍由呼叫端先擋，這裡再擋一次（值域是這個設定的定義）。

    ⚠ 值域判準走 ``_is_usable_kb_threshold`` —— 與解析端**同一個函式**。收得下來
    卻算不出來的值（存進去了、檢索卻用預設值）是這個設定唯一會靜默吃掉管理員
    輸入的地方，見那個函式的 docstring。

    只 ``flush``、不 ``commit``：呼叫端要把設定與稽核事件寫在同一個交易裡，
    不然會出現「門檻改了但沒有人知道是誰改的」。
    """
    if not _is_usable_kb_threshold(float(value)):
        raise ValueError(
            f"分數門檻必須介於 {KB_THRESHOLD_MIN} 與 {KB_THRESHOLD_MAX} 之間，收到 {value}"
        )
    row = db.get(PlatformSetting, KB_THRESHOLD_KEY)
    if row is None:
        row = PlatformSetting(key=KB_THRESHOLD_KEY, value=str(float(value)))
        db.add(row)
    else:
        row.value = str(float(value))
        row.updated_at = _utcnow()
    row.updated_by_user_id = actor.id if actor is not None else None
    db.flush()


# ── 泛化存取層 ─────────────────────────────────────────────────────────────
#
# 上面那三支函式是門檻**自己**的，已經關板（形狀被引用為範本，一行不動）。
# 下面這一段把同一個形狀泛化成「登錄表裡任何一顆設定都能用」的三支，五條不變式
# 逐條繼承：
#
# 1. **讀不快取** —— ``resolve_setting`` 每次都真的查一次 DB、每次都真的重讀一次
#    ``os.environ``。設定頁上的每一個開關都靠這件事才不是假控制項。
# 2. **收得下來的 ＝ 算得出來的** —— 值域判準是 ``spec.domain_fn``，寫入端與解析
#    端拿的是**同一個函式物件**（登錄表那一筆裡的那一個），不是各自複製的規則。
# 3. **一次解析拿兩個答案** —— ``resolve_setting`` 同時回「生效值」與「來源」。
#    顯示值與生效值必須出自同一次解析。
# 4. **壞值退回下一層，來源如實** —— DB 那一列存在但解不開或落在值域外時，退回
#    env（若設）再退回程式預設，而 ``source`` **絕不可以**還說 ``db``。門檻那一顆
#    的 ``calibrated`` 講的是同一件事：跑的既然不是他存的值，就不可以說是他設的。
# 5. **只 flush 不 commit** —— 設定與稽核事件要在同一個交易裡。
#
# ⚠ 登錄表（``app.services.settings_registry``）在**函式內**才 import：它需要本
# 模組的 ``_is_usable_kb_threshold`` 來宣告門檻那一筆別名，模組層互 import 會成環。

#: ``resolve_setting`` 回的來源。畫面上「來源」那一欄就是這三個值。
SOURCE_DB = "db"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"

#: 「這一層沒有可用的值」。用 sentinel 而不是 ``None``，因為 ``None`` 對某些
#: 設定是合法值。
_NO_VALUE = object()


def _usable_or_nothing(spec, raw: str, layer: str) -> Any:
    """把某一層的字串解成值；解不開或不在值域內就回 ``_NO_VALUE`` 並留 warning。

    **靜默成功比報錯危險**：退回下一層本身是對的（讓檢索整個炸掉的代價更高），
    但它必須留下痕跡，否則管理員會看著一個他以為設好了的值繼續用下去。
    """
    try:
        value = spec.value_type.parse(raw)
    except (TypeError, ValueError):
        logger.warning(
            "設定 %s 在 %s 層的值 %r 解不成 %s，改用下一層回退",
            spec.key,
            layer,
            raw,
            spec.value_type.name,
        )
        return _NO_VALUE
    if not spec.domain_fn(value):
        logger.warning(
            "設定 %s 在 %s 層的值 %r 落在值域之外（%s），改用下一層回退",
            spec.key,
            layer,
            value,
            spec.description,
        )
        return _NO_VALUE
    return value


def resolve_setting(db: Session, key: str) -> tuple[Any, str]:
    """一次解出「實際生效的值」與「它是哪一層來的」。

    回退鏈：``platform_settings`` 那一列 → 環境變數（若有設）→ 程式內預設值。
    env 繼續有效直到管理員第一次從畫面改 —— 氣隙升級因此不需要遷移手續。

    ⚠ **這兩個答案一定要出自同一次解析。** 拆成「顯示走一套、生效走另一套」就
    會有「畫面顯示 A、實際用 B」的空間，而那種分歧不會有錯誤訊息。
    """
    from app.services.settings_registry import require_spec

    spec = require_spec(key)

    row = db.get(PlatformSetting, key)
    if row is not None:
        value = _usable_or_nothing(spec, row.value, SOURCE_DB)
        if value is not _NO_VALUE:
            return value, SOURCE_DB

    if spec.env_name is not None:
        raw = os.environ.get(spec.env_name)
        if raw is not None:
            value = _usable_or_nothing(spec, raw, SOURCE_ENV)
            if value is not _NO_VALUE:
                return value, SOURCE_ENV

    return spec.default, SOURCE_DEFAULT


def get_setting(db: Session, key: str) -> Any:
    """讀出目前實際生效的值。**每次呼叫都真的查一次 DB、真的重讀一次 env。**

    需要「來源」的呼叫端請直接用 ``resolve_setting``，一次解析拿兩個答案；不要
    自己再查一次 DB 湊出來。
    """
    return resolve_setting(db, key)[0]


def _coerce_for(spec, value: Any) -> Any:
    """把呼叫端給的東西轉成這顆設定的值型別。**轉不了就拋，不猜。**

    字串走這顆設定自己的 ``parse``（HTTP 送上來的一律是字串）；已經是原生型別的
    就原樣收下。``bool`` 不可以當數字用 —— ``isinstance(True, int)`` 是真，讓
    ``True`` 悄悄變成 1 會存下一個沒有人打算存的值。
    """
    value_type = spec.value_type
    if isinstance(value, str):
        return value_type.parse(value)
    if value_type.py_type is bool:
        if isinstance(value, bool):
            return value
        raise TypeError(f"{spec.key} 需要布林值，收到 {type(value).__name__}")
    if isinstance(value, bool):
        raise TypeError(f"{spec.key} 需要 {value_type.name}，收到布林值")
    if value_type.py_type is float and isinstance(value, (int, float)):
        return float(value)
    if value_type.py_type is int and isinstance(value, int):
        return value
    raise TypeError(f"{spec.key} 需要 {value_type.name}，收到 {type(value).__name__}")


def set_setting(db: Session, key: str, value: Any, *, actor: User | None = None) -> None:
    """寫入一顆設定。值域由登錄表那一筆的 ``domain_fn`` 把關 —— 與解析端同一個。

    只有 C 與 B-可編輯收得下來。其餘類別（祕密、安全類、B-鎖定）在這一層就拒絕，
    端點不必自己記得名單；拒絕訊息帶著登錄表裡的鎖定理由，因為那句話要上畫面。

    只 ``flush``、不 ``commit``：呼叫端要把設定與稽核事件寫在同一個交易裡，不然
    會出現「值改了但沒有人知道是誰改的」。
    """
    from app.services.settings_registry import EDITABLE_CLASSES, require_spec

    spec = require_spec(key)
    if spec.setting_class not in EDITABLE_CLASSES:
        raise ValueError(
            f"{key} 是 {spec.setting_class.value} 類設定，不可從畫面修改："
            f"{spec.locked_reason}"
        )

    parsed = _coerce_for(spec, value)
    if not spec.domain_fn(parsed):
        raise ValueError(f"{key}：{spec.description} 收到的是 {value!r}")

    rendered = spec.value_type.format(parsed)
    row = db.get(PlatformSetting, key)
    if row is None:
        row = PlatformSetting(key=key, value=rendered)
        db.add(row)
    else:
        row.value = rendered
        row.updated_at = _utcnow()
    row.updated_by_user_id = actor.id if actor is not None else None
    db.flush()
