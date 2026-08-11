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

主判準:**凍結的那顆常數**(2026-08-09 修訂)
=============================================

上面那條「共用同一支函式」是必要條件,**但 round 0 只釘到這裡是不夠的**。共用的那支
是**動態** helper（每次重讀環境),而驗章那一行讀的是 ``card_auth`` 在 import 當下凍結
的 ``_SKIP_NONCE_BINDING``;靜態環境下兩者永遠一致,所以閉合性再完整也錨在錯的東西上。
跨家審查(sol,08-09 紅線第二把鑰匙)實測出那個視窗:**以 ``=1`` import ``card_auth``、
開機前把變數從環境移除** → 旁路已經凍結成開著、反 replay 已經關掉,而守衛重讀環境看不到
任何東西於是**放行開機**。

所以出貨的守衛判 **frozen OR live**,frozen 是主判準:
``card_dev_skip_nonce_binding_frozen()``（重讀模組全域 —— 驗章讀的就是那一顆)為真 → 拒;
``card_dev_skip_nonce_binding_enabled()``（重讀環境)為真 → 也拒,涵蓋「環境已設、
``card_auth`` 還沒被 import」的設定意圖。兩個維度四種組合裡
(F,L)=(1,1)/(1,0)/(0,1) 拒、(0,0) 放,本檔逐一釘,兩把鑰匙各自獨立窮舉過。

⚠ 順帶更正一則流傳的說法:「``" true "`` 會讓消費端啟用」是**反的**。沒有 strip
表示 ``" true "`` **不會**啟用消費端（``" true ".lower()`` 不在那三個字面裡）,
本檔的 ``_NOT_ENABLING`` 與 ``test_settings_registry.py:450`` 都釘著這件事。
"""
from __future__ import annotations

import importlib.util
import re
import sys
import asyncio
import itertools
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from app.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CARD_AUTH_PY = _REPO_ROOT / "services" / "csp" / "app" / "services" / "card_auth.py"
_MAIN_PY = _REPO_ROOT / "services" / "csp" / "app" / "main.py"
_CSP_ROOT = _REPO_ROOT / "services" / "csp"

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
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")


@pytest.fixture
def dev_card_mode(monkeypatch):
    """dev-card 模式:``card_auth._dev_test_ca_explicitly_allowed()`` 的兩個條件。"""
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", "1")
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "password")


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
    for literal in ("1", "true", "yes", "0", "no", "on", "off"):
        # ⚠ **每一種**大小寫排列,不是 upper()／capitalize() 兩三個樣本。
        # 2026-08-09 紅線雙票指出:只取樣本的話,一份「恰好匹配這些樣本」的
        # 自寫 parser 可以讓整套全綠,而 ``TrUe``／``trUE``／``yEs`` 照樣放行。
        # 消費端的啟用集合＝所有 ``lower()`` 落在三個字面上的字串,對 ASCII
        # 而言就是下面這個 product ——所以這裡是**窮舉**,不是抽樣。
        for combo in itertools.product(*[(ch.lower(), ch.upper()) for ch in literal]):
            space.append("".join(combo))
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
    from app.services.card_auth import (
        card_dev_skip_nonce_binding_enabled,
        card_dev_skip_nonce_binding_frozen,
    )

    # 前提:這個測試行程的凍結狀態是「關」。守衛是 frozen OR live,凍結若為真
    # 就會**每個值都拒絕**,本條的意義會安靜地垮成同義反覆。
    assert card_dev_skip_nonce_binding_frozen() is False, (
        "測試行程本身是帶著旗標 import 的 —— 本條測的東西已經不是它宣稱的東西"
    )

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
    assert "ANILA_AUTH_MODE" in message
    # 祕密零外洩:訊息不准把值本身印出來。
    assert "=1" not in message.replace("CARD_DEV_TRUST_TEST_CA=1", "")


def test_trusting_the_test_ca_is_not_enough_when_card_login_is_the_only_door(
    monkeypatch, guard
):
    """``CARD_DEV_TRUST_TEST_CA=1`` 但 ``ANILA_AUTH_MODE=card-only`` = 內網。

    dev-card 模式的定義是 ``_dev_test_ca_explicitly_allowed()`` 的**兩個**條件,
    不是其中一個。守衛若只看前者,內網那台照樣開得起來。
    """
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", "1")
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")
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


# ══ Fix round 1 ═══════════════════════════════════════════════════════════════
#
# 紅線雙票（2026-08-09）回來了。sol 那一票的 Critical 是真的，我先實測重現才動手：
#
#     以 CARD_DEV_SKIP_NONCE_BINDING=1 import card_auth  → _SKIP_NONCE_BINDING = True
#     lifespan 之前把該變數從環境移除                      → 守衛重讀環境 → 看不到 → 放行
#     結果：反 replay 旁路**生效中**，而開機沒有被拒絕。
#
# 我第一輪的閉合證明比對的是**動態 helper**（`card_dev_skip_nonce_binding_enabled()`），
# 不是驗章那一行真正消費的**凍結常數** `_SKIP_NONCE_BINDING`（card_auth.py:148,257）。
# 兩者在「import 之後環境才變動」的視窗裡會分岔，而那正是唯一重要的視窗。
# 修法：守衛改看凍結狀態（frozen），並保留環境即時值（live）做為第二個觸發條件。


# ── 7. Critical 回歸：凍結的旁路不准搭著開機混進來 ──────────────────────────


def test_a_bypass_frozen_at_import_is_refused_even_if_the_env_is_gone(
    monkeypatch, guard, not_dev_card_mode
):
    """sol 的 Critical，最小 in-process 形式。

    ``_SKIP_NONCE_BINDING`` 是 import 當下凍結的;環境變數之後被移除或改值，
    **凍結的那個 True 不會跟著變**，而它才是 ``card_auth:257`` 真正讀的東西。
    守衛只看環境 = 看一個已經過期的問題。
    """
    import app.services.card_auth as card_auth

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)

    with pytest.raises(RuntimeError) as exc:
        guard()
    assert "CARD_DEV_SKIP_NONCE_BINDING" in str(exc.value)


@pytest.mark.parametrize("later_env", ["", "0", "false", " true ", "no"])
def test_a_frozen_bypass_is_refused_whatever_the_env_was_changed_to(
    monkeypatch, guard, not_dev_card_mode, later_env
):
    """環境「被改成別的值」與「被移除」是同一個視窗的兩種形狀。"""
    import app.services.card_auth as card_auth

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", later_env)

    with pytest.raises(RuntimeError):
        guard()


def test_the_guard_reads_the_same_constant_the_nonce_comparison_reads(monkeypatch, guard):
    """凍結狀態的來源必須是 ``card_auth._SKIP_NONCE_BINDING`` 本人。

    守衛若自己另外記一份（例如 import 時抄一份到 startup_security），
    monkeypatch 這顆常數就影響不到它 —— 那就是第二份會漂開的真相。
    """
    import app.services.card_auth as card_auth

    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", False)
    guard()  # 凍結是關的、環境也沒設 → 放行

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    with pytest.raises(RuntimeError):
        guard()


def test_a_real_process_that_imported_with_the_flag_then_lost_it_refuses_to_boot():
    """sol 的 Critical，**真的另起一個行程**跑一次（不靠 monkeypatch 模擬）。

    monkeypatch 版證明「守衛讀的是那顆常數」；這一版證明「真實的 import 時序
    確實會產生那個狀態」。少了這一版，凍結語意就只是測試裡的假設。
    """
    script = textwrap.dedent(
        """
        import os
        import app.services.card_auth as ca
        assert ca._SKIP_NONCE_BINDING is True, "SETUP_FAILED: flag did not freeze on"
        # lifespan 之前，環境變數不見了 —— 凍結的旁路仍然生效。
        os.environ.pop("CARD_DEV_SKIP_NONCE_BINDING", None)
        os.environ.pop("CARD_DEV_TRUST_TEST_CA", None)
        from app.config import settings
        settings.ANILA_AUTH_MODE = "card-only"          # 非 dev-card 模式
        from app.services.startup_security import (
            assert_card_dev_bypass_not_in_a_real_boot as guard,
        )
        try:
            guard()
            print("BOOT_PROCEEDED")
        except RuntimeError:
            print("BOOT_REFUSED")
        print("EFFECTIVE_SKIP=%r" % (ca._SKIP_NONCE_BINDING,))
        """
    )
    env = dict(os.environ)
    env["CARD_DEV_SKIP_NONCE_BINDING"] = "1"
    env["ANILA_AUTH_MODE"] = "password"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_CSP_ROOT), str(_REPO_ROOT / "packages" / "anila-core" / "src")]
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_CSP_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert "SETUP_FAILED" not in proc.stdout + proc.stderr, proc.stderr[-2000:]
    assert "EFFECTIVE_SKIP=True" in proc.stdout, (
        f"前提沒成立:旁路並未凍結成開啟。stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    )
    assert "BOOT_REFUSED" in proc.stdout, (
        "凍結的反 replay 旁路生效中,開機卻沒有被拒絕 —— "
        f"stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    )


# ── 8. 接線要釘「執行」,不是釘原始碼字串（殺 A-P1／A-P1b）─────────────────
#
# KEY A 那一票投了兩顆突變,兩顆都活著:
#   A-P1  把 main.py 的守衛呼叫搬到 ``yield`` 之後 → 整段服役期都沒有守衛
#   A-P1b 把它包進 ``try/except Exception: logger.warning(...)`` → fail-closed 變 no-op
# 兩顆的共同點:那一行**還在原始碼裡**,regex 掃得到。所以掃字串的測試守不住它。


def _drive_lifespan_startup(monkeypatch):
    """真的把 ``main.lifespan`` 的啟動段跑一次。回 (raised, served)。

    姊妹守衛先中性化,好讓「開機被拒」這件事**可歸因**到本包這一支;
    ``lifespan`` 內是函式內 import,所以 monkeypatch 模組屬性攔得到。
    """
    import app.main as main_module
    import app.services.startup_security as ss

    monkeypatch.setattr(ss, "assert_no_dev_defaults", lambda: None)
    monkeypatch.setattr(ss, "assert_intranet_lockdown_consistency", lambda: None)

    served: list[str] = []

    async def _boot():
        async with main_module.lifespan(main_module.app):
            served.append("serving")

    raised: list[BaseException] = []
    try:
        asyncio.run(_boot())
    except BaseException as exc:  # noqa: BLE001 - 要看的就是「有沒有東西逃出來」
        raised.append(exc)
    return raised, served


def test_the_guard_actually_refuses_the_boot_and_nothing_gets_served(monkeypatch):
    """非 dev-card × 旁路凍結開啟 → ``lifespan`` 的啟動段必須炸,而且**不准開始服務**。

    這一條同時殺死兩顆突變:
    - 呼叫被搬到 ``yield`` 之後 → ``served`` 會是 ``["serving"]`` → 紅。
    - 呼叫被 try/except 吞掉   → 什麼都沒丟出來 → 紅。
    """
    import app.services.card_auth as card_auth

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")

    raised, served = _drive_lifespan_startup(monkeypatch)

    assert raised, "開機沒有被拒絕 —— 守衛要嘛沒跑,要嘛例外被吞掉了"
    assert isinstance(raised[0], RuntimeError), f"丟出來的不是 RuntimeError:{raised[0]!r}"
    assert "CARD_DEV_SKIP_NONCE_BINDING" in str(raised[0]), (
        f"拒絕是別的原因,不是本包這一支:{raised[0]!r}"
    )
    # ⚠ 這一行才是殺死「搬到 yield 之後」的那一刀:那顆突變照樣會在關閉階段
    # 丟出同一個 RuntimeError,上面三條都會綠 —— 只有「服務從來沒開始過」擋得住。
    assert served == [], "守衛拒絕之前應用程式就已經開始服務了"


def test_a_healthy_config_still_boots_past_the_guard(monkeypatch):
    """反向釘:守衛不可以變成「永遠擋住開機」。

    少了這一條,把守衛改成無條件 raise 也會讓上面那條全綠。
    """
    import app.services.card_auth as card_auth

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", False)
    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")

    import app.services.startup_security as ss

    ran: list[str] = []
    real = ss.assert_card_dev_bypass_not_in_a_real_boot

    def _watched():
        ran.append("ran")
        return real()

    monkeypatch.setattr(ss, "assert_card_dev_bypass_not_in_a_real_boot", _watched)

    _drive_lifespan_startup(monkeypatch)
    assert ran == ["ran"], (
        "健康設定下 lifespan 的啟動段沒有呼叫到守衛 —— 接線在某個分支裡消失了"
    )


# ── 9. 命門的另一半:dev-card 模式判定也不准自寫（殺 A-P2／A-P2b）───────────
#
# 第一輪只把「值判定」釘住了。守衛承諾的是**兩件事**都共用真定義,
# 而 CARD_DEV_TRUST_TEST_CA 的值軸一次都沒被測過 —— KEY A 用一份「比真定義寬」
# 的自寫模式判定，讓守衛在 CARD_DEV_TRUST_TEST_CA=false 時放行了旁路，全套仍綠。

# ⚠ 這顆兄弟旗標的判定**有** strip（``card_auth:109``），與 SKIP 那顆差一個字。
# 手寫期望表,不是呼叫受測函式算出來的。
_TRUST_CA_EXPECTATIONS = [
    ("1", True), ("true", True), ("yes", True), ("TRUE", True), ("Yes", True),
    (" 1 ", True),      # ← 有 strip:這一格殺死「漏掉 .strip()」的自寫複本
    ("\t1\n", True),
    ("", False), ("0", False), ("false", False), ("no", False),
    ("2", False), ("on", False), ("y", False),   # ← 「非空非 0 即算開」的自寫複本死在這幾格
]


@pytest.mark.parametrize("trust_ca,is_trusted", _TRUST_CA_EXPECTATIONS)
@pytest.mark.parametrize("card_only", [True, False])
def test_the_guard_passes_exactly_when_the_real_dev_card_definition_says_so(
    monkeypatch, guard, trust_ca, is_trusted, card_only
):
    """二維閉合:CARD_DEV_TRUST_TEST_CA 的值 × ANILA_AUTH_MODE 的模式。

    期望值來自**手寫表**（``_TRUST_CA_EXPECTATIONS``）與 dev-card 模式的定義
    「兩個條件都要成立」,不是呼叫 ``_dev_test_ca_explicitly_allowed()`` 算出來的。
    """
    import app.services.card_auth as card_auth

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", trust_ca)
    monkeypatch.setattr(
        settings, "ANILA_AUTH_MODE", "card-only" if card_only else "password"
    )

    dev_card_mode = is_trusted and not card_only
    if dev_card_mode:
        guard()  # 放行
    else:
        with pytest.raises(RuntimeError):
            guard()


def test_the_guard_agrees_with_the_real_definition_across_the_whole_value_space(
    monkeypatch, guard
):
    """行為層閉合:守衛放行 ⟺ ``_dev_test_ca_explicitly_allowed()[0]``。

    手寫表守的是「真定義本身沒被改」,這一條守的是「守衛沒有另寫一份」——
    兩者缺一，A-P2／A-P2b 就有一顆活得下來。
    """
    from app.services.card_auth import _dev_test_ca_explicitly_allowed
    import app.services.card_auth as card_auth

    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")

    diverged = []
    for trust_ca in _generated_value_space():
        for card_only in (True, False):
            monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", trust_ca)
            monkeypatch.setattr(
                settings, "ANILA_AUTH_MODE", "card-only" if card_only else "password"
            )
            expected_pass = _dev_test_ca_explicitly_allowed()[0]
            try:
                guard()
                guard_passes = True
            except RuntimeError:
                guard_passes = False
            if guard_passes != expected_pass:
                diverged.append((trust_ca, card_only, guard_passes, expected_pass))
    assert diverged == [], (
        f"守衛的 dev-card 判定與真定義分岔(值, card_only, 守衛放行?, 真定義?):{diverged!r}"
    )


def test_the_guard_consults_the_real_dev_card_helper_object(monkeypatch, guard):
    """身分證明:守衛呼叫的必須是 ``card_auth`` 那個函式**物件**本人。

    行為閉合抓得到「判定結果不一樣」的複本,抓不到「複製貼上、目前剛好一樣」的
    複本 —— 那種複本會在原版被修正的那天安靜地留在舊語意上。
    """
    import app.services.card_auth as card_auth

    real = card_auth._dev_test_ca_explicitly_allowed
    consulted: list[str] = []

    def _spy():
        consulted.append("dev_card_mode")
        return real()

    monkeypatch.setattr(card_auth, "_dev_test_ca_explicitly_allowed", _spy)
    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", True)
    monkeypatch.setenv("CARD_DEV_SKIP_NONCE_BINDING", "1")
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")

    with pytest.raises(RuntimeError):
        guard()
    assert consulted == ["dev_card_mode"], (
        "守衛沒有呼叫 card_auth._dev_test_ca_explicitly_allowed —— "
        "它自己另外寫了一份 dev-card 模式判定"
    )


def test_the_guard_consults_the_real_flag_helpers(monkeypatch, guard):
    """同上,值判定那一半:frozen 與 live 兩支都要被真的呼叫到。"""
    import app.services.card_auth as card_auth

    consulted: list[str] = []
    real_frozen = card_auth.card_dev_skip_nonce_binding_frozen
    real_live = card_auth.card_dev_skip_nonce_binding_enabled

    def _spy_frozen():
        consulted.append("frozen")
        return real_frozen()

    def _spy_live():
        consulted.append("live")
        return real_live()

    monkeypatch.setattr(card_auth, "card_dev_skip_nonce_binding_frozen", _spy_frozen)
    monkeypatch.setattr(card_auth, "card_dev_skip_nonce_binding_enabled", _spy_live)
    monkeypatch.delenv("CARD_DEV_SKIP_NONCE_BINDING", raising=False)
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")

    guard()
    assert "frozen" in consulted, (
        "守衛沒有讀凍結狀態 —— 那是驗章那一行真正消費的東西(sol Critical)"
    )
    assert "live" in consulted, "守衛沒有讀環境即時值"
