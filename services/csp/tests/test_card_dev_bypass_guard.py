# -*- coding: utf-8 -*-
"""``CARD_DEV_SKIP_NONCE_BINDING`` 不可以搭著真的開機混進來。（🔴 紅線鄰接）

這顆旗標開啟 = ``card_auth`` 的 **nonce 綁定（反 replay）整條關掉**
（``card_auth.py`` 的 ``if not _SKIP_NONCE_BINDING and econtent != expected_nonce``）。
簽章與憑證鏈驗證照跑,所以「攔到一次成功的刷卡簽章 → 無限重放」是它唯一的後果。
在 2026-08-08 的盤點之前,它**沒有任何程式層攔截**:``startup_security`` 的
``_KNOWN_DEFAULTS`` 不含它,任何人在 compose overlay 加一行就靜默生效,
唯一的防線是 ``docs/runbooks/intranet-deployment-runbook.md:26`` 那句話。
本檔把那句話變成開機硬檢查。

命門:**真值判定只能有一份**
==============================

消費端 ``card_auth`` 的判定是 ``raw.lower() in ("1", "true", "yes")`` —— 注意
**沒有 ``strip()``**（兄弟旗標 ``CARD_DEV_TRUST_TEST_CA`` 有,兩顆差一個字）。
守衛如果自己寫一份「差不多」的解析,兩邊就會在邊緣形狀上分岔,而分岔的**任一
方向都是缺陷**:

- 守衛比消費端**窄**（例如只認 ``== "true"``）→ ``CARD_DEV_SKIP_NONCE_BINDING=yes``
  守衛放行、消費端啟用,**旁路照開**。這是會出人命的那個方向。
- 守衛比消費端**寬**（例如自己補了 ``strip()``）→ ``" true "`` 守衛拒絕開機,
  而消費端其實是關的。假警報,但一樣是說謊的控制項。

所以守衛與消費端呼叫**同一支函式**;下面的形狀矩陣兩個方向都釘。

⚠ 順帶更正一則流傳的說法:「``" true "`` 會讓消費端啟用」是**反的**。沒有 strip
表示 ``" true "`` **不會**啟用消費端（``" true ".lower()`` 不在那三個字面裡）,
本檔的 ``_NOT_ENABLING`` 與 ``test_settings_registry.py:450`` 都釘著這件事。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from app.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CARD_AUTH_PY = _REPO_ROOT / "services" / "csp" / "app" / "services" / "card_auth.py"
_MAIN_PY = _REPO_ROOT / "services" / "csp" / "app" / "main.py"

# ── 形狀矩陣 ────────────────────────────────────────────────────────────────
#
# 期望值是**手寫的字面表**,不是「呼叫受測函式算出來的」—— 後者會讓整份測試
# 變成同義反覆:改了判定,期望值跟著改,測試永遠綠。

_ENABLING = ["1", "true", "yes", "TRUE", "YES", "True", "Yes", "tRuE"]

_NOT_ENABLING = [
    "",  # 未設 / 空字串
    "0", "false", "no", "off", "2", "truthy", "y", "on",
    # ⚠ 不 strip 的邊界:這四個看起來像開,消費端其實是關的。
    " true ", " 1", "yes ", "\ttrue",
]


def _load_card_auth_isolated():
    """載入一份**獨立**的 card_auth 副本,不動 ``sys.modules`` 裡活著的那個。

    要驗的是「模組層那一行在 import 當下算出什麼」,只能重新 import 才看得到。
    但 ``importlib.reload(app.services.card_auth)`` 會換掉活的模組物件,而
    ``card_auth_service`` 等等在 import 期就 ``from ... import`` 過它的名字,
    手上仍是舊那顆 —— 那正是 ``test_startup_security`` 的 teardown 註解
    講的那種「隨執行順序紅綠不定」。獨立副本沒有這個副作用。
    """
    spec = importlib.util.spec_from_file_location("_card_auth_probe", _CARD_AUTH_PY)
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` 在 class 建立當下會回查 ``sys.modules[cls.__module__]``,
    # 所以副本必須先掛上去才 exec,結束再拿掉(留著會變成第二份活模組)。
    sys.modules["_card_auth_probe"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("_card_auth_probe", None)
    return module


@pytest.fixture
def guard():
    from app.services.startup_security import assert_card_dev_bypass_not_in_a_real_boot

    return assert_card_dev_bypass_not_in_a_real_boot


@pytest.fixture
def not_dev_card_mode(monkeypatch):
    """內網正式姿態:沒開 dev 測試 CA,而且卡登是唯一入口。"""
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)


@pytest.fixture
def dev_card_mode(monkeypatch):
    """dev-card 模式:``card_auth._dev_test_ca_explicitly_allowed()`` 的兩個條件。"""
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", "1")
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", False)


# ── 1. 共用判定本身 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", _ENABLING)
def test_the_shared_predicate_says_yes_to_every_enabling_shape(raw):
    from app.services.card_auth import _skip_nonce_binding_value_enables

    assert _skip_nonce_binding_value_enables(raw) is True


@pytest.mark.parametrize("raw", _NOT_ENABLING)
def test_the_shared_predicate_says_no_to_everything_else(raw):
    from app.services.card_auth import _skip_nonce_binding_value_enables

    assert _skip_nonce_binding_value_enables(raw) is False


@pytest.mark.parametrize("raw", _ENABLING + _NOT_ENABLING)
def test_the_env_reader_is_that_same_predicate(monkeypatch, raw):
    from app.services.card_auth import (
        _skip_nonce_binding_value_enables,
        card_dev_skip_nonce_binding_enabled,
    )

    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", raw)
    assert card_dev_skip_nonce_binding_enabled() is _skip_nonce_binding_value_enables(raw)


def test_unset_is_not_enabling(monkeypatch):
    from app.services.card_auth import card_dev_skip_nonce_binding_enabled

    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)
    assert card_dev_skip_nonce_binding_enabled() is False


# ── 1b. 閉合性:不只上面那 22 個形狀 ────────────────────────────────────────
#
# 「列舉樣本」會被當成規格,然後只有列舉到的那幾個被守住（本樹既有教訓:
# 黑名單式防護永遠補不完,驗收要求閉合性）。下面兩條把值空間**生成**出來,
# 而不是手寫清單。


def _generated_value_space() -> list[str]:
    """所有單字元（``range(256)``）＋ 真值字面的大小寫／空白包裝變體。

    ``chr(0)`` 排除:環境變數在 POSIX 上以 NUL 結尾,``os.environ`` 根本收不下
    含 NUL 的值（``ValueError: embedded null byte``）。那不是被漏掉的形狀,
    是**不存在的形狀**。
    """
    space = [chr(code) for code in range(1, 256)]
    for literal in ("1", "true", "yes", "0", "no", "on"):
        space.append(literal)
        space.append(literal.upper())
        space.append(literal.capitalize())
        for pad in (" ", "  ", "\t", "\n", "\r", "\x0b", "　", " "):
            space += [pad + literal, literal + pad, pad + literal + pad]
    # 大小寫摺疊的邊角。``lower()`` 與 ``casefold()`` 對 ASCII 完全一致,只在
    # 這種字上分岔:``"yeſ".casefold() == "yes"``,而 ``.lower()`` 不變。
    # ⚠ 少了這幾個,「把 lower() 換成 casefold()」那個突變會**活下來** ——
    # 2026-08-09 實測過,整套 123 條全綠,判定悄悄放寬而沒有人知道。
    space += ["yeſ", "YEſ", "ſ", "1ſ", "truſ",
              "ẞ", "ß", "K", "İ", "ı", "ﬅ"]
    return space


def test_the_extraction_kept_the_pre_extraction_rule_exactly():
    """抽函式前 ``card_auth.py:121`` 那一行的規則,原文寫在這裡當對照組。

    這是紅線的「搬移前後逐位元相同」證據:對照組是**手抄的原始運算式**,
    不是呼叫受測函式,所以它抓得到任何把規則改掉的搬移。
    """
    from app.services.card_auth import _skip_nonce_binding_value_enables

    diverged = [
        raw
        for raw in _generated_value_space()
        # ↓ 2026-08-09 抽函式之前,:121 那一行原原本本的判定。
        if _skip_nonce_binding_value_enables(raw) != (raw.lower() in ("1", "true", "yes"))
    ]
    assert diverged == [], f"抽出來的判定與抽之前的規則不一致:{diverged!r}"


def test_the_guard_refuses_exactly_when_the_consumer_would_enable(
    monkeypatch, guard, not_dev_card_mode
):
    """⚠ 命門的閉合形式:整個生成值空間上,「守衛拒絕」⟺「消費端啟用」。

    這一條不看守衛怎麼寫的,只看它的行為 —— 守衛自己寫任何一份跟消費端
    不同的解析（窄的、寬的、正則的、白名單的），都會在某個生成值上分岔。
    """
    from app.services.card_auth import card_dev_skip_nonce_binding_enabled

    diverged = []
    for raw in _generated_value_space():
        monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", raw)
        consumer_enables = card_dev_skip_nonce_binding_enabled()
        try:
            guard()
            guard_refuses = False
        except RuntimeError:
            guard_refuses = True
        if guard_refuses != consumer_enables:
            diverged.append((raw, guard_refuses, consumer_enables))
    assert diverged == [], (
        f"守衛與消費端在這些值上分岔(值, 守衛拒絕?, 消費端啟用?):{diverged!r}"
    )


# ── 2. 紅線反向釘:抽函式不准改到消費端的既有行為 ──────────────────────────


@pytest.mark.parametrize("raw", _ENABLING + _NOT_ENABLING)
def test_the_module_level_constant_is_bit_identical_to_the_predicate(monkeypatch, raw):
    """``_SKIP_NONCE_BINDING``（:121 那一行的產物）逐形狀對齊。

    這是抽函式那次搬移的「前後一模一樣」證據。它也是唯一殺得死
    「有人在 :121 重新內聯一份不一樣的規則」那個突變的測試 ——
    ``tests/README.md:160`` 禁止測試走 skip=on 的驗章路徑,所以既有卡登測試
    完全沒有覆蓋到旗標開啟時的模組層狀態。
    """
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", raw)
    module = _load_card_auth_isolated()
    assert module._SKIP_NONCE_BINDING is (raw in _ENABLING)


def test_the_module_level_constant_is_false_when_unset(monkeypatch):
    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)
    module = _load_card_auth_isolated()
    assert module._SKIP_NONCE_BINDING is False


def test_the_nonce_comparison_still_reads_the_module_constant():
    """驗章那一行只准照原樣讀 ``_SKIP_NONCE_BINDING``。

    搬移如果順手把 :234 改成「每次呼叫重讀 env」,語意就從 boot-time 凍結
    變成 per-call —— 那是行為變更,不是抽函式。
    """
    source = _CARD_AUTH_PY.read_text(encoding="utf-8")
    assert "if not _SKIP_NONCE_BINDING and econtent != expected_nonce:" in source, (
        "nonce 綁定那一行被改寫了 —— 這一包只准抽函式,不准動驗章語意"
    )


# ── 3. 守衛:非 dev-card 模式 × 每一種會讓消費端啟用的值 → 拒絕開機 ─────────


@pytest.mark.parametrize("raw", _ENABLING)
def test_a_real_boot_refuses_every_enabling_shape(
    monkeypatch, guard, not_dev_card_mode, raw
):
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", raw)
    with pytest.raises(RuntimeError) as exc:
        guard()
    assert "CARD_DEV_SKIP_NONCE_BINDING" in str(exc.value)


def test_the_refusal_says_what_is_broken_and_how_to_proceed(
    monkeypatch, guard, not_dev_card_mode
):
    """錯誤訊息要人話:壞在哪、後果是什麼、要怎麼辦。"""
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    with pytest.raises(RuntimeError) as exc:
        guard()
    message = str(exc.value)
    assert "Refusing to start" in message
    assert "replay" in message.lower()
    # 自救出口必須寫出來 —— 沒有出口的拒絕會被下一個人整段拿掉。
    assert "CARD_DEV_TRUST_TEST_CA" in message
    assert "REQUIRE_CARD_LOGIN_ONLY" in message
    # 祕密零外洩:訊息不准把值本身印出來。
    assert "=1" not in message.replace("CARD_DEV_TRUST_TEST_CA=1", "")


def test_trusting_the_test_ca_is_not_enough_when_card_login_is_the_only_door(
    monkeypatch, guard
):
    """``CARD_DEV_TRUST_TEST_CA=1`` 但 ``REQUIRE_CARD_LOGIN_ONLY=True`` = 內網。

    dev-card 模式的定義是 ``_dev_test_ca_explicitly_allowed()`` 的**兩個**條件,
    不是其中一個。守衛若只看前者,內網那台照樣開得起來。
    """
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", "1")
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    with pytest.raises(RuntimeError):
        guard()


def test_allow_dev_secret_is_not_a_second_escape_hatch(
    monkeypatch, guard, not_dev_card_mode
):
    """``ANILA_ALLOW_DEV_SECRET=1`` 不准把這一條降級成 warning。

    ``startup_security`` 其他檢查守的是「祕密還是預設值」,dev 機器降級成
    warning 是合理的。這一條守的是「反 replay 綁定被關掉」,而
    ``ANILA_ALLOW_DEV_SECRET=1`` 誤帶到內網是**已知會發生**的事
    （``platform.yml:8`` 特地為它寫了一段）。給它第二把鑰匙,等於讓一個
    設錯的 dev 旗標把紅線一起帶開。
    """
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    with pytest.raises(RuntimeError):
        guard()


# ── 4. 守衛:不該擋的一律不擋 ───────────────────────────────────────────────


@pytest.mark.parametrize("raw", _ENABLING)
def test_dev_card_mode_may_still_use_the_flag(monkeypatch, guard, dev_card_mode, raw):
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", raw)
    guard()  # 不得丟出例外


@pytest.mark.parametrize("raw", _NOT_ENABLING)
def test_a_value_the_consumer_ignores_does_not_block_the_boot(
    monkeypatch, guard, not_dev_card_mode, raw
):
    """⚠ 這一組就是「守衛自己補一個 ``strip()``」的死刑執行者。

    ``" true "`` 不會啟用消費端,所以它**不可以**擋開機。守衛比消費端寬,
    症狀是內網某天因為一個尾隨空白開不起來,而那顆旗標其實是關的。
    """
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", raw)
    guard()


def test_the_flag_unset_is_the_normal_intranet_boot(monkeypatch, guard, not_dev_card_mode):
    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)
    guard()


# ── 5. 接線:沒有人呼叫的守衛等於沒有守衛 ───────────────────────────────────


def test_the_guard_is_actually_called_at_startup():
    """本樹最貴的教訓之一:寫好卻沒接線的控制項就是假控制項。"""
    source = _MAIN_PY.read_text(encoding="utf-8")
    assert re.search(
        r"^\s*assert_card_dev_bypass_not_in_a_real_boot\(\)\s*$", source, re.MULTILINE
    ), "main.py 的 lifespan 沒有呼叫 assert_card_dev_bypass_not_in_a_real_boot()"
    assert "assert_card_dev_bypass_not_in_a_real_boot," in source, (
        "main.py 沒有從 startup_security import 這支守衛"
    )


# ── 6. 第三份拷貝:設定頁登錄表的解析器必須跟消費端同意 ─────────────────────


@pytest.mark.parametrize("raw", _ENABLING + _NOT_ENABLING)
def test_the_settings_registry_parser_agrees_with_the_consumer(raw):
    """登錄表為了顯示這顆旗標,自己也寫了一份不 strip 的解析
    （``settings_registry._parse_card_truthy_nostrip``）。

    這一包不動它的程式碼,但把「兩份必須同意」釘起來 —— 哪天有人只修其中
    一份,畫面上的開關狀態就會跟真正生效的狀態說不一樣的話。
    """
    from app.services.card_auth import _skip_nonce_binding_value_enables
    from app.services.settings_registry import REGISTRY

    spec = REGISTRY["card.dev_skip_nonce_binding"]
    assert spec.value_type.parse(raw) is _skip_nonce_binding_value_enables(raw)
