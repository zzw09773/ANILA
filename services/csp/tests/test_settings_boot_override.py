# -*- coding: utf-8 -*-
"""B 類設定的開機覆蓋：管理員上次存的值，下一次開機真的生效——或者誠實說沒有。

這一支釘的是什麼
================
C 類（Task 3）改完下一個**請求**就生效；B 類做不到那件事，因為它們的讀取點
是背景迴圈的間隔、開機自動註冊的清單、附件落地的目錄——都是「開機時決定」
的東西。擁有者裁定的機制是 **DB 開機覆蓋**（明確否決了寫回 ``.env``）：csp
開機、DB 可達之後、開始服務之前，把 ``platform_settings`` 裡的 B 類覆蓋值蓋
回那個 ``app.config.settings`` 單例。

**所以本檔要消滅的形狀有四種，每一種都有專屬的釘：**

1. **平行狀態**。覆蓋套在另一份物件上（``model_copy``、模組層第二份 dict），
   而全樹幾百個 ``from app.config import settings`` 讀的還是原來那一顆。畫面
   說改好了、後端跑舊值，而且不會有任何錯誤訊息。
   釘法：**先擷取參考再套用**，然後透過那個**事前**的參考讀值——就是所有
   消費模組在 import 期做的那件事。換成 rebuild-and-swap 這裡立刻紅。
2. **套錯層**。把 env 層／預設層的值也蓋回去。值域上多半看不出來（值一樣），
   但 ``pydantic`` 的 ``env_file=".env"`` 讀得到的東西 ``os.environ`` 讀不到
   ——真正的佈署會被登錄表預設值洗掉，而 Task 5 的「來源」欄會把每一顆都寫成
   db-boot。釘法：``test_only_the_db_layer_is_applied``＋快照鍵集合等式。
3. **載入失敗靜默假裝成功**。設定表壞掉時吞掉例外、快照仍說一切正常。
   釘法：誠實三件組（以 env 值開機／恰好一則大聲的 ERROR／快照記載失敗），
   外加「開機絕不因設定表掛掉」。
4. **宣稱可改、其實改不到**（C1 遞延的那一批）。開機序早於覆蓋載入的顆，或
   根本不住在 ``Settings`` 上的顆，留在 B-可編輯就是畫面上的謊。
   釘法：兩支機械掃描——B-可編輯的每一顆都必須是 ``Settings`` 的欄位；且
   全樹 ``app/`` 沒有任何一顆 B-可編輯欄位在**模組層**（import 期）被讀走。

⚠ **場上的預設值有哪些**（測試值一律避開全部）：登錄表與 ``config.py`` 的
100／5／60／60／``data/attachments``／``""``，以及 conftest 塞進 env 的
``HEALTH_CHECK_INTERVAL=3600``／``ALERT_CHECK_INTERVAL=3600``。所以下面用
4321／7777／87 與兩個帶識別字的字串——沒有一個撞到任何一份預設值。值撞到預設
值時，「覆蓋真的套上去了」與「什麼都沒發生」會一起變綠（本專案的常設規則）。
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import pathlib
from typing import Any

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app import config as config_module
from app.config import (
    BOOT_OVERRIDE_LOG_TAG,
    BootOverrideSnapshot,
    Settings,
    apply_boot_overrides,
    boot_override_snapshot,
    settings,
)
from app.models.platform_setting import PlatformSetting, set_setting
from app.services.settings_registry import REGISTRY, SETTINGS, SettingClass

# ── 場上的固定樣本 ─────────────────────────────────────────────────────────

#: 代表性的四顆（brief 要求至少三顆）：整數鈕兩顆（一顆 env 有值、一顆沒有）、
#: 純字串一顆、JSON 字串一顆。型別與回退層都不同，才問得出「是不是每一種都真
#: 的走到欄位上」。
ROUND_TRIP_CASES = {
    # key: (Settings 欄位, 要存進去的值)
    "usage.batch_size": ("USAGE_BATCH_SIZE", 4321),
    "health.check_interval": ("HEALTH_CHECK_INTERVAL", 7777),
    "storage.attachment_path": ("ATTACHMENT_STORAGE_PATH", "data/attachments-boot-4321"),
    "seed.agents": (
        "AUTO_REGISTER_AGENTS",
        '[{"name":"boot-override-probe-4321","endpoint_url":""}]',
    ),
}

#: C1 裁決：原本七顆從 B-可編輯降到 B-鎖定（今天剩六顆，見下）。每一顆的證據寫在
#: ``test_each_demoted_entry_says_why_it_cannot_be_edited`` 的表裡。
# ⚠ 2026-08-09：原本七顆，現在**六顆**。TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS 那顆的
#: 成因（import 期算成模組常數、直讀 os.environ）已經被最終審查的修訂拿掉——消費端改由
#: 手上有 session 的呼叫端走 ``get_setting`` 解析，所以它升回可編輯（C 類），
#: 不再是降級名單的一員。名單縮水本身就是這張表要記的事。
DEMOTED_ENV_NAMES = frozenset({
    "DEBUG", "STATIC_DIR", "ANILA_HOST", "PYTHONUNBUFFERED",
    "LEGACY_SQLITE_PATH",
    "ANILA_TEMPLATE_DIR",
})

#: 模組層（import 期）讀 ``settings.X`` 的豁免名單。**只有這兩顆**，而且是因為
#: hook 之後有一段把值補寫回 FastAPI 物件（``_resync_app_identity``），並且那件
#: 事本身被 ``test_boot_override_reaches_the_openapi_title`` 用 ``/openapi.json``
#: 的 ``info.title`` 釘住。名單用**等式**釘：多一顆進來就得先解釋它怎麼生效。
MODULE_LEVEL_READ_EXEMPT = frozenset({"APP_NAME", "APP_VERSION"})


def _b_edit_specs():
    return [s for s in SETTINGS if s.setting_class is SettingClass.B_EDIT]


def _b_edit_fields() -> list[str]:
    return [s.env_name for s in _b_edit_specs() if s.env_name is not None]


# ── 模擬 boot 工具 ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _settings_identity_precondition():
    """本檔用 import 期綁定的 ``settings`` 名字；先確認它就是 hook 會寫的那一顆。

    ⚠ 本套件有四個檔會 ``importlib.reload(app.config)``（`test_startup_security` /
    `test_deactivate_revokes_session` / `test_revocations_endpoint` /
    `test_token_revoke_publish`）。它們的 teardown 會把 ``app.config.settings``
    指回原本那顆物件，所以這個前提在今天是成立的——但萬一哪天不成立，
    ``monkeypatch.setattr(settings, ...)`` 會去改一顆沒有人在讀的物件，於是整檔
    以一種看不出原因的方式紅掉。**寧可讓它在這裡指名道姓地失敗。**
    """
    assert settings is config_module.settings, (
        "app.config.settings 已經不是本檔 import 期綁到的那一顆物件了 —— "
        "有人 reload 了 app.config 而沒有把物件放回去（見 test_startup_security.py:25-37）"
    )


@pytest.fixture
def simulated_boot(monkeypatch):
    """跑一次「開機」：把 B 類欄位還原成 env／預設決定的開機值，再套一次覆蓋。

    ⚠ 刻意**不**讓 hook 寫到另一份物件上：它套的就是那個全域單例，因為那才是
    全樹讀的東西。還原交給 ``monkeypatch``（含模組層的快照），所以測試之間不會
    互相污染。
    """

    def _boot(db):
        fresh = Settings()
        for field in _b_edit_fields():
            monkeypatch.setattr(settings, field, getattr(fresh, field), raising=False)
        monkeypatch.setattr(
            config_module,
            "_current_snapshot",
            config_module._current_snapshot,
        )
        return apply_boot_overrides(db)

    return _boot


class _ExplodingSession:
    """設定表讀不動的那種 DB。``get`` 一碰就炸，其餘一律不該被呼叫。"""

    def __init__(self, exc: Exception | None = None):
        self.exc = exc or RuntimeError("platform_settings 讀不到（模擬）")
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


# ── 1. round-trip：存進去的值，下一次開機真的落在欄位上 ────────────────────


@pytest.mark.parametrize("key", sorted(ROUND_TRIP_CASES))
def test_saved_value_takes_effect_at_the_next_boot(db, key, simulated_boot):
    field, value = ROUND_TRIP_CASES[key]

    # 開機前：欄位上是 env／預設決定的值，而且不等於我們要存的那個。
    before = getattr(settings, field)
    assert before != value, f"{key} 的測試值撞到場上的預設值 —— 這一支會假綠"

    set_setting(db, key, value)
    row = db.get(PlatformSetting, key)
    assert row is not None, "值沒有進到 platform_settings —— round-trip 的第一段就斷了"

    snapshot = simulated_boot(db)

    assert getattr(settings, field) == value
    assert snapshot.applied[key] == value
    assert snapshot.load_failed is False


def test_the_override_is_visible_through_a_reference_captured_before_the_hook(
    db, simulated_boot
):
    """平行狀態殺手：透過**事前**擷取的參考讀值。

    全樹每一個消費模組都在 import 期做過 ``from app.config import settings``，
    也就是說它們手上是那一刻的物件參考。``model_copy`` 出一份新的、或把模組
    屬性換掉，對它們一點效果也沒有——而所有測試如果都走 ``config.settings``
    這條路，那種壞法會全綠。
    """
    from app import main
    from app.services import auto_seed, usage_writer

    # hook 會寫到的那一顆，**在 hook 之前**先抓在手上。
    captured = config_module.settings

    # 真正在讀設定的模組裡，哪些與它是同一個物件。
    # ⚠ 不逐一斷言「每一個都是」：本套件有四個檔會 ``importlib.reload(app.config)``，
    # 而那些 fixture 放得回 ``settings`` 物件、放不回「在 reload 視窗中第一次被
    # import 的模組手上那一顆」（``test_startup_security.py:25-37`` 已記載的既有
    # 陷阱）。那是測試環境的性質，不是本包的不變式。改成「至少要有一個共用」，
    # 這一支就既不會隨順序紅綠不定，也不會退化成空話。
    consumers = {
        "usage_writer": usage_writer.settings,
        "auto_seed": auto_seed.settings,
        "app.main": main.settings,
    }
    sharing = [name for name, obj in consumers.items() if obj is captured]
    assert sharing, "沒有任何消費模組與 app.config.settings 共用同一顆物件 —— 這一支測不到東西"

    set_setting(db, "usage.batch_size", 4321)
    simulated_boot(db)

    assert captured.USAGE_BATCH_SIZE == 4321
    for name in sharing:
        assert consumers[name].USAGE_BATCH_SIZE == 4321, f"{name} 看不到覆蓋"
    assert config_module.settings is captured, "模組屬性被換掉了 —— 舊參考會看不到覆蓋"


def _probe_value_for(spec, boot_value: Any) -> Any:
    """給這顆設定挑一個**合法**、且不等於場上任何一份預設值的探針值。

    值撞到預設值時「真的套上去了」與「什麼也沒發生」會一起變綠（本專案的常設
    規則），所以要避開三個來源：登錄表的 ``default``、這次開機算出來的值
    （env／``.env``／程式預設），以及目前欄位上的值。同時要通過這顆自己的
    ``domain_fn``，並且 ``format``／``parse`` 來回不變 —— 不然測的就是編碼而不是套用。
    """
    forbidden = {spec.default, boot_value, getattr(settings, spec.env_name)}
    structured_probes = {
        "_is_seed_models": '[{"name":"boot-probe-model-4321","endpoint_url":""}]',
        "_is_seed_agents": '[{"name":"boot-probe-agent-4321","endpoint_url":""}]',
        "_is_seed_links": '[{"name":"boot-probe-link-4321","url":"http://boot-probe.example"}]',
    }
    structured = structured_probes.get(spec.domain_fn.__name__)
    if structured not in forbidden and spec.domain_fn(structured):
        return structured
    candidates = {
        int: [4321, 7777, 137, 43, 7],
        float: [0.375, 4.25, 37.5, 1.5],
        str: [f"boot-probe-4321-{spec.key}", "boot-probe-4321"],
        bool: [not spec.default],
    }[spec.value_type.py_type]
    for candidate in candidates:
        if candidate in forbidden or not spec.domain_fn(candidate):
            continue
        if spec.value_type.parse(spec.value_type.format(candidate)) != candidate:
            continue
        return candidate
    raise AssertionError(
        f"{spec.key} 找不到一個合法又避開所有預設值的探針值 —— 請補一個候選進來，"
        "不要讓這顆從全體釘裡漏掉"
    )


def test_every_applied_key_really_landed_on_settings(db, simulated_boot):
    """快照的核心承諾：``applied`` ＝ **真的蓋到 ``settings`` 上**的值。

    ⚠ 這一支涵蓋**當下登錄表裡的每一顆 B-可編輯**（從 ``SETTINGS`` 推導，不是
    手寫名單——手寫名單的教訓已經在帳上）。上一輪只有四顆有 per-key 證據，於是
    「寫入迴圈靜默跳過一顆、而 ``pending``／快照原封不動」在 297 passed 底下走得
    過去：Task 5 的來源欄會指著一個從來沒有落地的值說 db-boot。

    後置條件是通用的（逐 key 比對欄位現值），所以將來新增的 B-可編輯顆會自動
    加入，不需要有人記得回來補。
    """
    fresh = Settings()
    probes = {
        spec.key: _probe_value_for(spec, getattr(fresh, spec.env_name))
        for spec in _b_edit_specs()
    }
    # ⚠ 12 → 11：``alerts.check_interval`` 的消費端在 2026-08-09 重接線成每輪 get_setting，
    # 那一顆升成 C 類（改完下一輪生效，不再需要開機覆蓋）。縮水要當場看得見，所以這個
    # 下限跟著調而不是放寬成不等式。
    assert len(probes) == len(_b_edit_specs()) >= 11, "B-可編輯的顆數變了，先確認是有意的"
    for key, value in probes.items():
        set_setting(db, key, value)

    snapshot = simulated_boot(db)

    assert set(snapshot.applied) == set(probes), "快照的鍵集合與存進去的不一致"
    for key, recorded in snapshot.applied.items():
        field = REGISTRY[key].env_name
        assert recorded == probes[key], f"{key} 的快照值不是我們存的那個"
        assert getattr(settings, field) == recorded, (
            f"{key}：快照宣稱套用了 {recorded!r}，但 settings.{field} 上是 "
            f"{getattr(settings, field)!r} —— 來源欄會指著一個從來沒有落地的值"
        )


def test_the_hook_leaves_untouched_settings_alone(db, simulated_boot):
    """只動存過的那幾顆。整批重灌會把沒人碰過的欄位洗成登錄表預設值。"""
    set_setting(db, "usage.batch_size", 4321)
    before_flush = getattr(settings, "ATTACHMENT_STORAGE_PATH")

    snapshot = simulated_boot(db)

    assert set(snapshot.applied) == {"usage.batch_size"}
    assert settings.ATTACHMENT_STORAGE_PATH == before_flush


# ── 2. 只套 DB 那一層 ──────────────────────────────────────────────────────


def test_only_the_db_layer_is_applied(monkeypatch, db):
    """env 層與預設層一律不套 —— 套了就是把佈署的值洗掉。

    ``Settings`` 的值可能來自 ``.env`` 檔（``model_config`` 有 ``env_file``），
    而 ``resolve_setting`` 的 env 層讀的是 ``os.environ``：兩邊本來就會不一樣。
    沒有 DB 那一列時把 env／預設蓋回去，等於用一個**這個行程從來沒有用過的
    值**取代佈署真正在跑的值，而且 Task 5 的來源欄會把它寫成 db-boot。
    """
    monkeypatch.setattr(settings, "USAGE_BATCH_SIZE", 87)  # 假裝 .env 說 87
    monkeypatch.setenv("USAGE_BATCH_SIZE", "999")  # os.environ 說別的
    monkeypatch.setattr(
        config_module, "_current_snapshot", config_module._current_snapshot
    )
    assert db.get(PlatformSetting, "usage.batch_size") is None

    snapshot = apply_boot_overrides(db)

    assert settings.USAGE_BATCH_SIZE == 87, "沒有 DB 那一列，欄位不可以被動到"
    assert "usage.batch_size" not in snapshot.applied
    assert snapshot.applied == {}


def test_a_broken_row_is_not_reported_as_applied(db, simulated_boot):
    """壞值退回下一層時，快照**不可以**說它套用了。

    ``resolve_setting`` 已經保證壞值退回 env／預設（Task 1 的不變式 4）。這裡
    釘的是快照那一端：來源欄如果照樣寫 db-boot，管理員會看著一個他以為存好了
    的值繼續用下去。
    """
    good = 4321
    set_setting(db, "usage.batch_size", good)
    # 繞過 API 直接塞一列解不開的（只有這種路徑寫得出來）。
    db.add(PlatformSetting(key="health.check_interval", value="不是數字"))
    db.flush()

    snapshot = simulated_boot(db)

    assert set(snapshot.applied) == {"usage.batch_size"}
    assert snapshot.applied["usage.batch_size"] == good
    assert "health.check_interval" not in snapshot.applied
    assert snapshot.load_failed is False, "單一列壞掉不是整體載入失敗"


def test_snapshot_keys_are_exactly_the_keys_with_a_usable_db_row(db, simulated_boot):
    """快照鍵集合 ＝ 有可用 DB 列的那些 key，一個不多一個不少。

    這是唯一殺得死「三層全套」的釘：值可能剛好相同，鍵集合不會。
    """
    for key in ("usage.batch_size", "seed.agents"):
        set_setting(db, key, ROUND_TRIP_CASES[key][1])

    snapshot = simulated_boot(db)

    stored = {row.key for row in db.query(PlatformSetting).all()}
    assert set(snapshot.applied) == stored == {"usage.batch_size", "seed.agents"}


# ── 3. 誠實三件組：載入失敗 ────────────────────────────────────────────────


def test_a_broken_settings_table_boots_on_env_values(monkeypatch, caplog):
    """DB 讀取炸掉時：以 env 值開機、恰好一則大聲的 ERROR、快照記載失敗。"""
    monkeypatch.setattr(settings, "USAGE_BATCH_SIZE", 87)
    monkeypatch.setattr(
        config_module, "_current_snapshot", config_module._current_snapshot
    )
    exploding = _ExplodingSession()

    with caplog.at_level(logging.INFO, logger=config_module.__name__):
        snapshot = apply_boot_overrides(exploding)

    # ① 以 env 值開機（欄位一顆都沒被動）
    assert settings.USAGE_BATCH_SIZE == 87
    assert snapshot.applied == {}
    # ② 大聲：恰好一則 ERROR，而且帶著可以 grep 的標記
    errors = [
        r for r in caplog.records
        if r.levelno >= logging.ERROR and r.name == config_module.__name__
    ]
    assert len(errors) == 1, f"應該恰好一則大聲警告，實際 {len(errors)} 則"
    assert BOOT_OVERRIDE_LOG_TAG in errors[0].getMessage()
    # ③ 快照如實記載，讓 Task 5 的來源欄不必用猜的
    assert snapshot.load_failed is True
    assert snapshot.failure_reason
    assert boot_override_snapshot().load_failed is True
    # 一次就放棄：不是每顆 key 都去撞一次壞掉的 DB
    assert exploding.calls == 1


def test_a_failure_midway_leaves_nothing_half_applied(monkeypatch, db, simulated_boot):
    """讀到一半才炸：已經讀出來的那幾顆也不可以套上去。

    「套了一半」比整批失敗難查得多——平台跑在一個沒有人選過的組合上，而快照
    說載入失敗。所以讀與寫分兩段：**讀完全部才開始寫**。
    """
    # 兩顆都存好，但**引爆點排在第一顆之後**：先讀出來的那顆如果是邊讀邊套，
    # 這裡就會抓到它已經落在欄位上。引爆點用 key 判斷（不是第幾次呼叫），所以
    # 登錄表重新排序也不會讓這一支失去意義。
    early_key, boom_key = "health.check_interval", "usage.batch_size"
    b_edit_keys = [s.key for s in _b_edit_specs()]
    assert b_edit_keys.index(early_key) < b_edit_keys.index(boom_key)
    for key in (early_key, boom_key):
        set_setting(db, key, ROUND_TRIP_CASES[key][1])
    simulated_boot(db)  # 先確認這兩顆本來是套得上的
    assert settings.HEALTH_CHECK_INTERVAL == 7777

    fresh = Settings()
    for field in _b_edit_fields():
        monkeypatch.setattr(settings, field, getattr(fresh, field), raising=False)
    monkeypatch.setattr(
        config_module, "_current_snapshot", config_module._current_snapshot
    )

    calls = {"n": 0}
    real_get = db.get

    def _get_then_explode(model, key, *args, **kwargs):
        calls["n"] += 1
        if key == boom_key:
            raise RuntimeError(f"讀到 {boom_key} 就讀不動了")
        return real_get(model, key, *args, **kwargs)

    monkeypatch.setattr(db, "get", _get_then_explode)
    snapshot = apply_boot_overrides(db)

    assert calls["n"] > 1, "探針沒有走到第二次讀取 —— 這一支沒測到東西"
    assert snapshot.load_failed is True
    assert snapshot.applied == {}
    for field in _b_edit_fields():
        assert getattr(settings, field) == getattr(fresh, field), (
            f"{field} 被套了一半 —— 平台跑在一個沒有人選過的組合上"
        )


def test_load_failure_never_blocks_the_boot(monkeypatch):
    """設定表掛掉不可以讓平台起不來 —— 這條比覆蓋本身重要。"""
    monkeypatch.setattr(
        config_module, "_current_snapshot", config_module._current_snapshot
    )
    for exc in (RuntimeError("boom"), OSError("connection refused"), ValueError("x")):
        snapshot = apply_boot_overrides(_ExplodingSession(exc))
        assert snapshot.load_failed is True


def test_the_failure_reason_never_carries_the_exception_text(monkeypatch):
    """失敗原因要上畫面（Task 5），所以只放例外**類別名**，不放訊息。

    SQLAlchemy 的連線錯誤訊息會帶著 DSN，而 ``DATABASE_URL`` 內嵌帳密。把
    ``str(exc)`` 放進一個管理員頁面上的欄位，就是把資料庫密碼印在設定頁上。
    """
    secret = "postgresql://csp:s3cr3t-not-real@db:5432/csp"
    monkeypatch.setattr(
        config_module, "_current_snapshot", config_module._current_snapshot
    )

    snapshot = apply_boot_overrides(_ExplodingSession(RuntimeError(secret)))

    assert "s3cr3t-not-real" not in snapshot.failure_reason
    assert secret not in snapshot.failure_reason
    assert "RuntimeError" in snapshot.failure_reason


def test_a_later_boot_replaces_the_snapshot_instead_of_accumulating(db, simulated_boot):
    """快照是「這一次開機」的紀錄。累加會讓來源欄記著上一輪的事。

    ⚠ 這在測試環境是真的會發生的：``TestClient`` 每進一次 context 就跑一次
    lifespan，同一個行程內 hook 會跑很多次。
    """
    set_setting(db, "usage.batch_size", 4321)
    first = simulated_boot(db)
    assert set(first.applied) == {"usage.batch_size"}

    db.delete(db.get(PlatformSetting, "usage.batch_size"))
    db.flush()
    second = simulated_boot(db)

    assert second.applied == {}
    assert second.load_failed is False
    assert boot_override_snapshot() is second


def test_a_successful_load_also_leaves_a_line(db, caplog, simulated_boot):
    """成功那一邊也要留一行 —— 「完全沒有這行」才讀得出「開機沒走到這裡」。

    這是 host allow-list 那一條教訓的同一形狀（``log_host_allowlist_state``）。
    """
    set_setting(db, "usage.batch_size", 4321)
    with caplog.at_level(logging.INFO, logger=config_module.__name__):
        simulated_boot(db)

    tagged = [r for r in caplog.records if BOOT_OVERRIDE_LOG_TAG in r.getMessage()]
    assert tagged, "成功的那一次開機一行也沒留"
    assert all(r.levelno < logging.ERROR for r in tagged)


# ── 4. 快照本身：Task 5 的介面 ─────────────────────────────────────────────


def test_the_snapshot_is_read_only(db, simulated_boot):
    """模組層唯讀狀態。Task 5 只讀它，不可以有人從外面改它。"""
    set_setting(db, "usage.batch_size", 4321)
    snapshot = simulated_boot(db)

    with pytest.raises((TypeError, AttributeError)):
        snapshot.applied["usage.batch_size"] = 1  # type: ignore[index]
    with pytest.raises(Exception):
        snapshot.load_failed = True  # type: ignore[misc]
    # ⚠ 型別要**從活的模組**取，不能用 import 期綁死的那個名字。本套件有四個檔
    # （test_startup_security、test_deactivate_revokes_session、
    # test_revocations_endpoint、test_token_revoke_publish）會
    # ``importlib.reload(app.config)``：那些 fixture 會把 ``settings`` **物件**放
    # 回去，卻放不回 reload 產生的**新類別物件**。用 import 期的名字比對，這一支
    # 就會隨執行順序紅綠不定 —— 正是 ``test_startup_security.py:25-37`` 已經寫進
    # 註解的那個陷阱（全套跑到這裡時真的紅過一次）。
    assert isinstance(snapshot, config_module.BootOverrideSnapshot)


def test_boot_override_snapshot_returns_the_live_record(db, simulated_boot):
    snapshot = simulated_boot(db)
    assert boot_override_snapshot() is snapshot


# ── 5. C1 收尾：宣稱可改的，一定要真的改得到 ──────────────────────────────


def test_every_b_edit_entry_is_a_field_on_settings():
    """B-可編輯的每一顆都必須住在 ``Settings`` 上，否則 hook 蓋不到它。

    ``ANILA_TEMPLATE_DIR``／``LEGACY_SQLITE_PATH`` 這一類是直接讀
    ``os.environ`` 的，它們在 ``Settings`` 上根本沒有欄位——套用機制碰不到，
    留在 B-可編輯就是畫面上的謊。
    """
    missing = [
        s.key for s in _b_edit_specs()
        if s.env_name is None or s.env_name not in Settings.model_fields
    ]
    assert missing == [], (
        f"這些 B-可編輯的顆不是 Settings 的欄位，開機覆蓋碰不到它們：{missing}"
    )


def _module_level_settings_reads() -> dict[str, list[tuple[str, int]]]:
    """全樹 ``app/`` 在 **import 期**讀掉的 ``settings.X``（函式體不算）。

    用 AST 而不是 grep：縮排看起來像模組層的東西可能在 ``if`` 底下，而
    ``def`` 底下的讀取是執行期的，兩者的判準不同。
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    hits: dict[str, list[tuple[str, int]]] = {}

    def rec(node, out, path):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue  # 函式體 = 執行期，hook 之後才跑
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "settings"
            ):
                out.setdefault(child.attr, []).append((path, child.lineno))
            rec(child, out, path)

    for py in sorted(root.rglob("*.py")):
        rec(ast.parse(py.read_text(encoding="utf-8")), hits, str(py))
    return hits


def test_no_b_edit_field_is_consumed_at_import_time():
    """開機序早於覆蓋載入的顆，不可以留在 B-可編輯（C1 的機械版）。

    hook 跑在 lifespan 裡，而模組層的讀取發生在 import 期——早得多。那種顆
    在畫面上按下去、重啟、值照舊，而且沒有任何錯誤訊息。掃描是**反向**的：
    不是核對一張手寫名單，而是問「今天的碼裡有沒有 B-可編輯欄位被 import 期
    讀走」，所以把任何一顆降級回來、或新加一個模組層讀取，這裡都會紅。
    """
    module_level = _module_level_settings_reads()
    offenders = {
        field: sites
        for field, sites in module_level.items()
        if field in set(_b_edit_fields()) and field not in MODULE_LEVEL_READ_EXEMPT
    }
    assert offenders == {}, (
        f"這些 B-可編輯欄位在 import 期就被讀走了，覆蓋來不及：{offenders}"
    )
    # 豁免名單用**等式**釘（三條合起來才真的是等式，缺一條就只是包含關係）：
    #   ① offenders == {}            → (模組層 ∩ B-可編輯) ⊆ 豁免名單
    #   ② EXEMPT ⊆ module_level      → 名單裡沒有已經不存在的殘骸
    #   ③ EXEMPT ⊆ B_EDIT            → 名單沒有因為某顆被降級而靜默變得過寬
    # 少了 ③，哪天 APP_NAME 降級成 B-鎖定，這張豁免名單會繼續放行一個不需要
    # 豁免的欄位，而沒有任何測試會說話。
    assert MODULE_LEVEL_READ_EXEMPT <= set(module_level), (
        "豁免名單裡有已經不存在的模組層讀取 —— 名單該縮了"
    )
    assert MODULE_LEVEL_READ_EXEMPT <= set(_b_edit_fields()), (
        "豁免名單裡有已經不是 B-可編輯的欄位 —— 它不需要豁免了，名單該縮了"
    )
    assert set(module_level) & set(_b_edit_fields()) == MODULE_LEVEL_READ_EXEMPT


DEMOTION_EVIDENCE = {
    # env 名: 為什麼覆蓋碰不到它（報告 §2 逐顆的證據）
    "DEBUG": "app/database.py:10 —— engine 在 import 期建好，echo 當場定案",
    "STATIC_DIR": "app/main.py:460 —— import 期 mount /static",
    "ANILA_HOST": "startup_security 讀 os.environ，且 lifespan 早於 hook",
    "PYTHONUNBUFFERED": "由 CPython 直譯器消費，全樹零讀取點",
    "LEGACY_SQLITE_PATH": "startup_migrations 讀 os.environ，Settings 上沒有這個欄位",
    "ANILA_TEMPLATE_DIR": "api/agents/registration.py:106 —— import 期算成模組常數",
}


def test_each_demoted_entry_says_why_it_cannot_be_edited():
    """降級的那些（今天六顆）：類別要是 B-鎖定，而且鎖定理由要講得出「為什麼」。

    「不能改」對管理員沒有用，「為什麼不能改」才有——那句話會原樣上畫面。
    """
    assert set(DEMOTION_EVIDENCE) == DEMOTED_ENV_NAMES
    by_env = {s.env_name: s for s in SETTINGS if s.env_name is not None}
    for env_name in sorted(DEMOTED_ENV_NAMES):
        spec = by_env[env_name]
        assert spec.setting_class is SettingClass.B_LOCKED, (
            f"{env_name} 沒有降級 —— 它宣稱重啟後會生效，但它不會"
        )
        assert spec.locked_reason.strip()
        assert "開機序早於覆蓋載入" in spec.locked_reason or "os.environ" in spec.locked_reason


# ── 6. hook 真的接在開機路徑上，而且接在對的位置 ──────────────────────────


def test_the_hook_runs_after_the_schema_and_before_every_consumer():
    """lifespan 裡的順序釘。位置錯了，覆蓋就套在讀完之後——全套照樣綠。

    * ``_run_alembic_upgrade`` 之後：``platform_settings`` 那張表要先在。
    * ``auto_seed`` 之前：``ADMIN_USERNAME``／``AUTO_REGISTER_*`` 在那裡被讀。
    * 背景迴圈之前：健康檢查／用量寫入／告警的間隔在那裡被讀。
    """
    from app import main

    source = inspect.getsource(main.lifespan)
    # ⚠ 錨點必須是**呼叫**，不是 import 敘述。lifespan 裡的 import 是就地寫的，
    # ``source.find("apply_boot_overrides")`` 命中的是那一行；把整個呼叫區塊搬到
    # ``auto_seed()`` 之後、import 留在原位，這個釘就完全看不見（驗收自創突變
    # P-A 正是這樣活下來的）。真正的行為釘在
    # ``test_every_lifespan_consumer_observes_the_override_at_the_moment_it_runs``，
    # 這一支是**補充**的原始碼位置釘。
    positions = {
        name: source.find(name)
        for name in (
            "_run_alembic_upgrade",
            "= apply_boot_overrides(",
            "run_startup_migrations",
            "auto_seed(",
            "start_health_checker(",
            "start_usage_writer(",
            "start_alert_detectors(",
        )
    }
    missing = [n for n, i in positions.items() if i < 0]
    assert missing == [], f"lifespan 裡找不到這些呼叫：{missing}"
    assert positions["= apply_boot_overrides("] > source.find("apply_boot_overrides"), (
        "錨點又指到 import 敘述了 —— 這個釘會漏掉「呼叫搬走、import 留下」那個突變"
    )
    assert positions["_run_alembic_upgrade"] < positions["= apply_boot_overrides("]
    for later in (
        "run_startup_migrations",
        "auto_seed(",
        "start_health_checker(",
        "start_usage_writer(",
        "start_alert_detectors(",
    ):
        assert positions["= apply_boot_overrides("] < positions[later], (
            f"覆蓋套在 {later} 之後 —— 那顆設定的消費端讀到的是舊值"
        )


def test_the_boot_path_survives_a_hook_that_explodes(monkeypatch, real_boot_row):
    """hook 自己炸了也不可以擋住開機 —— 而且快照要說「我們沒有去看」。

    ``apply_boot_overrides`` 擋得住它自己看得到的例外；連 session 都開不起來的
    時候它根本沒被呼叫過，快照會停在初始值（``load_failed=False``、
    ``applied={}``）—— 那正好長得跟「沒有人設定過」一模一樣。開機端的保險絲要
    把這一次記成失敗，否則設定頁會把一次沒看到說成一次乾淨的開機。

    ⚠ **先跑一次成功的開機**再引爆。少了這一步，下面的 ``applied == {}`` 是恆真
    的（模組層快照本來就是空的），於是「失敗時把**上一輪**的 ``applied`` 留著」
    ——快照同時說「載入失敗」又列著幾顆套用過的 key，而 Task 5 的畫面會自相
    矛盾——那個形狀就有出口（驗收自創突變 P-C 正是這樣活下來的）。同一個行程
    內開機兩次不是假想：``TestClient`` 每進一次 context 就是一次 lifespan。
    """
    from fastapi.testclient import TestClient

    from app import main

    # ① 成功的那一次：把模組層快照填成非空。
    real_boot_row("usage.batch_size", 4321)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200
    assert boot_override_snapshot().applied == {"usage.batch_size": 4321}, (
        "第一次開機沒有套到東西 —— 這一支的前提沒有建立起來"
    )

    # ② 第二次開機，hook 自爆。
    def _boom(_db):
        raise RuntimeError("hook 自己炸了")

    monkeypatch.setattr(config_module, "apply_boot_overrides", _boom)
    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200

    snapshot = boot_override_snapshot()
    assert snapshot.load_failed is True, "開機沒讀到設定表，快照卻說一切正常"
    assert snapshot.applied == {}, (
        "失敗的開機留著上一輪的 applied —— 快照同時說「載入失敗」又列著套用過的 key"
    )
    assert "RuntimeError" in snapshot.failure_reason


# ── 7. 真的走一次開機：``TestClient`` 跑的是真正的 lifespan ────────────────


@pytest.fixture
def real_boot_row():
    """把一列寫進 ``SessionLocal`` 那個 DB（hook 在 lifespan 裡開的就是它）。

    收尾一定要把列刪掉並還原欄位：這個 DB 是整個 session 共用的，留一列下來
    等於讓後面每一支測試都在一個被改過名字的平台上跑。
    """
    from app import main
    from app.database import SessionLocal

    written: list[str] = []
    #: 每寫一個 key 就先把它對應的 ``Settings`` 欄位存起來 —— 真的 lifespan 會把
    #: 覆蓋蓋到全域單例上，不還原就等於讓後面每一支測試在一個被改過的平台上跑。
    saved_fields: dict[str, Any] = {}
    saved_app = {"title": main.app.title, "version": main.app.version}

    def _write(key: str, value):
        field = REGISTRY[key].env_name
        if field is not None and field not in saved_fields:
            saved_fields[field] = getattr(settings, field)
        session = SessionLocal()
        try:
            set_setting(session, key, value)
            session.commit()
        finally:
            session.close()
        written.append(key)

    try:
        yield _write
    finally:
        session = SessionLocal()
        try:
            for key in written:
                row = session.get(PlatformSetting, key)
                if row is not None:
                    session.delete(row)
            session.commit()
        finally:
            session.close()
        for field, value in saved_fields.items():
            setattr(settings, field, value)
        main.app.title = saved_app["title"]
        main.app.version = saved_app["version"]
        main.app.openapi_schema = None
        config_module._current_snapshot = BootOverrideSnapshot(
            applied=config_module._EMPTY_OVERRIDES, load_failed=False, failure_reason=""
        )


def test_every_lifespan_consumer_observes_the_override_at_the_moment_it_runs(
    real_boot_row, monkeypatch
):
    """**行為**版的順序釘：每一個消費端執行的當下，看到的必須是覆蓋後的值。

    原始碼位置比對可以被繞過（把呼叫區塊搬走、import 留在原位）；這一支問的是
    執行期的事實，所以與原始碼怎麼排版無關。做法是把 lifespan 會呼叫的那幾個
    消費端換成探針，記下**它被呼叫的那一刻** ``settings`` 上的值。

    覆蓋要是套得太晚，``admin.username``／``seed.*`` 會被 ``auto_seed`` 以覆蓋前
    的值消費掉，三個背景迴圈也會用舊的間隔跑一輪——管理員在畫面上改了、重啟
    了、值照舊，而且沒有任何錯誤訊息。**那正是本包宣稱要消滅的形狀。**

    ⚠ lifespan 裡的 import 都是就地寫的（``from ... import x`` 在函式體內），
    所以換掉模組屬性就換得掉它真正呼叫到的那一個。
    """
    from fastapi.testclient import TestClient

    from app import main
    from app.services import alert_detectors, auto_seed as auto_seed_module
    from app.services import health_checker, startup_migrations, usage_writer

    overrides = {
        "admin.username": ("ADMIN_USERNAME", "驗收用管理員-4321"),
        "seed.models": (
            "AUTO_REGISTER_MODELS",
            '[{"name":"boot-order-4321","endpoint_url":""}]',
        ),
        # ⚠ 原本這裡有兩顆：``alerts.check_interval`` 那一行在重接線之後被機械式改名成
        # ``health.check_interval``，於是與上一行**重複**（ruff F601），watched 欄位默默
        # 從 5 掉到 4。機械式取代正是「調整藏在裡面」的地方，所以直接刪掉那一行、
        # 補一顆真的不同的設定回來，維持四顆各自不同的 Settings 欄位。
        "health.check_interval": ("HEALTH_CHECK_INTERVAL", 4321),
        "usage.flush_interval": ("USAGE_FLUSH_INTERVAL", 137),
        "storage.attachment_path": ("ATTACHMENT_STORAGE_PATH", "data/attachments-order-4321"),
    }
    watched = {field for field, _v in overrides.values()}
    for key, (field, value) in overrides.items():
        assert getattr(settings, field) != value, f"{key} 的測試值撞到場上的值"
        real_boot_row(key, value)

    seen: dict[str, dict[str, Any]] = {}

    def _observe(label: str) -> None:
        seen[label] = {field: getattr(settings, field) for field in watched}

    def _spy_startup_migrations() -> None:
        _observe("run_startup_migrations")

    def _spy_auto_seed() -> None:
        _observe("auto_seed")

    def _spy_starter(label: str):
        async def _start():
            _observe(label)
            return None  # lifespan 只在非 None 時 cancel，回 None 是安全的

        return _start

    monkeypatch.setattr(
        startup_migrations, "run_startup_migrations", _spy_startup_migrations
    )
    monkeypatch.setattr(auto_seed_module, "auto_seed", _spy_auto_seed)
    monkeypatch.setattr(
        health_checker, "start_health_checker", _spy_starter("start_health_checker")
    )
    monkeypatch.setattr(
        usage_writer, "start_usage_writer", _spy_starter("start_usage_writer")
    )
    monkeypatch.setattr(
        alert_detectors, "start_alert_detectors", _spy_starter("start_alert_detectors")
    )

    with TestClient(main.app) as c:
        assert c.get("/health").status_code == 200

    expected = {field: value for field, value in overrides.values()}
    for label in (
        "run_startup_migrations",
        "auto_seed",
        "start_health_checker",
        "start_usage_writer",
        "start_alert_detectors",
    ):
        assert label in seen, f"{label} 根本沒被呼叫到 —— 這一支沒有測到東西"
        for field, value in expected.items():
            assert seen[label][field] == value, (
                f"{label} 執行的當下 settings.{field} 是 {seen[label][field]!r}，"
                f"不是覆蓋值 {value!r} —— 覆蓋套得太晚，這顆設定要等下下次開機才生效"
            )


def test_boot_override_reaches_the_openapi_title(real_boot_row):
    """端到端：存一個平台名 → 真的跑一次 lifespan → 三個顯示點全部換了。

    ``FastAPI(title=...)``（main.py:211）在 import 期就把值抄走了，所以 hook
    之後必須把它補寫回去。少了那一步，``/health`` 會說新名字而
    ``/openapi.json`` 的 ``info.title`` 還是舊的——同一顆設定兩個答案，而這
    正是本包宣稱要消滅的形狀。
    """
    from fastapi.testclient import TestClient

    from app import main

    new_name = "驗收用平台名-4321"
    assert main.app.title != new_name
    real_boot_row("app.name", new_name)

    # ⚠ 先把 openapi 文件生出來，讓 FastAPI 的快取**帶著舊名字**進到這次開機。
    # 不先踩這一步，``openapi_schema`` 本來就是 None，於是「有沒有作廢快取」在
    # 測試裡看起來一模一樣 —— 那個保險就變成沒有人證明過的死碼（第一輪突變
    # battery 的 M11 就是這樣活下來的）。
    stale = main.app.openapi()
    assert stale["info"]["title"] != new_name
    assert main.app.openapi_schema is not None

    with TestClient(main.app) as c:
        body = c.get("/health").json()
        assert body["service"] == new_name  # 執行期讀取點
        assert main.app.title == new_name  # import 期抄走的那一份
        assert main.app.openapi()["info"]["title"] == new_name  # 快取重生成
        assert boot_override_snapshot().applied["app.name"] == new_name
