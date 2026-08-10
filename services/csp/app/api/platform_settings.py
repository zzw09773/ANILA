# -*- coding: utf-8 -*-
"""設定頁的後端：一個端點，把每一顆設定的全部實情講完。

* ``GET /api/platform-settings/overview``  登錄表全部 96 顆，每一顆五個欄位的實情
* ``PUT /api/platform-settings/{key}``     收下 registry 全部類別；A 類另有 username 閘門

為什麼「生效值」要自己算，不能拿現成的
======================================
這個平台已經有三個地方看起來像「現在的值」，而它們**互相不等**：

* ``platform_settings`` 那一列（``stored``）—— 管理員存的。C 類存完立刻是生效值；
  B 類要等下一次開機，而且值壞掉時**永遠不會**生效。
* ``BootOverrideSnapshot.applied``（Task 4）—— 這一次開機**套用**了什麼。它不是
  生效值：開機之後才存的那一列根本不在裡面，開機之後又改過的值它也不知道。
* ``settings`` 單例的欄位 / ``get_setting`` —— 消費端真正讀的東西。

所以本模組的規則只有一條，而它是這個頁面的全部價值：
**``effective`` 一律問消費端問的那個來源**——C 類走 ``get_setting``（每請求查一次
DB，Task 2／3），B 與 SEC 走 ``getattr(settings, …)``（開機覆蓋就地寫在那一顆上，
Task 4）或該讀取點自己的 ``os.environ``。**絕不拿快照當生效值。**

快照與生效值分岔的三種情形（三支測試各釘一種）：開機**之後**才存的那一列
（快照裡根本沒有這個 key）、開機之後又被動到的欄位（快照不會知道），以及
「快照宣稱套過、行程其實沒套」的失敗姿態（``load_failed`` 卻帶著 ``applied``）。
⚠ 正常開機之後 ``applied[key]`` 與欄位上的值**必然相等**（覆蓋就是拿它去 setattr
的），所以這條規則只有在上面那三種狀態下問得出來 —— 實測過：少了那三支，
一個「優先讀 applied」的實作可以讓整個測試檔全綠。

⚠ 曾經有一種**不屬於**這個分岔的落差：``alert_detectors.py:577`` 的 ``max(15, …)``
是**消費端自己**的樓地板，``settings`` 上仍然是原值 —— 存 7 進去，畫面說 7，而迴圈
跑 15。修法不是把 15 抄進 payload（值域的唯一來源是登錄表），而是把**宣告拉齊
現實**：``alerts.check_interval`` 的值域下界改成 15（controller 2026-08-09 裁定，
理由寫在 ``settings_registry.py`` 那一顆上面）。所以今天從這個頁面存得進去的每一個
值，消費端那個 ``max`` 對它都是 no-op，它只剩安全帶的角色。釘住「宣告 ＝ 現實」的是
``test_the_declared_lower_bound_is_the_consumers_real_floor``（下界從
``alert_detectors`` 的原始碼讀出來比對：消費端改了樓地板而登錄表沒跟上就會紅）。

⚠ **A 類的遮蔽做在這裡，不是做在畫面上。** 13 顆祕密的 ``effective``／``stored``／
``default`` 一律 ``None``，只回一個 ``is_set``。遮蔽若留給前端，任何一個 curl、
任何一份 HAR、任何一次「順手把 payload 貼進 issue」都會把 ``DATABASE_URL``
（內嵌帳密）帶出去。而 ``is_set`` 的判準是「**有沒有人設過**」而不是「有沒有值」：
``admin.password`` 的程式預設是 ``changeme``，用「非空」當判準，這一顆開箱就會
顯示已設定——那正是這個頁面要消滅的謊。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, boot_override_snapshot, settings
from app.database import get_db
from app.models.platform_setting import (
    _NO_VALUE,
    _usable_or_nothing,
    PlatformSetting,
    resolve_setting,
    resolve_setting_without_db,
    set_setting,
)
from app.models.user import User
from app.schemas.base import ApiResponseModel
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.settings_registry import (
    EDITABLE_CLASSES,
    SETTINGS,
    SettingClass,
    SettingSpec,
    UnknownSettingError,
    require_spec,
)
from app.utils.client_ip import client_ip as _client_ip

OVERVIEW_PATH = "/api/platform-settings/overview"

# 讀與寫同一道門：這一份 payload 說得出每一顆安全類設定現在是什麼姿態
# （CORS 白名單、SSRF 旗標、卡登信任鏈），那是治理面的東西，不是一般使用者的。
router = APIRouter(
    prefix="/api/platform-settings",
    tags=["平台設定"],
    dependencies=[Depends(require_admin)],
)

#: ``source`` 欄的四個值。``db`` 是 C 類（每請求讀），``db-boot`` 是 B 類
#: （這一次開機套上去的）—— 兩者分開講，因為它們的「改完什麼時候生效」不同。
SOURCE_DB = "db"
SOURCE_DB_BOOT = "db-boot"
SOURCE_ENV = "env"
SOURCE_DEFAULT = "default"


class SettingItem(ApiResponseModel):
    """一顆設定在畫面上的一整列。**欄位少一個，就少講一段實情。**"""

    key: str
    # ``class`` 是 Python 保留字，只能在序列化時改名。畫面契約是 ``class``。
    setting_class: str = Field(serialization_alias="class")
    description: str
    env_name: str | None
    value_type: str
    editable: bool
    restart_required: bool
    locked_reason: str | None
    #: 程式內預設值（A 類一律 ``None``）。
    default: Any | None
    #: **現在真正生效的值**。A 類一律 ``None``。
    effective: Any | None
    #: ``platform_settings`` 那一列的原值（字串，沒有列＝``None``）。A 類一律 ``None``。
    stored: str | None
    #: 那一列讀得回來嗎。``False`` ＝ 存進去了但解不開／落在值域外，於是它**永遠
    #: 不會**生效。少了這一欄，管理員會盯著一個他以為存好了的值。
    stored_usable: bool
    #: B-可編輯專屬：存了、但這一次開機還沒套上去的值（＝重啟後才生效）。
    pending: Any | None
    source: str
    #: A 類專屬：有沒有人設過（不是「有沒有值」）。其餘類別一律 ``None``。
    is_set: bool | None
    updated_at: datetime | None
    updated_by: str | None


class PlatformSettingsOverview(ApiResponseModel):
    total: int
    #: 這一次開機**沒有**去讀設定表。畫面要照實說「這一次開機沒有載入覆蓋」，
    #: 不可以顯示成「沒有人設定過」（Task 4 的快照 docstring）。
    boot_override_load_failed: bool
    #: 只放例外的類別名 —— 連線錯誤的訊息會帶著內嵌帳密的 DSN。
    boot_override_failure_reason: str
    boot_override_applied_count: int
    items: list[SettingItem]


class SettingUpdate(BaseModel):
    """值域檢查刻意不放在 ``Field`` 上：每一顆的值域住在登錄表的 ``domain_fn``，
    而寫入端與解析端必須共用**同一個函式物件**（存取層不變式 2）。在這裡多寫
    一層 pydantic 值域，就是把那條規則複製成兩份。"""

    value: Any


# ── 生效值：一律問消費端問的那個來源 ───────────────────────────────────────


def _env_layer(spec: SettingSpec) -> Any:
    """讀取點直接讀 ``os.environ`` 的那 35 顆：用**這顆自己的**字串規則解。

    ``ANILA_ALLOW_HTTP_ENDPOINT=true`` 對 ``url_guard`` 是 **False**（``== "1"``）。
    用一套通用的 bool 解析，畫面就會把一個關著的 SSRF 旗標顯示成開著的。
    """
    if spec.env_name is None:
        return _NO_VALUE
    raw = os.environ.get(spec.env_name)
    if raw is None:
        return _NO_VALUE
    return _usable_or_nothing(spec, raw, SOURCE_ENV)


def _effective_and_source(db: Session, spec: SettingSpec, snapshot) -> tuple[Any, str]:
    """**一次解析拿兩個答案**：現在真正生效的值，以及它是哪一層來的。

    ⚠ 拆成「顯示值走一套、來源走另一套」正是 ``resolve_setting`` 的 docstring 點名
    要防的形狀（存取層不變式 3）：兩條路各自讀一次 DB，哪天其中一條被改掉，畫面
    就會出現「值是管理員存的、來源卻寫 env」這種**不會有錯誤訊息**的分歧。C 類
    因此只呼叫一次 ``resolve_setting``，兩個答案都從那一次拿。
    """
    if spec.setting_class is SettingClass.C:
        # 每請求讀 DB → env → 預設。改完下一個請求就生效的那條路。
        return resolve_setting(db, spec.key)

    if spec.setting_class is SettingClass.A:
        # 祕密值只作待部署保存，csp 目前的 consumer 仍讀 env／Settings 的開機值；
        # 所以來源必須走扣掉 DB 的同一條解析鏈，不能把「已存一列」謊報成「本次
        # 行程已套用」。值本身仍永遠遮罩，只有 is_set 另說明有人存過。
        # ``resolve_setting_without_db`` 讀的是 process env；pydantic 的 ``.env``
        # layer 只存在 ``settings`` 單例上，A 類 consumer 仍可能直接讀它。
        if (
            spec.env_name is not None
            and spec.env_name in Settings.model_fields
            and os.environ.get(spec.env_name) is None
            and getattr(settings, spec.env_name) != spec.default
        ):
            return None, SOURCE_ENV
        _, source = resolve_setting_without_db(spec.key)
        return None, source
    source = _boot_layer_source(spec, snapshot)
    if spec.env_name is not None and spec.env_name in Settings.model_fields:
        # 開機覆蓋是**就地** setattr 這一顆單例（Task 4），全樹的消費模組手上抓著
        # 的也是它。⚠ 不可以改讀 ``snapshot.applied``：那份紀錄說的是「這次開機
        # 套了什麼」，套完之後有人再動到欄位，它不會知道。
        return getattr(settings, spec.env_name), source
    value = _env_layer(spec)
    if value is _NO_VALUE:
        # env 沒設，**或**設了卻讀不回來（解不開／落在值域外）—— 兩種情況跑的都是程式
        # 預設值，來源就必須說 ``default``。⚠ 這裡曾經一律回 ``source``（＝``env``，因為
        # 那個變數確實有設）：那是存取層不變式 4 點名的謊——「跑的既然不是他設的值，
        # 就不可以說是他設的」。畫面會告訴管理員他 compose 裡那個打錯的值正在生效，
        # 而真正在跑的是程式預設。``resolve_setting`` 對 C 類早就處理對了，這條路是
        # 本模組自己走的，補上（fix round 1，實測見 test_an_unreadable_env_value_…）。
        return spec.default, SOURCE_DEFAULT
    return value, source


def _is_set(spec: SettingSpec, row: PlatformSetting | None) -> bool:
    """A 類的「有沒有人設過」。**不是**「有沒有值」——見模組 docstring。"""
    if row is not None:
        return True
    if spec.env_name is not None and os.environ.get(spec.env_name, "").strip():
        return True
    if spec.env_name is not None and spec.env_name in Settings.model_fields:
        # ``.env`` 檔那一層 ``os.environ`` 讀不到，只看得出「已經不是宣告的預設」。
        return getattr(settings, spec.env_name) != spec.default
    return False


def _boot_layer_source(spec: SettingSpec, snapshot) -> str:
    """非 C 類的來源。C 類不走這裡 —— 它的來源與值出自同一次 ``resolve_setting``。

    ⚠ ``load_failed`` 為真時，一列都不可以說自己是 db-boot。今天 Task 4 保證失敗時
    ``applied`` 必為空，但快照若哪天長出「套了一半」的姿態（review 的 P-C 形狀），
    顯示層要選保守那一邊：這次開機沒讀成，就沒有 db-boot。
    """
    if not snapshot.load_failed and spec.key in snapshot.applied:
        return SOURCE_DB_BOOT
    if spec.env_name is not None and os.environ.get(spec.env_name) is not None:
        return SOURCE_ENV
    if (
        spec.env_name is not None
        and spec.env_name in Settings.model_fields
        and getattr(settings, spec.env_name) != spec.default
    ):
        # ``pydantic`` 的 ``env_file=".env"`` 讀得到、``os.environ`` 讀不到的那一層。
        # 說成 ``default`` 會讓一個佈署真的在用的值被畫成「程式預設」。
        return SOURCE_ENV
    return SOURCE_DEFAULT


def _pending(spec: SettingSpec, stored_value: Any, snapshot) -> Any:
    """存了、但這一次開機還沒套上去的值（＝重啟之後才會生效）。

    只有 B-可編輯有這一態：C 類立刻生效，鎖定類別存了也永遠不會生效。
    """
    if spec.setting_class is not SettingClass.B_EDIT or stored_value is _NO_VALUE:
        return None
    if snapshot.load_failed or spec.key not in snapshot.applied:
        return stored_value
    if snapshot.applied[spec.key] != stored_value:
        return stored_value
    return None


def _describe(
    db: Session,
    spec: SettingSpec,
    snapshot,
    row: PlatformSetting | None,
    updated_by: str | None,
) -> SettingItem:
    masked = spec.setting_class is SettingClass.A
    stored_value = _NO_VALUE
    if row is not None:
        stored_value = _usable_or_nothing(spec, row.value, SOURCE_DB)
    # 一次解析拿兩個答案 —— 顯示值與它的來源不可以各讀各的。
    effective, source = _effective_and_source(db, spec, snapshot)
    return SettingItem(
        key=spec.key,
        setting_class=spec.setting_class.value,
        description=spec.description,
        env_name=spec.env_name,
        value_type=spec.value_type.name,
        editable=spec.setting_class in EDITABLE_CLASSES,
        restart_required=spec.restart_required,
        locked_reason=spec.locked_reason or None,
        default=None if masked else spec.default,
        effective=effective,
        stored=None if masked else (row.value if row is not None else None),
        stored_usable=stored_value is not _NO_VALUE,
        pending=None if masked else _pending(spec, stored_value, snapshot),
        source=source,
        is_set=_is_set(spec, row) if masked else None,
        updated_at=row.updated_at if row is not None else None,
        updated_by=updated_by,
    )


def _rows_and_actors(db: Session) -> tuple[dict[str, PlatformSetting], dict[str, str]]:
    """一次把 ``platform_settings`` 全部撈起來，順便解出「誰改的」。

    ⚠ 這**不是**快取：它活在一次請求裡，下一個請求會重撈。每顆設定的生效值
    仍然各自走 ``get_setting``／``getattr``（``resolve_setting`` 的主鍵查詢會命中
    同一個 session 的 identity map，值還是這一刻的真值）。行程生命期的快取才是
    設定頁的死穴——一次就替 96 個開關同時埋雷。
    """
    rows = {row.key: row for row in db.query(PlatformSetting).all()}
    actor_ids = {r.updated_by_user_id for r in rows.values() if r.updated_by_user_id}
    names: dict[int, str] = {}
    if actor_ids:
        for user in db.query(User).filter(User.id.in_(actor_ids)).all():
            names[user.id] = user.username
    return rows, {
        key: names.get(row.updated_by_user_id)
        for key, row in rows.items()
        if row.updated_by_user_id is not None
    }


@router.get("/overview", response_model=PlatformSettingsOverview)
def read_overview(db: Session = Depends(get_db)) -> PlatformSettingsOverview:
    """登錄表全部 96 顆，含那 57 顆從來沒有出現在 compose 的隱形設定。

    宣告的是**全部**而不是「可編輯的那些」：少一列 = 又一個看不見的開關。
    """
    snapshot = boot_override_snapshot()
    rows, actors = _rows_and_actors(db)
    items = [
        _describe(db, spec, snapshot, rows.get(spec.key), actors.get(spec.key))
        for spec in SETTINGS
    ]
    return PlatformSettingsOverview(
        total=len(items),
        boot_override_load_failed=snapshot.load_failed,
        boot_override_failure_reason=snapshot.failure_reason,
        boot_override_applied_count=0 if snapshot.load_failed else len(snapshot.applied),
        items=items,
    )


@router.put("/{key}", response_model=SettingItem)
def update_setting(
    key: str,
    payload: SettingUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> SettingItem:
    """改一顆設定。收不收、為什麼不收，一律照登錄表那一筆。

    值域與 A 類 username 閘門都在 ``set_setting`` 那一層（端點不自己記名單——
    手抄的名單在這個 repo 已經漏過兩次）。這裡只負責把它的拒絕理由變成 400 的
    人話，並且把稽核事件與設定寫入放進**同一個交易**。
    """
    try:
        spec = require_spec(key)
    except UnknownSettingError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"沒有 {key} 這個設定。設定名是後端給的，所以這只可能是打錯了："
                f"完整名單見 GET {OVERVIEW_PATH}。"
            ),
        )

    previous = _effective_and_source(db, spec, boot_override_snapshot())[0]
    # ⚠ **複製那個字串，不要抓著那一列**。``db.get`` 回的是 identity map 裡的同一顆
    # ORM 物件，而 ``set_setting`` 是**就地**改 ``row.value`` —— 抓著物件的話，等到下面
    # 組稽核 metadata 時，「舊值」已經變成新值了，稽核紀錄會永遠寫著 from == to。
    # 那是一條看起來有在記、其實什麼都沒記的稽核軌跡（本包要消滅的形狀，長在本包自己身上）。
    _stored_before_row = db.get(PlatformSetting, key)
    is_secret = spec.setting_class is SettingClass.A
    stored_before = (
        None
        if is_secret or _stored_before_row is None
        else _stored_before_row.value
    )
    try:
        set_setting(db, key, payload.value, actor=current_user)
    except (ValueError, TypeError) as exc:
        # ``_coerce_for`` 對錯型別丟 ``TypeError``、值域與 A 類帳號閘門丟 ``ValueError``；
        # 漏接任何一種都會變成 500，而畫面上什麼也看不到。
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except SQLAlchemyError as exc:
        # flush 期間的 constraint／connection failure 也必須明確回復這個交易；
        # 否則呼叫端會只得到裸 500，無法知道設定列沒有落地。
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{key} 沒有存起來：設定列未能在交易中寫入，請稍後重試。",
        ) from exc

    row = db.get(PlatformSetting, key)
    if row is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{key} 沒有存起來：寫入後找不到設定列，請稍後重試。",
        )
    # ``set_setting`` 已經 flush，所以這就是**這一次要落地的那個字串**。A 類只在
    # 記憶體中使用它，稽核 metadata 的 from/to/stored_before 永遠是 None。
    rendered = row.value
    stored_now = (
        _NO_VALUE
        if is_secret
        else _usable_or_nothing(spec, rendered, SOURCE_DB)
    )
    audit_metadata = {
        # ⚠ ``to`` 是**存進去的值**，不是生效值：B 類要等下一次開機，寫成
        # 生效值等於在稽核紀錄裡說一件還沒發生的事。
        "from": None if is_secret else previous,
        "to": None if is_secret or stored_now is _NO_VALUE else stored_now,
        "stored_before": None if is_secret else stored_before,
        "class": spec.setting_class.value,
        "restart_required": spec.restart_required,
    }
    # 先 flush 取得這一次事件自己的 PK，再由端點 commit；不能用「最後一列的值」或
    # 「該 key 的稽核筆數 + 1」歸屬成功。兩個並行寫入可以互相覆蓋列值／改變筆數，
    # 但各自 flush 出來的 audit PK 仍然只屬於這一次交易。
    try:
        audit_event = log_audit_event(
            db,
            commit=False,
            actor=current_user,
            action="platform_setting_set",
            resource_type="platform_setting",
            resource_id=key,
            ip_address=_client_ip(request),
            metadata=audit_metadata,
        )
        if audit_event is None:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"{key} 沒有存起來：稽核事件未寫入，設定與稽核已整筆回復。"
                    "請稍後重試。"
                ),
            )
        db.flush()
        audit_id = audit_event.id
        # flush 成功後，audit PK 是這次交易自己的可辨識事實；不要用 identity map
        # 再查同一個 session 裡剛 flush 的物件，那只會證明物件仍在 session，不能
        # 額外證明 DB 已持久化。commit 成功本身就是持久化邊界，不能在 commit 後
        # 再做一次可能暫時失敗的查詢，然後把「已存好」誤報成 500。
        if audit_id is None:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"{key} 沒有存起來：本次稽核事件沒有取得識別碼，"
                    "設定與稽核已整筆回復。請稍後重試。"
                ),
            )
        response = _describe(
            db, spec, boot_override_snapshot(), row, current_user.username
        )
        db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"{key} 沒有存起來：設定與稽核未能在同一個交易提交，已整筆回復。"
                "請稍後重試。"
            ),
        ) from exc
    # ``response`` 在 commit 前已由這次 transaction 的兩筆 row 組好；commit 成功後
    # 直接回它，不再用一個新的 post-commit read 把成功寫入誤報成失敗，也不會把並行
    # writer 的最後列值冒認成這次 request 的結果。
    return response
