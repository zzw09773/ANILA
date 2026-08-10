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
3. **鎖定類別其實收得下來**。釘法：非可編輯的**全名單**逐顆 PUT（63 顆，
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

from app.api import platform_settings as platform_settings_api
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
from sqlalchemy.exc import SQLAlchemyError
from tests.conftest import login, make_user

OVERVIEW_URL = "/api/platform-settings/overview"


def _put_url(key: str) -> str:
    return f"/api/platform-settings/{key}"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def admin_token(client, db) -> str:
    make_user(db, username="admin", role="admin")
    return login(client, "admin")


@pytest.fixture()
def other_admin_token(client, db) -> str:
    make_user(db, username="platform_settings_other_admin", role="admin")
    return login(client, "platform_settings_other_admin")


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


def _consumer_alert_floor() -> int:
    """消費端那個樓地板 —— 讀**它宣告的常數**，不抄字面。

    2026-08-09 之前這裡是用 regex 去原始碼裡撈 ``max(15, int(getattr(settings, …)))``
    的 15。那一行已經不存在了（消費端改走 ``resolve_check_interval()``），現在讀的是
    ``alert_detectors.ALERT_INTERVAL_FLOOR_SECONDS`` 這個具名常數：消費端哪天改成 30
    而登錄表沒跟上，比對會紅——而且是符號層級的比對，不是文字比對。
    """
    from app.services import alert_detectors

    return int(alert_detectors.ALERT_INTERVAL_FLOOR_SECONDS)


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

    情境：開機時套用了 17（快照 applied=17、欄位=17），開機之後管理員又改成 23。
    此刻 stored=23、effective=17、env 層是 3600——三個值互不相同，也都不等於
    登錄表預設 60。任何一種「拿快照當生效值」或「拿 stored 當生效值」的實作，
    在這裡都會露出來。
    """
    set_setting(db, "health.check_interval", 17)
    db.commit()
    snapshot = simulated_boot(db)
    assert snapshot.applied["health.check_interval"] == 17

    put = client.put(
        _put_url("health.check_interval"), json={"value": 23}, headers=_auth(admin_token)
    )
    assert put.status_code == 200, put.text

    row = _row(_overview(client, admin_token), "health.check_interval")
    assert row["stored"] == "23", "DB 列的原值"
    assert row["effective"] == 17, "現在真正在跑的是開機時套上去的那個值"
    assert row["pending"] == 23, "重啟之後才會變成 23"
    assert row["source"] == "db-boot"
    assert row["restart_required"] is True
    # PUT 的回應就是這一列（改完立刻看得到分態）。
    assert put.json() == row


PUT_ROW_CASES = {
    # key: (送出去的值, 期望的 stored 字串)
    "proxy.llm_timeout": (137, "137"),        # C：改完立刻生效
    "usage.batch_size": (73, "73"),           # B_EDIT：改完要等重啟
}


@pytest.mark.parametrize("key", sorted(PUT_ROW_CASES))
def test_the_put_response_is_the_same_truth_as_the_overview_row(
    client, admin_token, db, key
):
    """不變式：**PUT 的回應與緊接的 overview，對同一顆 key 必須逐欄位相同。**

    brief 寫的是「回應＝該列的 overview payload」，而原本唯一的全列比對騎在一顆
    B_EDIT 上——B 類的生效值本來就不隨寫入改變（前後都是 7），比不出來。驗收的 P2
    因此可以把回應的 ``effective`` 換成寫入**之前**的值而 125 全綠：實測回應說
    ``stored=137, effective=120``，同一顆的 overview 卻說 137。管理員改完 C 類設定，
    畫面說值還是舊的 → 以為沒生效 → 再按一次。這正是本頁要消滅的形狀。

    所以這一支兩類都跑，而且比的是**整列**（未來多一個欄位也自動涵蓋），
    不是挑幾格對。
    """
    value, rendered = PUT_ROW_CASES[key]
    spec = REGISTRY[key]
    before = _row(_overview(client, admin_token), key)
    assert before["stored"] is None
    assert before["effective"] != value, f"{key} 的測試值撞到改之前的生效值 —— 會假綠"

    put = client.put(_put_url(key), json={"value": value}, headers=_auth(admin_token))
    assert put.status_code == 200, put.text
    body = put.json()
    after = _row(_overview(client, admin_token), key)

    assert body == after, "PUT 的回應與 overview 對同一顆 key 說了不同的話"
    assert body["stored"] == rendered

    if spec.setting_class is SettingClass.C:
        # 改完下一個請求就生效 —— 回應就必須已經是新值。
        assert body["effective"] == value
        assert body["pending"] is None
        assert get_setting(db, key) == value
    else:
        # 要等重啟 —— 回應必須說「還沒生效」，而不是假裝已經生效。
        assert body["effective"] != value
        assert body["pending"] == value
        assert body["restart_required"] is True


def test_a_row_written_after_boot_is_pending_not_effective(client, admin_token, db):
    """開機之後才存的那一列：快照裡根本沒有這個 key。

    拿快照當生效值的實作在這裡會回 null 或整列漏掉；拿 stored 當生效值的實作
    會宣稱 47 已經在跑（實際上背景迴圈跑的是 env 的 3600）。
    """
    client.put(
        _put_url("health.check_interval"), json={"value": 47}, headers=_auth(admin_token)
    )

    row = _row(_overview(client, admin_token), "health.check_interval")
    assert row["stored"] == "47"
    assert row["effective"] == 3600, "env 層（conftest 塞的 HEALTH_CHECK_INTERVAL）還在跑"
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
    單例的路徑），讓兩個來源分岔：快照說 17，欄位說 4321。消費模組手上的是**欄位**，
    畫面就必須說 4321。
    """
    set_setting(db, "health.check_interval", 17)
    db.commit()
    snapshot = simulated_boot(db)
    assert snapshot.applied["health.check_interval"] == 17

    monkeypatch.setattr(settings, "HEALTH_CHECK_INTERVAL", 4321, raising=False)

    row = _row(_overview(client, admin_token), "health.check_interval")
    assert row["effective"] == 4321, "讀了快照的 applied，不是消費端手上的那顆欄位"
    assert row["source"] == "db-boot", "來源仍然是這次開機套上去的那一層"


def test_the_declared_lower_bound_is_the_consumers_real_floor(client, admin_token, db):
    """**收得下來的 ＝ 跑得出來的**——這一顆的下界不是挑的，是消費端量的。

    2026-08-09 controller 裁決之前：值域宣告 1–86400，而 ``alert_detectors.py:577``
    是 ``max(15, …)``（背景迴圈啟動時算一次）。於是管理員存 7、畫面說 7、迴圈其實
    跑 15 —— **平台收下了一個它不會照辦的值，而且不說**。那是假控制項的定義，也是
    這個頁面存在的理由要消滅的東西。裁定的修法是把**宣告拉齊現實**（下界改 15），
    不是在顯示層抄一份消費端的規則（值域的唯一來源仍然只有登錄表一處）。

    ⚠ 下界不是寫死在這支測試裡的：它從 ``alert_detectors`` 的**原始碼**把那個樓地板
    讀出來比對。哪天消費端改成 ``max(30, …)`` 而登錄表沒跟上，這裡會紅 —— 否則
    「宣告 ＝ 現實」就只是修訂當天為真的一句話。
    """
    spec = REGISTRY["alerts.check_interval"]
    low, high = spec.domain_fn.bounds
    assert low == _consumer_alert_floor(), (
        "登錄表宣告的下界與消費端的樓地板對不上 —— 兩者必須是同一個數字"
    )

    refused = client.put(
        _put_url("alerts.check_interval"), json={"value": low - 8}, headers=_auth(admin_token)
    )
    assert refused.status_code == 400, refused.text
    detail = refused.json()["detail"]
    assert str(low) in detail and str(high) in detail, "拒絕訊息要把合法區間講出來"
    assert db.get(PlatformSetting, "alerts.check_interval") is None, "被拒還是留了一列"

    # 下界本身收得下來，而且存進去之後消費端那個 max 對它是 no-op：
    # 值域裡的每一個值都不會再被靜默改寫。
    ok = client.put(
        _put_url("alerts.check_interval"), json={"value": low}, headers=_auth(admin_token)
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["stored"] == str(low)
    assert max(_consumer_alert_floor(), low) == low


def test_the_alert_loop_sleeps_exactly_what_the_page_says(
    client, admin_token, db, monkeypatch
):
    """**畫面上的秒數就是那個迴圈真的睡的秒數。**

    最終審查跨家雙票的實證：env 填 5 時，設定頁顯示 5（``settings`` 上的原值），
    而 ``alert_detectors`` 的迴圈跑的是 ``max(15, 5)`` ＝ 15 —— 平台收下了一個它不
    照辦的值。修法不是在顯示層抄一份 clamp（規則就會變兩份），而是讓消費端走**同一條
    解析鏈**：``resolve_check_interval()`` 現在也是 DB → env → 預設 ＋ 同一個 domain_fn。

    這一支不比常數、比**行為**：問頁面一次、問消費端一次，兩個數字必須相等。
    三種狀態各問一次（值域外的 env／值域內的 env／管理員從畫面存的值）。
    """
    from sqlalchemy.orm import sessionmaker

    from app.services import alert_detectors

    # ⚠ 消費端自己開 session（``SessionLocal``），而那指向 conftest 的 session 級檔案 DB，
    # 不是本測試 fixture 那顆引擎——不接上去的話，第 (3) 段會讀不到剛存的那一列，
    # 而那正是這一支最要問的一段。接到同一顆引擎上，問的才是「同一個 DB、兩條路」。
    monkeypatch.setattr(
        alert_detectors,
        "SessionLocal",
        sessionmaker(bind=db.get_bind(), expire_on_commit=False),
    )

    # (1) 值域外的 env（審查用的那個 5）：兩邊都必須落回登錄表預設 60。
    monkeypatch.setenv("ALERT_CHECK_INTERVAL", "5")
    row = _row(_overview(client, admin_token), "alerts.check_interval")
    assert row["effective"] == alert_detectors.resolve_check_interval()
    assert row["effective"] == 60, "值域外的 env 要退回程式預設，兩邊一起退"
    assert row["source"] == "default"

    # (2) 值域內的 env：兩邊都用它。37 不等於預設 60、也不等於 conftest 的 3600。
    monkeypatch.setenv("ALERT_CHECK_INTERVAL", "37")
    row = _row(_overview(client, admin_token), "alerts.check_interval")
    assert row["effective"] == 37
    assert row["effective"] == alert_detectors.resolve_check_interval()
    assert row["source"] == "env"

    # (3) 管理員從畫面存的值 —— 這一顆現在是 C 類，改完下一輪就生效。
    put = client.put(
        _put_url("alerts.check_interval"), json={"value": 41}, headers=_auth(admin_token)
    )
    assert put.status_code == 200, put.text
    assert put.json()["effective"] == 41
    assert put.json()["restart_required"] is False
    assert alert_detectors.resolve_check_interval() == 41, "迴圈還在讀舊來源"


def test_the_revocation_publisher_uses_exactly_what_the_page_says(
    client, admin_token, db, monkeypatch
):
    """撤銷發布的 Redis 逾時：畫面上那個數字就是連線真的用的那個。

    最終審查實證：env 填 999 時，設定頁照登錄表值域（0.1–60）退回顯示 2.0，而
    ``token_revocation_publisher`` 的模組常數直接拿 999 去連 Redis —— 畫面與實跑分歧。
    模組常數已經拿掉；值改由**手上有 session 的呼叫端**在 ``db.commit()`` **之前**解析
    （Task 3 的 pool 教訓），以必填關鍵字往下傳。
    """
    from app.services import token_revocation, token_revocation_publisher

    captured: dict = {}

    class _FakeClient:
        def publish(self, *args, **kwargs):
            return 1

        def close(self):
            return None

    def _fake_factory(redis_url=None, *, timeout):
        captured["timeout"] = timeout
        return _FakeClient()

    monkeypatch.setattr(
        token_revocation_publisher, "_make_sync_redis_client", _fake_factory
    )

    def _page_value() -> float:
        return _row(_overview(client, admin_token), "queue.token_revocation_redis_timeout")[
            "effective"
        ]

    # (1) 值域外的 env（審查用的那個 999）：兩邊都退回 2.0。
    monkeypatch.setenv("TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS", "999")
    user = make_user(db, username="revoke_probe_one")
    token_revocation.commit_token_revocation(db, user)
    assert captured["timeout"] == _page_value() == 2.0, (
        "連線用的逾時與畫面顯示的不一樣"
    )

    # (2) 管理員從畫面存一個值域內的值 —— 這一顆現在是 C 類。
    put = client.put(
        _put_url("queue.token_revocation_redis_timeout"),
        json={"value": 7.5},
        headers=_auth(admin_token),
    )
    assert put.status_code == 200, put.text
    other = make_user(db, username="revoke_probe_two")
    token_revocation.commit_token_revocation(db, other)
    assert captured["timeout"] == _page_value() == 7.5


def test_the_timeout_is_resolved_before_the_pool_releasing_commit(db, monkeypatch):
    """順序釘：**解析 → commit → 發布**。Task 3 的形狀，搬到這個新站點。

    ``db.commit()`` 會把那條池化連線還回池子。把 ``get_setting`` 搬到 commit **之後**，
    每一次撤銷都會在 Redis publish 還在飛的時候又借一條連線——而所有數值斷言都還是綠的
    （commit 前後 ``get_setting`` 回同一個數字）。驗收的 Probe A 就是這樣活下來的。
    所以這一支不看值，看**先後**。
    """
    from app.models import platform_setting as ps_module
    from app.services import token_revocation, token_revocation_publisher

    order: list[str] = []
    real_resolve = ps_module.resolve_setting

    def _spy_resolve(session, key):
        if key == "queue.token_revocation_redis_timeout":
            order.append("resolve")
        return real_resolve(session, key)

    monkeypatch.setattr(ps_module, "resolve_setting", _spy_resolve)

    real_commit = type(db).commit

    def _spy_commit(self, *args, **kwargs):
        order.append("commit")
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(type(db), "commit", _spy_commit)

    class _FakeClient:
        def publish(self, *args, **kwargs):
            order.append("publish")
            return 1

        def close(self):
            return None

    monkeypatch.setattr(
        token_revocation_publisher,
        "_make_sync_redis_client",
        lambda redis_url=None, *, timeout: _FakeClient(),
    )

    user = make_user(db, username="revoke_order_probe")
    order.clear()
    token_revocation.commit_token_revocation(db, user)

    assert "resolve" in order and "publish" in order, f"探針沒被走到：{order!r}"
    i = order.index("publish")
    assert order[:i + 1][-3:] == ["resolve", "commit", "publish"], (
        f"解析／commit／發布的先後是 {order!r} —— 值在連線還回池子之後才解的話，"
        "每一次撤銷都會在 Redis publish 期間握著第二條池化連線"
    )


def test_the_alert_loop_re_reads_the_interval_every_cycle(monkeypatch):
    """「改完下一輪就生效」是**寫在管理員畫面上的承諾**，所以它要有釘子。

    ⚠ 修訂前全樹**沒有任何測試碰過 ``_alert_detector_loop``**：把
    ``resolve_check_interval()`` 提到 ``while`` 外面（＝退回開機時決定一次的老樣子），
    整套測試一個都不會紅，而登錄表的說明還在跟管理員說「下一輪就生效」。
    """
    import asyncio

    from app.services import alert_detectors

    seen: list[int] = []
    sleeps = {"n": 0}

    def _fake_resolve():
        seen.append(len(seen))
        return 15

    async def _stop_after_three(_seconds):
        # ⚠ 停止條件要**獨立於被測的那個呼叫**：早先的版本是「解析滿三次就停」，
        # 於是把解析提到迴圈外的突變會讓這一支**跑不完**（掛住），而不是變紅。
        # 掛住的測試比紅的測試難查得多。改成數 sleep 的次數。
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise asyncio.CancelledError
        return None

    monkeypatch.setattr(alert_detectors, "resolve_check_interval", _fake_resolve)
    monkeypatch.setattr(alert_detectors, "evaluate_database", lambda *a, **k: [])
    monkeypatch.setattr(alert_detectors, "evaluate_disk", lambda *a, **k: [])

    async def _no_ingress():
        return []

    monkeypatch.setattr(alert_detectors, "evaluate_platform_ingress", _no_ingress)
    monkeypatch.setattr(alert_detectors.asyncio, "sleep", _stop_after_three)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(alert_detectors._alert_detector_loop())

    assert len(seen) >= 3, (
        f"三輪只解析了 {len(seen)} 次 —— 間隔是開機時決定一次的，不是每輪重問"
    )


def test_the_alert_fail_safe_falls_through_env_not_straight_to_default(monkeypatch):
    """DB 打嗝時退回**下一層**（env），不是一路跳到程式預設。

    直接跳等於一次跳過兩層：運維寫在 compose 的 37 會被當成不存在，畫面（走得到 DB）
    顯示 37、迴圈睡 60，沒有任何錯誤訊息 —— 這個包要消滅的形狀，換到故障路徑上發生。
    """
    from app.services import alert_detectors

    class _ExplodingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("DB 打嗝（模擬）")

        def query(self, *args, **kwargs):
            raise RuntimeError("DB 打嗝（模擬）")

        def close(self):
            return None

    monkeypatch.setattr(alert_detectors, "SessionLocal", lambda: _ExplodingSession())

    # env 有一個合法值 → 退到 env 那一層，不是程式預設 60。
    monkeypatch.setenv("ALERT_CHECK_INTERVAL", "37")
    assert alert_detectors.resolve_check_interval() == 37, (
        "跳過了 env 層 —— compose 裡的值在 DB 打嗝時被當成不存在"
    )

    # env 的值不可用（值域外）→ 這時才輪到程式預設。
    monkeypatch.setenv("ALERT_CHECK_INTERVAL", "5")
    assert alert_detectors.resolve_check_interval() == 60

    monkeypatch.delenv("ALERT_CHECK_INTERVAL", raising=False)
    assert alert_detectors.resolve_check_interval() == 60


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


# ── 2b. env 那一層：29 顆設定的生效值走這條路 ──────────────────────────────


def _env_layer_specs():
    """走 ``_env_layer`` 的那一批：非 C、非 A，且 ``Settings`` 上**沒有**這個欄位。

    名單由登錄表推導。這批的讀取點（``url_guard``／``card_auth``／``ocr``）自己讀
    ``os.environ``，而且**每一顆的真值判準都不一樣**——登錄表存在的第一個理由。
    """
    return [
        spec
        for spec in SETTINGS
        if spec.setting_class not in (SettingClass.C, SettingClass.A)
        and spec.env_name is not None
        and spec.env_name not in Settings.model_fields
    ]


#: 每一顆走的是**它自己那個讀取點**的字串規則。這裡刻意把五種互不相容的真值判準
#: 全部拉出來問一次，而且值一律不等於登錄表預設（否則「讀了 env」與「回了預設」
#: 會一起變綠 —— 驗收的 P3 就是這樣讓 ``_env_layer`` 整段變成死碼還 125 全綠的）。
ENV_LAYER_CASES = [
    # (key, env 字串, 期望的生效值, 說明)
    ("network.allow_http_model_endpoint", "1", True, "== '1' 判準：1 才是開"),
    ("network.allow_private_endpoint", " 1 ", True, "== '1' 判準會先 strip"),
    # SEC 類的開發豁免：驗收 P3 實際翻掉的就是這一顆。
    ("auth.allow_dev_secret", "1", True, "開發祕密豁免真的開著時要說開著"),
    # 兄弟旗標，判準差一個 strip()。
    ("card.dev_trust_test_ca", " yes ", True, "card_auth:109 有 strip"),
    ("card.dev_skip_nonce_binding", "true", True, "沒有空白就收得下來"),
    ("ingestion.pdf_ocr_fallback", "TRUE", True, "lower()=='true'：大小寫不拘"),
    ("ingestion.vision_verify_ssl", "false", False, "預設 True，env 關掉要看得見"),
    # 非布林的型別也問。⚠ 這條路上**今天沒有 float 顆了**——原本那顆
    # （queue.token_revocation_redis_timeout）在 2026-08-09 重接線後升成 C 類，
    # 走的是 resolve_setting 那條（另有測試）。所以這裡是兩顆 int ＋一顆 str。
    ("ingestion.pdf_ocr_concurrency", "7", 7, "int"),
    ("ingestion.pdf_ocr_dpi", "137", 137, "int"),
    ("memory.llm_model", "anila-probe-model-4321", "anila-probe-model-4321", "str"),
]


def test_the_env_layer_covers_the_keys_this_file_thinks_it_covers():
    """名單是推導的；這一支只是把「29 顆」說出來，並確認案例真的落在這批裡。"""
    specs = {spec.key for spec in _env_layer_specs()}
    assert len(specs) == 28, f"走 env 層的顆數變了：{len(specs)}"
    for key, *_ in ENV_LAYER_CASES:
        assert key in specs, f"{key} 不走 _env_layer —— 這一支問錯路了"


@pytest.mark.parametrize(
    "key,raw,expected,why",
    ENV_LAYER_CASES,
    ids=[f"{k}={r!r}" for k, r, _e, _w in ENV_LAYER_CASES],
)
def test_the_env_layer_reads_each_key_with_its_own_string_rule(
    client, admin_token, monkeypatch, key, raw, expected, why
):
    """畫面上的值必須跟著**那一顆自己的** ``parse`` 走，不是跟著登錄表預設走。

    這條路覆蓋 29 顆（``url_guard``／卡登／OCR 的讀取點），而在 fix round 1 之前
    **沒有任何測試用非預設值問過它**：把整段 ``_env_layer`` 換成「回 `_NO_VALUE`」
    ——等於這一層變死碼、29 顆全部退回程式預設——125 個測試一個都沒紅，
    而 SEC 類的 ``auth.allow_dev_secret`` 從 True 翻成 False（豁免實際開著、
    畫面說關著），``source`` 還照樣寫 ``env``。值與來源互相矛盾，且沒有錯誤訊息。
    """
    spec = REGISTRY[key]
    assert expected != spec.default, f"{key} 的期望值撞到登錄表預設 —— 會假綠（{why}）"
    monkeypatch.setenv(spec.env_name, raw)

    row = _row(_overview(client, admin_token), key)
    assert row["effective"] == expected, f"{key}：{why}"
    assert row["source"] == "env", "值來自 env，來源就要說 env"


#: 「這個字串在這一顆是**關**」的案例。單獨問分不出來——那三顆的登錄表預設也是
#: ``False``，所以「真的按規則讀成關」與「根本沒讀、回了預設」會一起變綠（這正是
#: 驗收 P3 的假綠機制）。所以一律成對問：同一顆、兩個字串、一關一開。
ENV_LAYER_FLIP_CASES = [
    # (key, 讀成關的字串, 讀成開的字串, 為什麼)
    # ⚠ 模組 docstring 自己點名的那個案例：url_guard 的判準是 ``strip() == "1"``，
    # 所以 "true" 在這一顆是**關**。用通用 bool 解析的畫面會說它開著。
    ("network.allow_http_model_endpoint", "true", "1", "== '1'：true 是關、1 才是開"),
    # 兄弟旗標差一個 ``strip()``：帶空白的 " true " 在這一顆是關，去掉空白才是開。
    ("card.dev_skip_nonce_binding", " true ", "true", "card_auth:121 沒有 strip"),
    # ocr 的判準是 ``lower() == "true"``：這一顆的 "1" 是關。
    ("ingestion.pdf_ocr_fallback", "1", "true", "lower()=='true'：1 是關"),
]


@pytest.mark.parametrize(
    "key,raw_off,raw_on,why",
    ENV_LAYER_FLIP_CASES,
    ids=[k for k, _o, _n, _w in ENV_LAYER_FLIP_CASES],
)
def test_a_string_that_reads_as_off_is_not_the_same_as_no_value_at_all(
    client, admin_token, monkeypatch, key, raw_off, raw_on, why
):
    """同一顆、兩個字串、一關一開 —— 兩個斷言一起才問得出「規則真的被套用了」。

    這三顆的登錄表預設都是 ``False``，所以「按規則讀成關」與「這一層是死碼、
    回了預設」單看一次是分不出來的。成對問就分得出來：死碼實作在「開」那一邊
    只能回 False。⚠ 這也是本檔第二次踩到同一條帳本規則（值不可以等於場上的預設），
    第一次是 M4；差別是這次由測試自己的守衛當場攔下來。
    """
    spec = REGISTRY[key]

    monkeypatch.setenv(spec.env_name, raw_off)
    off = _row(_overview(client, admin_token), key)
    assert off["effective"] is False, f"{key}：{why}"

    monkeypatch.setenv(spec.env_name, raw_on)
    on = _row(_overview(client, admin_token), key)
    assert on["effective"] is True, f"{key}：{why}"
    assert on["effective"] != spec.default, "「開」那一邊必須不等於預設，否則整支假綠"


def test_an_unreadable_env_value_is_reported_as_default_not_as_env(
    client, admin_token, monkeypatch
):
    """env 有設但讀不回來時，``source`` **不可以**還說 env。

    存取層不變式 4：跑的既然不是他設的值，就不可以說是他設的。這一格在 fix round 1
    之前是錯的（實測 ``PDF_OCR_DPI=not-a-number`` → ``effective=200``、``source='env'``）：
    畫面會告訴管理員他 compose 裡那個打錯的值正在生效，而真正在跑的是程式預設 ——
    正是這個頁面要消滅的那種「不會有錯誤訊息的分歧」。
    """
    monkeypatch.setenv("PDF_OCR_DPI", "not-a-number")
    row = _row(_overview(client, admin_token), "ingestion.pdf_ocr_dpi")
    assert row["effective"] == 200, "壞值要退回程式預設"
    assert row["source"] == "default", "退回預設了，來源就不可以說 env"

    # 值域外的值同理（解得開、但不在值域裡）。
    monkeypatch.setenv("TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS", "999")
    row = _row(_overview(client, admin_token), "queue.token_revocation_redis_timeout")
    assert row["effective"] == 2.0
    assert row["source"] == "default"


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


def _plant_a_class_sentinels(db, monkeypatch) -> dict[str, str]:
    """13 顆祕密**三路**植入可辨識的值，回 ``{key: sentinel}``。

    三路：``os.environ``（env 層）、``settings`` 欄位（``.env`` 檔那一層）、以及一列
    繞過 API 寫進 ``platform_settings`` 的 DB 列。名單由登錄表推導並釘住顆數 ——
    多一顆祕密而沒有人告訴掃描器，是這個頁面最貴的那種漏。
    """
    specs = _a_specs()
    assert len(specs) == 13, "A 類名單變了——遮蔽掃描要跟著走"
    sentinels: dict[str, str] = {}
    for index, spec in enumerate(specs):
        sentinel = f"anila-secret-sentinel-{index}-4321"
        sentinels[spec.key] = sentinel
        monkeypatch.setenv(spec.env_name, sentinel)
        if spec.env_name in Settings.model_fields:
            monkeypatch.setattr(settings, spec.env_name, sentinel, raising=False)
        db.add(PlatformSetting(key=spec.key, value=sentinel))
    db.commit()
    return sentinels


def test_no_secret_value_appears_anywhere_in_the_overview(
    client, admin_token, db, monkeypatch
):
    """13 顆全名單植入 sentinel，對整份 JSON 做**字串**掃描。

    三路都植：``os.environ``（env 層）、``settings`` 欄位（``.env`` 檔那一層），
    以及一列繞過 API 寫進 ``platform_settings`` 的 DB 列。逐欄位抽查會漏掉
    ``default`` 欄、錯誤訊息、以及任何一個「順手回出去」的新欄位；字串掃描不會。
    """
    sentinels = _plant_a_class_sentinels(db, monkeypatch)
    specs = _a_specs()

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


def test_no_secret_value_escapes_through_any_response_this_router_returns(
    client, other_admin_token, db, monkeypatch
):
    """遮蔽是**這個 router 的**不變式，不是「overview 這一支」的不變式。

    非固定維運帳號的 A 類 PUT 必須是 400，而 400 的 detail 正是最容易被善意加料的地方
    （「順便告訴他目前值是什麼」）。驗收的 P4 證實那條路今天沒有守門員：
    讓 detail 附上 ``os.environ.get(env_name)``，``SECRET_KEY`` 的 sentinel 直接出現在
    回應 body，而 125 個測試一個都沒紅。所以掃描要跟著**表面**走，不是跟著端點走：
    13 顆各發一次 PUT、掃 400 的原始字串，順帶把未知 key 的 404 也掃過
    （它會把使用者送上來的 key 原樣回出去，是同一類的加料面）。
    """
    sentinels = _plant_a_class_sentinels(db, monkeypatch)

    surfaces: list[tuple[str, str]] = []
    for spec in _a_specs():
        refused = client.put(
            _put_url(spec.key),
            json={"value": "anila-probe-4321"},
            headers=_auth(other_admin_token),
        )
        assert refused.status_code == 400, f"{spec.key} 竟然不是 400：{refused.text}"
        assert "username=admin" in refused.json()["detail"]
        surfaces.append((f"PUT {spec.key} 的 400", refused.text))

    unknown = client.put(
        _put_url("auth.secret_keyy"), json={"value": 1}, headers=_auth(other_admin_token)
    )
    assert unknown.status_code == 404
    surfaces.append(("未知 key 的 404", unknown.text))

    for label, raw in surfaces:
        for key, sentinel in sentinels.items():
            assert sentinel not in raw, f"{key} 的值從「{label}」漏出去了"


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


EDITABLE_KEYS = sorted(spec.key for spec in SETTINGS)


def test_the_census_matches_the_registry():
    """名單是推導出來的，不是抄的——這一支只是把數字說出來。"""
    assert len(EDITABLE_KEYS) == len(REGISTRY) == 96
    assert all(spec.setting_class in EDITABLE_CLASSES for spec in SETTINGS)


def test_locked_and_security_classes_are_editable_but_keep_their_reason(
    client, admin_token
):
    body = _overview(client, admin_token)
    for item in body["items"]:
        if item["class"] in {"B_LOCKED", "SEC"}:
            assert item["editable"] is True
            assert item["locked_reason"]
            if item["class"] == "SEC":
                assert "platform_settings" in item["locked_reason"]
                assert "不會自動套用" in item["locked_reason"]


def test_secret_writes_require_the_literal_admin_username(
    client, admin_token, other_admin_token, db
):
    refused = client.put(
        _put_url("admin.password"),
        json={"value": "anila-secret-probe-4321"},
        headers=_auth(other_admin_token),
    )
    assert refused.status_code == 400, refused.text
    assert "username=admin" in refused.json()["detail"]
    assert db.get(PlatformSetting, "admin.password") is None

    accepted = client.put(
        _put_url("admin.password"),
        json={"value": "anila-secret-probe-4321"},
        headers=_auth(admin_token),
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["is_set"] is True
    assert body["effective"] is None
    assert body["stored"] is None
    assert "anila-secret-probe-4321" not in accepted.text


def test_b_locked_write_is_accepted_and_bad_security_value_is_refused(
    client, admin_token, db
):
    accepted = client.put(
        _put_url("alerts.smtp_port"), json={"value": 2525}, headers=_auth(admin_token)
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["editable"] is True
    assert accepted.json()["locked_reason"]
    assert db.get(PlatformSetting, "alerts.smtp_port").value == "2525"

    refused = client.put(
        _put_url("auth.access_token_expire_minutes"),
        json={"value": -1},
        headers=_auth(admin_token),
    )
    assert refused.status_code == 400, refused.text
    assert "正整數" in refused.json()["detail"]
    assert db.get(PlatformSetting, "auth.access_token_expire_minutes") is None


def test_secret_source_describes_the_runtime_fallback_not_the_staged_db_row(
    client, admin_token, db, monkeypatch
):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(
        settings, "ADMIN_PASSWORD", REGISTRY["admin.password"].default, raising=False
    )
    accepted = client.put(
        _put_url("admin.password"),
        json={"value": "anila-staged-secret-4321"},
        headers=_auth(admin_token),
    )
    assert accepted.status_code == 200, accepted.text
    row = accepted.json()
    assert row["is_set"] is True
    assert row["source"] == "default"
    assert row["effective"] is None
    assert row["stored"] is None
    assert "anila-staged-secret-4321" not in accepted.text


def test_secret_source_reports_the_settings_file_layer(monkeypatch, client, admin_token):
    """A 類 consumer 可能拿的是 pydantic Settings 的 ``.env`` 值，不是 os.environ。"""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    settings_file_value = "anila-settings-file-secret-4321"
    monkeypatch.setattr(settings, "ADMIN_PASSWORD", settings_file_value, raising=False)

    response = client.get(OVERVIEW_URL, headers=_auth(admin_token))
    assert response.status_code == 200, response.text
    row = _row(response.json(), "admin.password")
    assert row["source"] == "env"
    assert row["is_set"] is True
    assert settings_file_value not in response.text


def test_invalid_seed_json_is_rejected_before_it_can_be_stored(client, admin_token, db):
    refused = client.put(
        _put_url("seed.models"),
        json={"value": "{not-json"},
        headers=_auth(admin_token),
    )
    assert refused.status_code == 400, refused.text
    assert "{not-json" in refused.json()["detail"]
    assert db.get(PlatformSetting, "seed.models") is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ingestion.vision_url", "http://[broken"),
        ("queue.redis_url", "redis://[broken"),
    ],
)
def test_malformed_url_values_are_rejected_without_crashing_the_endpoint(
    client, admin_token, db, key, value
):
    refused = client.put(
        _put_url(key), json={"value": value}, headers=_auth(admin_token)
    )
    assert refused.status_code == 400, refused.text
    assert db.get(PlatformSetting, key) is None


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
    choices = getattr(spec.domain_fn, "choices", ())
    if choices:
        return next(value for value in choices if value != spec.default)
    if spec.value_type.py_type is bool:
        return not spec.default
    if spec.domain_fn.__name__ == "_is_url_or_empty":
        return "https://example.test/anila-probe"
    if spec.domain_fn.__name__ == "_is_redis_url":
        return "redis://redis:6379/0"
    if spec.domain_fn.__name__ == "_is_csv":
        return "anila-probe-4321,anila-probe-4322"
    if spec.domain_fn.__name__ == "_positive_int":
        return 4321
    if spec.domain_fn.__name__ == "_is_supported_text_encoding":
        return "gbk"
    if spec.domain_fn.__name__ == "_is_seed_models":
        return '[{"name":"anila-probe-model","endpoint_url":""}]'
    if spec.domain_fn.__name__ == "_is_seed_agents":
        return '[{"name":"anila-probe-agent","endpoint_url":""}]'
    if spec.domain_fn.__name__ == "_is_seed_links":
        return '[{"name":"anila-probe-link","url":"http://probe.example"}]'
    if spec.domain_fn.__name__ == "_is_seed_api_keys":
        return '[{"username":"anila-probe-user","key":"sk-anila-probe"}]'
    if spec.value_type.py_type is float:
        # 門檻那一顆的值域函式是別名（``_is_usable_kb_threshold``），沒有 bounds。
        return 0.61
    if spec.value_type.py_type is int:
        return 4321
    return "anila-probe-4321"


@pytest.mark.parametrize("key", EDITABLE_KEYS)
def test_every_editable_key_accepts_a_valid_write(client, admin_token, db, key):
    """全 registry 名單：收得下來、存得進去、讀得回來、留得下稽核。"""
    spec = REGISTRY[key]
    value = _probe_value_for(spec)
    assert value != spec.default, f"{key} 的探針值撞到預設值 —— 會假綠"

    resp = client.put(_put_url(key), json={"value": value}, headers=_auth(admin_token))
    assert resp.status_code == 200, f"{key} 被拒絕了：{resp.text}"

    row = db.get(PlatformSetting, key)
    assert row is not None
    assert row.value == spec.value_type.format(value)
    body = resp.json()
    if spec.setting_class is SettingClass.A:
        assert body["stored"] is None
        assert body["effective"] is None
        assert body["is_set"] is True
    else:
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


BOOL_KEYS = sorted(s.key for s in SETTINGS if s.value_type.py_type is bool)

#: 「關」的寫法。⚠ 這一組**不是**讀取端那五種判準——那些是消費模組怎麼讀 env 的既成
#: 事實；這一組是**人在畫面上打字**的詞彙。用讀取端的規則收人打的字，``!= "0"`` 那一型
#: 會把 ``false`` 讀成開啟（實測：存成 "1"、開關朝反方向動、沒有錯誤訊息）。
OFF_WORDS = ("false", "0", "no", "off", "n", "f", " FALSE ")


def test_the_bool_census_is_derived_not_typed():
    assert len(BOOL_KEYS) == 18, f"布林顆數變了：{BOOL_KEYS}"


@pytest.mark.parametrize("key", BOOL_KEYS)
@pytest.mark.parametrize("word", OFF_WORDS)
def test_writing_a_falsey_word_never_turns_a_switch_on(db, key, word):
    """**每一顆**布林設定、**每一種**「關」的寫法：存成關，或當場被拒。絕不是開。

    最終審查跨家實證：``intl.zh_normalize`` 打 ``false`` → 200、存成 ``"1"``、
    畫面顯示開啟。管理員的意圖被靜默反轉，而這一頁存在的理由就是不讓那件事發生。
    """
    try:
        set_setting(db, key, word)
    except (ValueError, TypeError):
        # 鎖定類別、或看不懂的寫法 —— 兩種都可以拒絕，只要**沒有寫進去**。
        # （這裡不能斷言生效值不是 True：鎖定顆的現值本來就可能是 True，
        #   例如 conftest 把 ANILA_ALLOW_DEV_SECRET 設成 1。）
        assert db.get(PlatformSetting, key) is None, f"{key} 被拒卻留下一列"
        return
    assert get_setting(db, key) is False, f"{key} 打 {word!r} 竟然變成開啟"


@pytest.mark.parametrize("key", [k for k in BOOL_KEYS if REGISTRY[k].setting_class.value in ("C", "B_EDIT")])
def test_writing_a_truthy_word_turns_it_on(db, key):
    for word in ("true", "1", "yes", "ON"):
        set_setting(db, key, word)
        assert get_setting(db, key) is True, f"{key} 打 {word!r} 沒有變成開啟"


def test_a_word_the_switch_cannot_map_is_refused_with_the_accepted_forms(
    client, admin_token, db
):
    """看不懂就拒收，並且把可用寫法講出來 —— 不猜、不預設成關。"""
    resp = client.put(
        _put_url("intl.zh_normalize"), json={"value": "maybe"}, headers=_auth(admin_token)
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "true" in detail and "false" in detail, "沒有告訴管理員可以填什麼"
    assert db.get(PlatformSetting, "intl.zh_normalize") is None


# ── 6. 稽核與設定寫在同一個交易裡（帳本舊債） ──────────────────────────────


def test_a_successful_write_records_who_changed_what(client, admin_token, db):
    client.put(
        _put_url("memory.retrieve_top_k"), json={"value": 47}, headers=_auth(admin_token)
    )

    events = _audit_events(db, "memory.retrieve_top_k")
    assert len(events) == 1
    event = events[0]
    assert event.actor_username == "admin"
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

    resp = client.put(
        _put_url("usage.batch_size"), json={"value": 73}, headers=_auth(admin_token)
    )
    assert resp.status_code == 500
    assert "沒有存起來" in resp.json()["detail"]

    db.rollback()
    assert db.get(PlatformSetting, "usage.batch_size") is None, (
        "稽核失敗了，設定卻留了下來 —— 兩件事不在同一個交易裡"
    )


def test_setting_flush_failure_is_explicitly_rolled_back(
    client, admin_token, db, monkeypatch
):
    """flush 的 DB 例外也要說明未落地，不留下半個交易。"""

    def _boom(*args, **kwargs):
        raise SQLAlchemyError("flush 炸掉（模擬）")

    monkeypatch.setattr(platform_settings_api, "set_setting", _boom)

    resp = client.put(
        _put_url("usage.batch_size"), json={"value": 73}, headers=_auth(admin_token)
    )
    assert resp.status_code == 500
    assert "沒有存起來" in resp.json()["detail"]
    assert "重試" in resp.json()["detail"]

    db.rollback()
    assert db.get(PlatformSetting, "usage.batch_size") is None


def test_a_swallowed_audit_failure_is_never_answered_with_success(
    client, admin_token, db, monkeypatch
):
    """稽核**軟失敗**時，端點不可以回 200 說「已儲存」。

    ``log_audit_event`` 是 fail-soft 的：commit 炸掉時它 rollback、吞例外、回 ``None``
    （``audit_service.py:105-124``）。而設定與稽核同交易，所以那一次 rollback 把管理員
    存的值一起帶走了。端點若不看回傳值，畫面會說「已儲存」而 DB 裡沒有那一列 ——
    **正是這一包存在要消滅的形狀，長在這一包自己身上**（最終審查跨家雙票同判 Important）。

    這裡走的是**真的** fail-soft 路徑：把 ``Session.commit`` 換成會炸的，
    ``log_audit_event`` 自己的 try 會接住 → rollback → 回 None。
    （建構子炸掉那條路是另一支測試，兩條都要有。）
    """
    from sqlalchemy.orm import Session as SASession

    def _boom_commit(self, *args, **kwargs):
        raise RuntimeError("commit 炸掉（模擬）")

    monkeypatch.setattr(SASession, "commit", _boom_commit)

    resp = client.put(
        _put_url("proxy.llm_timeout"), json={"value": 137}, headers=_auth(admin_token)
    )

    assert resp.status_code >= 400, f"稽核失敗了卻回 {resp.status_code}：{resp.text}"
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "沒有存起來" in detail, "錯誤訊息要說清楚「沒存成」，不是一句 500"
    assert "重試" in detail, "拒絕要給做法"

    monkeypatch.undo()
    db.rollback()
    assert db.get(PlatformSetting, "proxy.llm_timeout") is None, (
        "回了錯誤，那一列卻留了下來 —— 交易語意破了"
    )
    assert get_setting(db, "proxy.llm_timeout") == 120, "生效值必須還是改之前那個"


def test_a_persisted_write_does_not_depend_on_post_commit_refresh(
    client, admin_token, db, monkeypatch
):
    """**資料存好了就要說存好了** —— 判準是自己的 audit PK，不是 refresh 回傳值。

    舊的 helper 在 commit 後 refresh；refresh 失敗會把已提交的寫入誤報成失敗。
    端點現在由自己 flush audit、commit，再查自己的 audit PK，因此不需要 refresh。
    """
    from sqlalchemy.orm import Session as SASession

    calls = {"n": 0}

    def _flaky_refresh(self, *args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("不應依賴 refresh")

    monkeypatch.setattr(SASession, "refresh", _flaky_refresh)

    resp = client.put(
        _put_url("proxy.llm_timeout"), json={"value": 137}, headers=_auth(admin_token)
    )

    assert calls["n"] == 0, "設定寫入不應以 post-commit refresh 作為成功判準"
    assert resp.status_code == 200, f"資料存好了卻回 {resp.status_code}：{resp.text}"
    assert resp.json()["effective"] == 137
    assert "重試" not in resp.text, "對一個成功的寫入給了重試指引"

    db.rollback()
    assert db.get(PlatformSetting, "proxy.llm_timeout").value == "137"
    assert len(_audit_events(db, "proxy.llm_timeout")) == 1, (
        "假失敗會讓管理員重試 —— 那就會變成兩筆稽核"
    )


def test_the_audit_event_records_the_old_value_not_the_new_one(client, admin_token, db):
    """稽核的「舊值」必須真的是舊值。

    ``db.get`` 回的是 identity map 裡那顆 ORM 物件，而 ``set_setting`` 是**就地**改
    ``row.value``：端點若抓著物件不放，等到組 metadata 時「舊值」已經變成新值，
    稽核軌跡會永遠寫著 from == to —— 一條看起來有在記、其實什麼都沒記的軌跡。

    兩次寫入的值互不相同、也都不等於登錄表預設 3（避開帳本那條「值不可以等於場上
    任何預設」——否則 from/to 相等與「真的記了舊值」會一起變綠）。
    """
    first = client.put(
        _put_url("memory.retrieve_top_k"), json={"value": 47}, headers=_auth(admin_token)
    )
    assert first.status_code == 200, first.text
    second = client.put(
        _put_url("memory.retrieve_top_k"), json={"value": 61}, headers=_auth(admin_token)
    )
    assert second.status_code == 200, second.text

    events = _audit_events(db, "memory.retrieve_top_k")
    assert len(events) == 2
    latest = json.loads(events[-1].metadata_json)
    assert latest["from"] == 47, "舊值被就地改成新值了"
    assert latest["to"] == 61
    assert latest["stored_before"] == "47", "stored_before 抓的是 ORM 物件，不是當時的字串"
    assert latest["from"] != latest["to"]


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
    assert len(orphans) == 13, f"孤兒名單變了：{sorted(orphans)}"

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
    """「不能從畫面改」要接一句「那要去哪裡改」，否則管理員只剩重開機可以試。

    ⚠ 2026-08-09 起是**六顆**不是七顆：``queue.token_revocation_redis_timeout`` 的降級
    成因（import 期模組常數）已被最終審查的修訂拿掉，那一顆升回 C 類。名單由
    ``locked_reason`` 前綴推導，所以縮水這件事本身就被這一支記著。
    """
    demoted = _demoted_specs()
    assert len(demoted) == 6, f"降級名單變了：{[s.key for s in demoted]}"

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
