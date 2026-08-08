# -*- coding: utf-8 -*-
"""設定頁的後端：一個端點，把每一顆設定的**全部實情**講完。

這一支要消滅的形狀
==================
前四包各自關掉了一半的謊：登錄表（宣告）、泛化存取層（讀寫）、C 類每請求生效、
B 類開機覆蓋。剩下最後一個、也是最容易長出來的：**畫面上的那一欄不是後端真正
在用的那個值**。它有四種長法，這一檔逐一釘死：

1. **拿快照當生效值**。``BootOverrideSnapshot.applied`` 記的是「這次開機**套用**
   了什麼」，不是「現在生效的是什麼」。兩者在三種情形下會分岔：管理員開機後才
   存的那一列（快照裡根本沒有這個 key）、開機後又改過的值、以及消費端自己有
   樓地板的那型（``alert_detectors.py:577`` 的 ``max(15, …)``）。
   釘法：``effective`` 一律 C 走 ``get_setting``、B 走 ``getattr(settings, …)``，
   並且用「三個值互不相同」的情境（stored 23 / effective 7 / env 3600）去問。
2. **祕密走後門出來**。13 顆 A 類的值不可以出現在 payload 的**任何角落**——
   包含錯誤訊息、``default`` 欄、以及「有人繞過 API 寫進 DB 的那一列」。
   釘法：13 顆全部植入 sentinel（env ＋ ``settings`` 欄位 ＋ DB 列三路），
   然後對整份 JSON 做**字串**掃描，不是逐欄位抽查。
3. **鎖定類別其實收得下來**。釘法：非可編輯的**全名單**逐顆 PUT（76 顆，
   由登錄表推導，零手抄），每一顆都要 400、都要把鎖定理由講給人聽、
   而且不可以留下任何一列。
4. **值改了、沒有人知道是誰改的**。釘法：稽核事件與設定寫入必須同一個交易——
   稽核那一段炸掉時，設定那一列也必須不存在（帳本舊債，Task 4 之前就欠）。

⚠ **場上的預設值有哪些**（測試值一律避開全部）：登錄表／``config.py`` 的
120／30／3／0.5／60／60／100／5／``data/attachments``／``changeme``，以及 conftest
塞進 env 的 ``HEALTH_CHECK_INTERVAL=3600``／``ALERT_CHECK_INTERVAL=3600``。
所以下面用 137／47／23／7／73／4321 這幾個數字——沒有一個撞到任何一份預設值。
值撞到預設值時，「真的讀了新值」與「什麼都沒發生」會一起變綠（本專案常設規則，
帳本第二次記載）。
"""

from __future__ import annotations

import ast
import json
import os
import pathlib

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app import config as config_module
from app.config import BootOverrideSnapshot, Settings, apply_boot_overrides, settings
from app.models.platform_setting import PlatformSetting, get_setting, set_setting
from app.services import audit_service
from app.services.auto_seed import sync_env_seeded_services
from app.services.settings_registry import (
    EDITABLE_CLASSES,
    REGISTRY,
    SETTINGS,
    SettingClass,
    _BOOT_ORDER_REASON,
    _ENV_ONLY_CHANNEL_REASON,
    _INTERPRETER_REASON,
)
from tests.conftest import login, make_user

OVERVIEW_URL = "/api/platform-settings/overview"


def _put_url(key: str) -> str:
    return f"/api/platform-settings/{key}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client, db) -> str:
    make_user(db, username="platform_settings_admin", role="admin")
    return login(client, "platform_settings_admin")


@pytest.fixture()
def plain_token(client, db) -> str:
    make_user(db, username="platform_settings_plain", role="developer")
    return login(client, "platform_settings_plain")


@pytest.fixture(autouse=True)
def _settings_identity_precondition():
    """本檔用 import 期綁定的 ``settings``；先確認它就是端點會讀的那一顆。

    見 ``test_settings_boot_override.py`` 的同名 fixture：套件裡有四個檔會
    ``importlib.reload(app.config)``，前提不成立時 ``monkeypatch.setattr(settings, …)``
    會去改一顆沒有人在讀的物件，整檔以看不出原因的方式紅掉。
    """
    assert settings is config_module.settings, (
        "app.config.settings 已經不是本檔 import 期綁到的那一顆物件了"
    )


@pytest.fixture
def simulated_boot(monkeypatch):
    """跑一次「開機」：B 類欄位還原成 env／預設值，再套一次覆蓋。

    ⚠ 還原全部交給 ``monkeypatch``（含模組層快照）——``apply_boot_overrides`` 是
    就地 ``setattr`` 全域單例、並整份取代模組層快照的，沒有還原就會把
    ``ALERT_CHECK_INTERVAL=7`` 漏給整套測試。形狀取自
    ``test_settings_boot_override.py:120`` 的同名 fixture。
    """

    def _boot(db):
        fresh = Settings()
        for spec in SETTINGS:
            if spec.setting_class is not SettingClass.B_EDIT:
                continue
            if spec.env_name is None or spec.env_name not in Settings.model_fields:
                continue
            monkeypatch.setattr(
                settings, spec.env_name, getattr(fresh, spec.env_name), raising=False
            )
        monkeypatch.setattr(
            config_module, "_current_snapshot", config_module._current_snapshot
        )
        return apply_boot_overrides(db)

    return _boot


@pytest.fixture
def frozen_snapshot(monkeypatch):
    """把模組層快照換成指定的一份（測「開機失敗」「套了一半」那兩種姿態）。"""

    def _freeze(snapshot: BootOverrideSnapshot) -> None:
        monkeypatch.setattr(config_module, "_current_snapshot", snapshot)

    return _freeze


def _overview(client, token) -> dict:
    resp = client.get(OVERVIEW_URL, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _row(body: dict, key: str) -> dict:
    for item in body["items"]:
        if item["key"] == key:
            return item
    raise AssertionError(f"payload 裡沒有 {key} 這一列")


# ── 1. 全登錄表都在畫面上 ───────────────────────────────────────────────────


def test_overview_lists_every_registry_entry_exactly_once(client, admin_token):
    """少宣告一顆 = 畫面上少一列 = 又一個看不見的開關（登錄表 docstring）。"""
    body = _overview(client, admin_token)
    keys = [item["key"] for item in body["items"]]

    assert len(keys) == len(set(keys)), "同一顆設定出現兩次"
    assert set(keys) == set(REGISTRY), "payload 與登錄表對不起來"
    assert body["total"] == len(REGISTRY) == 96


def test_the_class_column_is_spelled_class_in_the_json(client, admin_token):
    """Task 6 的欄位契約：分區欄在 JSON 裡叫 ``class``。"""
    body = _overview(client, admin_token)
    row = _row(body, "proxy.llm_timeout")
    assert row["class"] == "C"
    assert "setting_class" not in row


def test_editable_is_derived_from_the_registry_not_a_hand_list(client, admin_token):
    body = _overview(client, admin_token)
    for item in body["items"]:
        spec = REGISTRY[item["key"]]
        assert item["editable"] is (spec.setting_class in EDITABLE_CLASSES)
        if spec.locked_reason:
            assert item["locked_reason"] == spec.locked_reason
        else:
            assert item["locked_reason"] is None


def test_the_page_is_admin_only(client, plain_token):
    assert client.get(OVERVIEW_URL, headers=_auth(plain_token)).status_code == 403
    refused = client.put(
        _put_url("proxy.llm_timeout"), json={"value": 137}, headers=_auth(plain_token)
    )
    assert refused.status_code == 403


# ── 2. effective 是那一欄的重心 ─────────────────────────────────────────────


def test_c_class_effective_is_the_live_db_read(client, admin_token, db):
    """C 類：改完下一個請求就生效，畫面上那個數字就是後端會用的那個。"""
    put = client.put(
        _put_url("proxy.llm_timeout"), json={"value": 137}, headers=_auth(admin_token)
    )
    assert put.status_code == 200, put.text

    row = _row(_overview(client, admin_token), "proxy.llm_timeout")
    assert row["effective"] == 137
    assert row["stored"] == "137"
    assert row["source"] == "db"
    assert row["pending"] is None, "C 類不需要重啟，不可以有待生效值"
    assert get_setting(db, "proxy.llm_timeout") == 137


def test_c_class_effective_ignores_the_orphaned_settings_field(
    client, admin_token, monkeypatch
):
    """C 類**不可以**讀 ``settings.PROXY_MAX_RETRIES``——那顆欄位已經沒有人讀了。

    Task 2／3 把 C 類的讀取點全部搬成 ``get_setting``，``config.py`` 上那 12 個
    欄位變成孤兒（``test_no_app_module_reads_the_orphaned_config_fields`` 釘住沒有
    人再讀它們）。設定頁若圖方便讀那顆欄位，畫面就會顯示一個**這個行程從來沒有
    用過的值**——而且沒有錯誤訊息。
    """
    monkeypatch.setattr(settings, "PROXY_MAX_RETRIES", 9, raising=False)

    row = _row(_overview(client, admin_token), "proxy.max_retries")
    assert row["effective"] == 3, "讀到了孤兒欄位（9），不是真正生效的登錄表預設值"
    assert row["source"] == "default"


def test_b_class_effective_stored_and_pending_are_three_different_values(
    client, admin_token, db, simulated_boot
):
    """三態分辨：**存的**、**現在生效的**、**重啟後才生效的**必須各自看得出來。

    情境：開機時套用了 7（快照 applied=7、欄位=7），開機之後管理員又改成 23。
    此刻 stored=23、effective=7、env 層是 3600——三個值互不相同，也都不等於
    登錄表預設 60。任何一種「拿快照當生效值」或「拿 stored 當生效值」的實作，
    在這裡都會露出來。
    """
    set_setting(db, "alerts.check_interval", 7)
    db.commit()
    snapshot = simulated_boot(db)
    assert snapshot.applied["alerts.check_interval"] == 7

    put = client.put(
        _put_url("alerts.check_interval"), json={"value": 23}, headers=_auth(admin_token)
    )
    assert put.status_code == 200, put.text

    row = _row(_overview(client, admin_token), "alerts.check_interval")
    assert row["stored"] == "23", "DB 列的原值"
    assert row["effective"] == 7, "現在真正在跑的是開機時套上去的那個值"
    assert row["pending"] == 23, "重啟之後才會變成 23"
    assert row["source"] == "db-boot"
    assert row["restart_required"] is True
    # PUT 的回應就是這一列（改完立刻看得到分態）。
    assert put.json() == row


def test_a_row_written_after_boot_is_pending_not_effective(client, admin_token, db):
    """開機之後才存的那一列：快照裡根本沒有這個 key。

    拿快照當生效值的實作在這裡會回 null 或整列漏掉；拿 stored 當生效值的實作
    會宣稱 47 已經在跑（實際上背景迴圈跑的是 env 的 3600）。
    """
    client.put(
        _put_url("alerts.check_interval"), json={"value": 47}, headers=_auth(admin_token)
    )

    row = _row(_overview(client, admin_token), "alerts.check_interval")
    assert row["stored"] == "47"
    assert row["effective"] == 3600, "env 層（conftest 塞的 ALERT_CHECK_INTERVAL）還在跑"
    assert row["pending"] == 47
    assert row["source"] == "env"


def test_effective_follows_the_field_the_consumers_read_not_the_snapshot(
    client, admin_token, db, simulated_boot, monkeypatch
):
    """把兩個來源**刻意**弄成不一樣，然後問畫面跟著誰走。

    ⚠ 這一支是刻意構造的，理由要說清楚：``apply_boot_overrides`` 是拿同一個值去
    ``setattr`` 的，所以正常開機之後 ``snapshot.applied[key]`` 與
    ``getattr(settings, field)`` **必然相等**——「不可以拿快照當生效值」這條規則在
    那個狀態下根本問不出來（實測：一個「優先讀 applied、否則讀欄位」的實作可以讓
    整檔全綠）。所以這裡在開機之後把欄位改掉（模擬任何一個在 hook 之後動到那顆
    單例的路徑），讓兩個來源分岔：快照說 7，欄位說 4321。消費模組手上的是**欄位**，
    畫面就必須說 4321。
    """
    set_setting(db, "alerts.check_interval", 7)
    db.commit()
    snapshot = simulated_boot(db)
    assert snapshot.applied["alerts.check_interval"] == 7

    monkeypatch.setattr(settings, "ALERT_CHECK_INTERVAL", 4321, raising=False)

    row = _row(_overview(client, admin_token), "alerts.check_interval")
    assert row["effective"] == 4321, "讀了快照的 applied，不是消費端手上的那顆欄位"
    assert row["source"] == "db-boot", "來源仍然是這次開機套上去的那一層"


def test_the_effective_column_is_the_settings_value_not_the_consumer_floor(
    client, admin_token, db, simulated_boot
):
    """⚠ **已揭露的落差**：``alert_detectors.py:577`` 是 ``max(15, …)``。

    存 7 進去，欄位上是 7、快照 applied 也是 7，但背景迴圈實際用的是 15。
    ``effective`` 照定義回 7（那是 ``settings`` 上的值，也是登錄表值域裡的值），
    登錄表沒有宣告這個樓地板、本包也不准改那一顆的文字，所以**不把 15 抄進
    payload**——抄一份消費端的規則進顯示層，正是登錄表 docstring 說的那種病。
    這一支把落差釘成**已知**，Task 5 報告列為 concern（帳本 Task 4 carry 1）。
    """
    from app.services import alert_detectors

    set_setting(db, "alerts.check_interval", 7)
    db.commit()
    simulated_boot(db)

    row = _row(_overview(client, admin_token), "alerts.check_interval")
    assert row["effective"] == 7
    consumer_floor = max(15, int(getattr(alert_detectors.settings, "ALERT_CHECK_INTERVAL")))
    assert consumer_floor == 15, "樓地板不見了——這一支的前提要重寫"
    assert row["effective"] != consumer_floor


def test_a_row_that_cannot_be_read_back_is_shown_as_unusable(client, admin_token, db):
    """繞過 API 寫進來的壞值：``effective`` 退回下一層，而 ``stored`` 照實顯示。

    不變式 4（存取層 docstring）：跑的既然不是他存的值，``source`` 就不可以說
    ``db``。畫面還要看得出「你存的那個值用不了」，否則管理員會盯著一個他以為
    生效了的數字。
    """
    db.add(PlatformSetting(key="proxy.llm_timeout", value="not-a-number"))
    db.commit()

    row = _row(_overview(client, admin_token), "proxy.llm_timeout")
    assert row["stored"] == "not-a-number"
    assert row["stored_usable"] is False
    assert row["effective"] == 120, "退回登錄表預設值"
    assert row["source"] == "default"


def test_a_broken_b_class_row_never_advertises_a_pending_value(client, admin_token, db):
    """壞掉的 B 類列不可以宣告「重啟後生效」——重啟後它一樣會被丟掉。"""
    db.add(PlatformSetting(key="usage.batch_size", value="-5"))
    db.commit()

    row = _row(_overview(client, admin_token), "usage.batch_size")
    assert row["stored"] == "-5"
    assert row["stored_usable"] is False
    assert row["pending"] is None
    assert row["effective"] == 100


# ── 3. 開機覆蓋沒載入的時候，畫面要照實說 ──────────────────────────────────


def test_a_failed_boot_override_is_reported_and_never_shown_as_applied(
    client, admin_token, db, frozen_snapshot
):
    """把「載入失敗」顯示成「沒有人設定過」，是這個包最該死的靜默成功。"""
    set_setting(db, "usage.batch_size", 73)
    db.commit()
    frozen_snapshot(
        BootOverrideSnapshot(applied={}, load_failed=True, failure_reason="OperationalError")
    )

    body = _overview(client, admin_token)
    assert body["boot_override_load_failed"] is True
    assert body["boot_override_failure_reason"] == "OperationalError"

    row = _row(body, "usage.batch_size")
    assert row["source"] != "db-boot", "這一次開機根本沒去讀設定表"
    assert row["source"] == "default"
    assert row["effective"] == 100
    assert row["pending"] == 73


def test_a_half_applied_snapshot_never_renders_as_db_boot(
    client, admin_token, db, frozen_snapshot
):
    """``load_failed=True`` 卻還帶著 applied（Task 4 review 的 P-C 形狀）。

    快照自己自相矛盾時，顯示層要選**保守**那一邊：這次開機失敗了，就不可以有
    任何一列說自己是 db-boot 來的。
    """
    set_setting(db, "usage.batch_size", 73)
    db.commit()
    frozen_snapshot(
        BootOverrideSnapshot(
            applied={"usage.batch_size": 73},
            load_failed=True,
            failure_reason="OperationalError",
        )
    )

    body = _overview(client, admin_token)
    assert all(item["source"] != "db-boot" for item in body["items"])
    assert body["boot_override_applied_count"] == 0
    # ⚠ 連**值**也不可以從那份自相矛盾的快照拿。這一行是 M4 突變（effective 改讀
    # applied）唯一殺得死的地方：正常開機時 ``applied[key]`` 與欄位上的值是**同一個
    # 值**（``apply_boot_overrides`` 就是拿它去 setattr 的），所以只有在「快照宣稱套過、
    # 行程其實沒套」的狀態下，兩者才分得開。73 從來沒有落到欄位上，畫面就不可以說它生效。
    row = _row(body, "usage.batch_size")
    assert row["effective"] == 100, "顯示了一個快照宣稱、但行程從來沒有套上去的值"


# ── 4. A 類：遮蔽在後端 ─────────────────────────────────────────────────────


def _a_specs():
    return [s for s in SETTINGS if s.setting_class is SettingClass.A]


def test_no_secret_value_appears_anywhere_in_the_overview(
    client, admin_token, db, monkeypatch
):
    """13 顆全名單植入 sentinel，對整份 JSON 做**字串**掃描。

    三路都植：``os.environ``（env 層）、``settings`` 欄位（``.env`` 檔那一層），
    以及一列繞過 API 寫進 ``platform_settings`` 的 DB 列。逐欄位抽查會漏掉
    ``default`` 欄、錯誤訊息、以及任何一個「順手回出去」的新欄位；字串掃描不會。
    """
    specs = _a_specs()
    assert len(specs) == 13, "A 類名單變了——遮蔽掃描要跟著走"

    sentinels = {}
    for index, spec in enumerate(specs):
        sentinel = f"anila-secret-sentinel-{index}-4321"
        sentinels[spec.key] = sentinel
        monkeypatch.setenv(spec.env_name, sentinel)
        if spec.env_name in Settings.model_fields:
            monkeypatch.setattr(settings, spec.env_name, sentinel, raising=False)
        db.add(PlatformSetting(key=spec.key, value=sentinel))
    db.commit()

    resp = client.get(OVERVIEW_URL, headers=_auth(admin_token))
    assert resp.status_code == 200, resp.text
    raw = resp.text
    for key, sentinel in sentinels.items():
        assert sentinel not in raw, f"{key} 的值出現在 payload 裡"

    body = resp.json()
    for spec in specs:
        row = _row(body, spec.key)
        assert row["effective"] is None
        assert row["stored"] is None
        assert row["default"] is None
        assert row["is_set"] is True


def test_is_set_is_false_when_only_the_code_default_is_in_play(
    client, admin_token, monkeypatch
):
    """``admin.password`` 的程式預設是 ``changeme``。

    把「有一個非空值」當成「已設定」，這一顆開箱就會顯示已設定——正是這個頁面
    要消滅的那種謊。判準是「有沒有人設過」：env 有值，或欄位已經不是宣告的預設。
    """
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(
        settings, "ADMIN_PASSWORD", REGISTRY["admin.password"].default, raising=False
    )

    row = _row(_overview(client, admin_token), "admin.password")
    assert row["is_set"] is False
    assert row["source"] == "default"

    monkeypatch.setenv("ADMIN_PASSWORD", "anila-not-the-default-4321")
    row = _row(_overview(client, admin_token), "admin.password")
    assert row["is_set"] is True
    assert row["source"] == "env"


def test_only_a_class_rows_carry_is_set(client, admin_token):
    body = _overview(client, admin_token)
    for item in body["items"]:
        spec = REGISTRY[item["key"]]
        if spec.setting_class is SettingClass.A:
            assert isinstance(item["is_set"], bool)
        else:
            assert item["is_set"] is None


def test_the_effective_value_has_the_type_the_registry_declares(client, admin_token):
    """96 顆全掃：``effective`` 的型別必須就是登錄表宣告的那個。

    ``effective`` 有三條來源路徑（``get_setting``／``getattr(settings, …)``／
    讀取點自己的 ``os.environ``）。任何一條走錯層——例如把 env 的原始字串直接
    回出去——型別會先露出來，而型別露出來比值錯了容易發現。
    """
    body = _overview(client, admin_token)
    for item in body["items"]:
        spec = REGISTRY[item["key"]]
        if spec.setting_class is SettingClass.A:
            continue
        value = item["effective"]
        py_type = spec.value_type.py_type
        if py_type is bool:
            assert isinstance(value, bool), f"{item['key']} 應該是 bool，收到 {value!r}"
        elif py_type is int:
            assert isinstance(value, int) and not isinstance(value, bool), (
                f"{item['key']} 應該是 int，收到 {value!r}"
            )
        elif py_type is float:
            assert isinstance(value, (int, float)) and not isinstance(value, bool), (
                f"{item['key']} 應該是數值，收到 {value!r}"
            )
        else:
            assert isinstance(value, str), f"{item['key']} 應該是字串，收到 {value!r}"


# ── 5. PUT 的類別閘門：全名單迭代，零手抄 ──────────────────────────────────


NON_EDITABLE_KEYS = sorted(
    spec.key for spec in SETTINGS if spec.setting_class not in EDITABLE_CLASSES
)
EDITABLE_KEYS = sorted(
    spec.key for spec in SETTINGS if spec.setting_class in EDITABLE_CLASSES
)


def test_the_census_matches_the_registry():
    """名單是推導出來的，不是抄的——這一支只是把數字說出來。"""
    assert len(NON_EDITABLE_KEYS) == 64
    assert len(EDITABLE_KEYS) == 32
    assert len(NON_EDITABLE_KEYS) + len(EDITABLE_KEYS) == len(REGISTRY) == 96


@pytest.mark.parametrize("key", NON_EDITABLE_KEYS)
def test_every_non_editable_key_is_refused_with_its_reason(client, admin_token, db, key):
    """B_LOCKED／SEC／A 一顆都不可以收。拒絕訊息要帶**為什麼**。"""
    spec = REGISTRY[key]
    resp = client.put(_put_url(key), json={"value": "anila-probe-4321"}, headers=_auth(admin_token))

    assert resp.status_code == 400, f"{key} 竟然收下了：{resp.text}"
    detail = resp.json()["detail"]
    assert spec.locked_reason in detail, f"{key} 的拒絕訊息沒有說為什麼"
    assert db.get(PlatformSetting, key) is None, f"{key} 被拒絕了卻留下一列"


def _probe_value_for(spec):
    """從登錄表那一筆推一個**合法而且不撞任何預設值**的探針值。

    值域來自 ``domain_fn`` 自己掛的 ``bounds``（Task 1 的形狀），不是手抄的表。
    """
    bounds = getattr(spec.domain_fn, "bounds", None)
    if bounds is not None:
        low, high = bounds
        if spec.value_type.py_type is int:
            candidate = int(low) + 7
            if candidate > high or candidate == spec.default:
                candidate = int(low) + 1 if int(low) + 1 <= high else int(low)
            return candidate
        candidate = round(float(low) + (float(high) - float(low)) * 0.37, 4)
        if candidate == spec.default:
            candidate = round(float(low) + (float(high) - float(low)) * 0.61, 4)
        return candidate
    if spec.value_type.py_type is bool:
        return not spec.default
    if spec.domain_fn.__name__ == "_is_supported_text_encoding":
        return "gbk"
    if spec.value_type.py_type is float:
        # 門檻那一顆的值域函式是別名（``_is_usable_kb_threshold``），沒有 bounds。
        return 0.61
    if spec.value_type.py_type is int:
        return 4321
    return "anila-probe-4321"


@pytest.mark.parametrize("key", EDITABLE_KEYS)
def test_every_editable_key_accepts_a_valid_write(client, admin_token, db, key):
    """C 與 B_EDIT 全名單：收得下來、存得進去、讀得回來、留得下稽核。"""
    spec = REGISTRY[key]
    value = _probe_value_for(spec)
    assert value != spec.default, f"{key} 的探針值撞到預設值 —— 會假綠"

    resp = client.put(_put_url(key), json={"value": value}, headers=_auth(admin_token))
    assert resp.status_code == 200, f"{key} 被拒絕了：{resp.text}"

    row = db.get(PlatformSetting, key)
    assert row is not None
    assert row.value == spec.value_type.format(value)
    body = resp.json()
    assert body["stored"] == row.value
    assert body["stored_usable"] is True

    events = _audit_events(db, key)
    assert len(events) == 1, f"{key} 沒有留下恰好一筆稽核事件"


def _audit_events(db, key):
    from app.models.audit_log import AuditLog

    return (
        db.query(AuditLog)
        .filter(AuditLog.action == "platform_setting_set", AuditLog.resource_id == key)
        .all()
    )


def test_an_unknown_key_is_404_and_says_where_the_names_come_from(client, admin_token):
    resp = client.put(
        _put_url("proxy.llm_timeoutt"), json={"value": 137}, headers=_auth(admin_token)
    )
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert "proxy.llm_timeoutt" in detail
    assert OVERVIEW_URL in detail, "拒絕要給做法：名單在哪裡拿"


def test_a_value_outside_the_domain_changes_nothing(client, admin_token, db):
    """拒收的寫入不可以動到那一列，也不可以動到生效值（Task 3 的樓地板前例）。"""
    client.put(_put_url("proxy.max_retries"), json={"value": 7}, headers=_auth(admin_token))

    refused = client.put(
        _put_url("proxy.max_retries"), json={"value": 99}, headers=_auth(admin_token)
    )
    assert refused.status_code == 400
    assert REGISTRY["proxy.max_retries"].description[:6] in refused.json()["detail"]

    row = _row(_overview(client, admin_token), "proxy.max_retries")
    assert row["stored"] == "7"
    assert row["effective"] == 7
    assert get_setting(db, "proxy.max_retries") == 7


def test_a_wrong_json_type_is_a_400_not_a_500(client, admin_token):
    """``_coerce_for`` 對錯型別丟的是 ``TypeError`` —— 沒接住就是 500。"""
    resp = client.put(
        _put_url("proxy.llm_timeout"), json={"value": True}, headers=_auth(admin_token)
    )
    assert resp.status_code == 400, resp.text
    assert "proxy.llm_timeout" in resp.json()["detail"]

    null = client.put(
        _put_url("proxy.llm_timeout"), json={"value": None}, headers=_auth(admin_token)
    )
    assert null.status_code == 400, null.text


# ── 6. 稽核與設定寫在同一個交易裡（帳本舊債） ──────────────────────────────


def test_a_successful_write_records_who_changed_what(client, admin_token, db):
    client.put(
        _put_url("memory.retrieve_top_k"), json={"value": 47}, headers=_auth(admin_token)
    )

    events = _audit_events(db, "memory.retrieve_top_k")
    assert len(events) == 1
    event = events[0]
    assert event.actor_username == "platform_settings_admin"
    assert event.resource_type == "platform_setting"
    metadata = json.loads(event.metadata_json)
    assert metadata["from"] == 3, "改之前生效的是登錄表預設值"
    assert metadata["to"] == 47


def test_the_setting_and_its_audit_event_share_one_transaction(
    client, admin_token, db, monkeypatch
):
    """稽核那一段炸掉時，設定那一列也必須不存在。

    「值改了但沒有人知道是誰改的」是 ``set_setting`` docstring 明寫要防的東西，
    而它只 ``flush`` 不 ``commit`` 就是為了讓呼叫端把兩件事放進同一個交易。
    先 ``commit`` 設定、再寫稽核的實作在這裡會留下一列沒有主人的設定。
    """

    class _ExplodingAuditLog:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("稽核寫入炸掉（模擬）")

    monkeypatch.setattr(audit_service, "AuditLog", _ExplodingAuditLog)

    with pytest.raises(RuntimeError):
        client.put(
            _put_url("usage.batch_size"), json={"value": 73}, headers=_auth(admin_token)
        )

    db.rollback()
    assert db.get(PlatformSetting, "usage.batch_size") is None, (
        "稽核失敗了，設定卻留了下來 —— 兩件事不在同一個交易裡"
    )


# ── 7. 孤兒欄位掃描（Task 2 carry） ────────────────────────────────────────


def _orphaned_config_fields() -> set[str]:
    """登錄表推導：C 類的讀取點已經全部搬成 ``get_setting``（Task 2／3）。

    所以凡是 C 類、而 ``config.py`` 上還留著同名欄位的，那顆欄位就是孤兒——
    再有人去讀它，畫面上的值與後端用的值就分岔了，而且沒有錯誤訊息。
    """
    return {
        spec.env_name
        for spec in SETTINGS
        if spec.setting_class is SettingClass.C
        and spec.env_name is not None
        and spec.env_name in Settings.model_fields
    }


def _settings_field_reads(path: pathlib.Path) -> list[tuple[str, int]]:
    """``settings.X`` 與 ``getattr(settings, "X", …)`` 的**全部**讀取點。

    與 Task 4 那支掃描互補：那一支只看模組層（import 期），這一支不分作用域——
    孤兒欄位在函式體裡被讀到一樣是分岔。用 AST 不用 grep，是因為註解、字串
    與縮排都會騙人。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
        ):
            found.append((node.attr, node.lineno))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "settings"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            found.append((node.args[1].value, node.lineno))
    return found


def test_no_app_module_reads_the_orphaned_config_fields():
    """Task 2 的遞延：那些欄位只剩宣告，不可以再有讀取點。"""
    orphans = _orphaned_config_fields()
    assert len(orphans) == 12, f"孤兒名單變了：{sorted(orphans)}"

    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    scanned = 0
    for path in sorted(app_dir.rglob("*.py")):
        if path.name == "config.py":
            continue  # 宣告處本身，不是讀取點
        scanned += 1
        for field, lineno in _settings_field_reads(path):
            if field in orphans:
                offenders.append(f"{path.relative_to(app_dir.parent)}:{lineno} settings.{field}")

    assert scanned > 100, "掃描器沒掃到東西——路徑錯了"
    assert offenders == [], (
        "C 類設定又有人從 config.py 的欄位讀了：\n" + "\n".join(offenders)
    )


def test_the_orphan_scanner_would_see_a_read_if_there_were_one(tmp_path):
    """反向釘：掃描器不是回空清單就結案。"""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from app.config import settings\n"
        "def f():\n"
        "    return settings.LLM_TIMEOUT\n"
        "def g():\n"
        "    return getattr(settings, 'ANILA_DEPARTMENT_MAX_DEPTH', 3)\n",
        encoding="utf-8",
    )
    fields = {field for field, _ in _settings_field_reads(probe)}
    assert {"LLM_TIMEOUT", "ANILA_DEPARTMENT_MAX_DEPTH"} <= fields


# ── 8. 降級七顆的自救路徑（Task 4 carry） ──────────────────────────────────


def _demoted_specs():
    """從登錄表推導：鎖定理由是三個「開機序／通道／直譯器」常數之一的那些。

    手抄一份七顆的名單，下一次降級就會靜靜地少一條自救說明。
    """
    reasons = (_BOOT_ORDER_REASON, _ENV_ONLY_CHANNEL_REASON, _INTERPRETER_REASON)
    return [s for s in SETTINGS if s.locked_reason.startswith(reasons)]


def test_every_demoted_entry_says_how_to_change_it(client, admin_token):
    """「不能從畫面改」要接一句「那要去哪裡改」，否則管理員只剩重開機可以試。"""
    demoted = _demoted_specs()
    assert len(demoted) == 7, f"降級名單變了：{[s.key for s in demoted]}"

    body = _overview(client, admin_token)
    for spec in demoted:
        assert spec.env_name, f"{spec.key} 沒有 env 名，自救說明無從指路"
        reason = _row(body, spec.key)["locked_reason"]
        assert "compose" in reason, f"{spec.key} 的鎖定理由沒有說去哪裡改"
        assert spec.env_name in reason, f"{spec.key} 的鎖定理由沒有指名鍵名"


# ── 9. seed.* 三顆的實際語意（Task 4 carry） ───────────────────────────────


def test_the_three_seed_entries_describe_what_editing_them_actually_does():
    """三顆的語意**不一樣**，所以說明也不可以是同一句複製。

    真碼（``auto_seed.py``）：``AUTO_REGISTER_MODELS`` 只負責建立（同名的列連端點
    都不蓋，OE-2 B3）；``AUTO_REGISTER_AGENTS`` 每次開機重新同步既有 agent 的
    多數欄位（端點除外）；``AUTO_REGISTER_LINKS`` 每次開機 upsert env 擁有的欄位。
    「僅首次開機生效」對後兩顆是**假的**——照抄那句話就是新的假控制項。
    """
    models = REGISTRY["seed.models"].description
    agents = REGISTRY["seed.agents"].description
    links = REGISTRY["seed.links"].description

    assert "只負責建立" in models
    assert "每次開機" in agents
    assert "每次開機" in links
    assert len({models, agents, links}) == 3, "三顆的說明被寫成同一句"


def test_the_links_seed_is_not_a_first_boot_only_control(db):
    """行為證據：``sync_env_seeded_services`` 第二次跑真的會改既有的列。

    這一支存在的理由是文字：說明若寫「僅首次開機生效」，這裡會紅。
    """
    from app.models.registered_service import RegisteredService

    config_v1 = [{"name": "anila-probe-link", "url": "http://probe-4321:9000", "icon": "a"}]
    sync_env_seeded_services(db, config_v1)
    db.commit()

    config_v2 = [{"name": "anila-probe-link", "url": "http://probe-7777:9000", "icon": "b"}]
    sync_env_seeded_services(db, config_v2)
    db.commit()

    row = db.query(RegisteredService).filter(
        RegisteredService.name == "anila-probe-link"
    ).one()
    assert row.entry_url == "http://probe-7777:9000", "第二次開機沒有同步 —— 說明要改"
