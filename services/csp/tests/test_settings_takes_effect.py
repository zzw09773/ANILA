# -*- coding: utf-8 -*-
"""十顆 C 類設定：改完**下一個請求**就生效，不是下一次重啟。

Task 1 蓋好了登錄表與回退鏈（``get_setting`` = DB → env → 程式預設），但那時
沒有任何消費端在讀它。登錄表把這十顆宣告成 ``restart_required=False``，而程式
碼當時說的是另一件事：七顆數值鈕讀的是 ``config.py`` 那個 **import 期就凍結**
的 ``settings`` 物件，三顆嚴格旗標讀的是 ``os.environ``（每請求，但畫面改不
到）。宣告與現實之間那道縫，正是本包要焊起來的地方。

**這一支測的是「縫焊起來了」，不是「函式會回傳東西」。**

每一顆都釘四件事：

1. **round-trip 三點**：``set_setting`` → ``platform_settings`` 那一列 →
   ``get_setting`` → **真正抵達下游函式的那個參數**。下界／內插值（0.375 型）／
   上界。只測邊界會漏掉「值被四捨五入／被當成旗標」這一類壞法；只測預設值會
   讓「什麼都沒接上」也全綠。
2. **回退鏈三態**：DB 有列 → env（若設）→ 程式預設，而且 **DB 那一列要贏過
   env**。舊的 env 佈署不需要遷移手續就繼續有效，這是氣隙升級的前提。
3. **同一個行程內改完即生效**：先讀到預設值、寫入、再讀一次要是新值。
   ⚠ 這一條是專門殺「首次呼叫後記憶」的：import 期擷取會被 round-trip 殺掉
   （import 早於那一列），但 ``@lru_cache`` 型的記憶不會 —— 它第一次回預設值
   然後把它快取起來，round-trip 反而可能剛好綠。門檻那一顆的模組 docstring
   把這件事寫成第一條不變式，這裡是它的十份副本。
4. **行為層**：值真的改變了下游的判斷（body 超長會 413、正規化真的換字、
   查詢真的展開、檔名真的用指定碼頁解、附件真的因為安全係數被擠出預算、
   部門真的因為層數上限被擋）。「讀得到新值」與「新值有作用」是兩件事。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import zipfile
from typing import Any, Callable

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.platform_setting import PlatformSetting, get_setting, set_setting
from app.services import (
    attachment_context,
    conversation_service,
    department_tree,
    message_action_service,
    search_expansion,
    zh_normalize_service,
)
from app.services.settings_registry import REGISTRY

# ── 探針用的固定輸入 ───────────────────────────────────────────────────────

#: 比例／係數兩顆是「乘出來的」，所以探針要回真正被乘出來的那個數字，不是
#: 比例本身 —— 消費端拿到的是預算與成本，不是 0.7。
_RATIO_WINDOW = 1_000_000
_SAFETY_TOKENS = 1_000_000

#: 民國紀年展開的探針查詢：105 → 2016。
_EXPANSION_QUERY = "105年函頒"

#: ⚠ 這串 bytes 在 cp950 與 gbk 底下**都解得開，但解出不同的字**。
#: 若挑一個 cp950 解不開的字，預設值那一輪會自動落到 gbk，測試就分不出
#: 「有讀到設定」與「剛好回退對了」——那正是這一顆最容易假綠的地方。
_ZIP_GBK_TEXT = "上"
_ZIP_RAW = _ZIP_GBK_TEXT.encode("gbk") + b".txt"
_ZIP_CP950_TEXT = _ZIP_RAW.decode("cp950")

assert _ZIP_CP950_TEXT != _ZIP_GBK_TEXT + ".txt", "探針失效：兩個碼頁解出同一個字"


def _cp437_member() -> zipfile.ZipInfo:
    """做一個「沒有 UTF-8 旗標、檔名被 zipfile 用 CP437 誤解」的 entry。"""
    member = zipfile.ZipInfo(filename=_ZIP_RAW.decode("cp437"))
    member.flag_bits = 0  # 沒有 0x800 → 走還原碼頁那條路
    return member


def _zip_probe(db: Session) -> str:
    from app.api.ingestion.documents import _zip_member_name

    return _zip_member_name(db, _cp437_member())


# ── 十顆的宣告：key → 下游參數的探針 ──────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Knob:
    key: str
    #: 回傳**真正抵達下游函式的那個參數**（不是 ``get_setting`` 本身）。
    probe: Callable[[Session], Any]
    #: 下界／內插值／上界。布林與字串這兩種在值域上退化成兩點。
    points: tuple[Any, ...]
    #: 寫進去的值 → 探針該回什麼。多數是恆等，比例／係數是乘出來的。
    expect: Callable[[Any], Any]
    #: 一個**與程式預設不同**的值，用來殺「首次呼叫後記憶」。
    distinct: Any
    #: env 層的原始字串與它該被讀成的探針結果。
    env_raw: str
    env_expected: Any
    #: 「DB 贏過 env」那一輪要用的 env —— 它解出來的結果必須**不等於**
    #: ``distinct``，否則兩層剛好同值，測試分不出是誰贏的（假綠）。
    #: 預設沿用 ``env_raw``；三顆旗標／字串型的要另外給。
    contrary_env_raw: str | None = None
    contrary_env_expected: Any = None

    def contrary(self) -> tuple[str, Any]:
        if self.contrary_env_raw is None:
            return self.env_raw, self.env_expected
        return self.contrary_env_raw, self.contrary_env_expected


def _identity(value: Any) -> Any:
    return value


KNOBS: tuple[Knob, ...] = (
    Knob(
        key="limits.department_max_depth",
        probe=lambda db: department_tree.max_depth(db),
        points=(1, 7, 10),
        expect=_identity,
        distinct=7,
        env_raw="4",
        env_expected=4,
    ),
    Knob(
        key="limits.message_max_siblings",
        probe=lambda db: conversation_service._max_siblings(db),
        points=(1, 375, 1000),
        expect=_identity,
        distinct=375,
        env_raw="9",
        env_expected=9,
    ),
    Knob(
        key="limits.action_max_body_chars",
        probe=lambda db: message_action_service._max_body_chars(db),
        points=(1, 375, 1_000_000),
        expect=_identity,
        distinct=375,
        env_raw="4096",
        env_expected=4096,
    ),
    Knob(
        key="limits.default_context_window",
        probe=lambda db: attachment_context.get_context_window(db, None),
        points=(256, 375_000, 10_000_000),
        expect=_identity,
        distinct=375_000,
        env_raw="65536",
        env_expected=65536,
    ),
    Knob(
        key="limits.attachment_budget_ratio",
        probe=lambda db: attachment_context.attachment_budget_tokens(db, _RATIO_WINDOW),
        points=(0.0, 0.375, 1.0),
        expect=lambda v: int(_RATIO_WINDOW * float(v)),
        distinct=0.375,
        env_raw="0.25",
        env_expected=250_000,
    ),
    Knob(
        key="limits.attachment_token_safety",
        probe=lambda db: attachment_context.effective_cost(db, _SAFETY_TOKENS),
        points=(1.0, 1.375, 4.0),
        expect=lambda v: int(_SAFETY_TOKENS * float(v)),
        distinct=1.375,
        env_raw="2.5",
        env_expected=2_500_000,
    ),
    Knob(
        key="limits.attachment_max_stored_tokens",
        probe=lambda db: attachment_context.max_stored_tokens(db),
        points=(1, 375, 100_000_000),
        expect=_identity,
        distinct=375,
        env_raw="12345",
        env_expected=12345,
    ),
    Knob(
        key="intl.zh_normalize",
        probe=lambda db: zh_normalize_service._enabled(db),
        points=(False, True),
        expect=_identity,
        distinct=False,
        env_raw="0",
        env_expected=False,
        contrary_env_raw="1",
        contrary_env_expected=True,
    ),
    Knob(
        key="intl.query_expansion",
        # 「開／關」在這一顆沒有獨立的讀取函式，旗標的下游就是展開本身。
        probe=lambda db: search_expansion.expand_query(db, _EXPANSION_QUERY)
        != _EXPANSION_QUERY,
        points=(False, True),
        expect=_identity,
        distinct=False,
        env_raw="0",
        env_expected=False,
        contrary_env_raw="1",
        contrary_env_expected=True,
    ),
    Knob(
        key="intl.zip_filename_encoding",
        probe=_zip_probe,
        points=("", "gbk"),
        expect=lambda v: (_ZIP_GBK_TEXT + ".txt") if v == "gbk" else _ZIP_CP950_TEXT,
        distinct="gbk",
        env_raw="gbk",
        env_expected=_ZIP_GBK_TEXT + ".txt",
        contrary_env_raw="",
        contrary_env_expected=_ZIP_CP950_TEXT,
    ),
)

_IDS = [k.key for k in KNOBS]


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """把這十顆的 env 從測試環境裡拔乾淨。

    留著的話，「回退到程式預設」那一輪會讀到跑測試的人殼裡剛好有的值 ——
    答案取決於誰在哪台機器上跑，那不是基準線。
    """
    for knob in KNOBS:
        env_name = REGISTRY[knob.key].env_name
        assert env_name is not None, f"{knob.key} 少了 env 名，回退鏈斷了一層"
        monkeypatch.delenv(env_name, raising=False)


def _param(knob: Knob, value: Any):
    return pytest.param(knob, value, id=f"{knob.key}::{value!r}")


_POINTS = [_param(k, v) for k in KNOBS for v in k.points]


# ── 1. round-trip 三點：set → 那一列 → get → 下游參數 ────────────────────


@pytest.mark.parametrize("knob, value", _POINTS)
def test_round_trip_reaches_the_downstream_parameter(db, knob: Knob, value: Any):
    spec = REGISTRY[knob.key]
    set_setting(db, knob.key, value)

    row = db.get(PlatformSetting, knob.key)
    assert row is not None, f"{knob.key}：set_setting 沒有留下那一列"
    assert row.value == spec.value_type.format(value)
    assert get_setting(db, knob.key) == value

    assert knob.probe(db) == knob.expect(value), (
        f"{knob.key} = {value!r} 存進 DB 了，下游拿到的卻不是它"
    )


@pytest.mark.parametrize("knob", KNOBS, ids=_IDS)
def test_out_of_domain_writes_are_refused(db, knob: Knob):
    """值域由登錄表那一筆把關；收得下來的必須等於算得出來的。"""
    spec = REGISTRY[knob.key]
    bounds = getattr(spec.domain_fn, "bounds", None)
    if bounds is None:
        if knob.key != "intl.zip_filename_encoding":
            pytest.skip("這一顆沒有數值值域")
        with pytest.raises(ValueError):
            set_setting(db, knob.key, "no-such-codepage-9x")
        return

    low, high = bounds
    for bad in (low - 1, high + 1):
        with pytest.raises(ValueError):
            set_setting(db, knob.key, bad)
    assert db.get(PlatformSetting, knob.key) is None


# ── 2. 回退鏈：DB → env → 程式預設，順序不可以顛倒 ───────────────────────


@pytest.mark.parametrize("knob", KNOBS, ids=_IDS)
def test_env_still_works_when_no_one_has_touched_the_page(db, knob: Knob, monkeypatch):
    monkeypatch.setenv(REGISTRY[knob.key].env_name, knob.env_raw)
    assert db.get(PlatformSetting, knob.key) is None
    assert knob.probe(db) == knob.env_expected


@pytest.mark.parametrize("knob", KNOBS, ids=_IDS)
def test_default_when_neither_db_nor_env(db, knob: Knob):
    assert knob.probe(db) == knob.expect(REGISTRY[knob.key].default)


@pytest.mark.parametrize("knob", KNOBS, ids=_IDS)
def test_db_row_beats_env(db, knob: Knob, monkeypatch):
    """管理員從畫面改過之後，容器裡那個舊 env 就不可以再說話。"""
    raw, env_result = knob.contrary()
    assert env_result != knob.expect(knob.distinct), (
        f"{knob.key}：DB 與 env 兩層的探針結果一樣，這一輪分不出誰贏"
    )
    monkeypatch.setenv(REGISTRY[knob.key].env_name, raw)
    set_setting(db, knob.key, knob.distinct)
    assert knob.probe(db) == knob.expect(knob.distinct)


# ── 3. 同一個行程內改完即生效（殺記憶／殺 import 期擷取） ────────────────


@pytest.mark.parametrize("knob", KNOBS, ids=_IDS)
def test_takes_effect_without_restart(db, knob: Knob):
    default_result = knob.expect(REGISTRY[knob.key].default)
    assert knob.probe(db) == default_result

    set_setting(db, knob.key, knob.distinct)

    assert knob.probe(db) == knob.expect(knob.distinct), (
        f"{knob.key}：第一次讀之後就記住了，畫面上改的值要等重啟才生效"
    )
    assert knob.expect(knob.distinct) != default_result


# ── 4. 行為層：新值真的改變了下游的判斷 ──────────────────────────────────


def test_action_body_cap_rejects_by_the_stored_value(db):
    set_setting(db, "limits.action_max_body_chars", 5)
    message_action_service._validate_body(db, "x" * 5)
    with pytest.raises(HTTPException) as exc:
        message_action_service._validate_body(db, "x" * 6)
    assert exc.value.status_code == 413


def test_zh_normalize_off_stores_the_original_text(db):
    set_setting(db, "intl.zh_normalize", False)
    assert zh_normalize_service.prepare_message_content(db, "assistant", "软件测试") == (
        "软件测试",
        0,
    )
    set_setting(db, "intl.zh_normalize", True)
    content, changed = zh_normalize_service.prepare_message_content(
        db, "assistant", "软件测试"
    )
    assert content == "軟體測試"
    assert changed > 0


def test_query_expansion_off_returns_the_query_unchanged(db):
    set_setting(db, "intl.query_expansion", False)
    assert search_expansion.expand_query(db, _EXPANSION_QUERY) == _EXPANSION_QUERY
    set_setting(db, "intl.query_expansion", True)
    assert "2016" in search_expansion.expand_query(db, _EXPANSION_QUERY)


def test_zip_filename_encoding_decodes_with_the_stored_codepage(db):
    set_setting(db, "intl.zip_filename_encoding", "gbk")
    assert _zip_probe(db) == _ZIP_GBK_TEXT + ".txt"


def test_token_safety_pushes_an_attachment_out_of_the_budget(db):
    """安全係數是乘在每一份附件成本上的 —— 迴圈裡也要讀到新值。"""
    from types import SimpleNamespace

    rows = [
        SimpleNamespace(id=1, extract_status="ok", token_count=600),
        SimpleNamespace(id=2, extract_status="ok", token_count=300),
    ]
    set_setting(db, "limits.attachment_token_safety", 1.0)
    admitted, excluded = attachment_context.admit(db, rows, 1000)
    assert (admitted, excluded) == ([1, 2], [])

    set_setting(db, "limits.attachment_token_safety", 1.5)
    admitted, excluded = attachment_context.admit(db, rows, 1000)
    assert (admitted, excluded) == ([1], [2])


def test_sibling_cap_blocks_by_the_stored_value(db):
    """**擋人的那一行**（``_enforce_sibling_cap`` 裡的 ``cap``）要讀到存進 DB 的值。

    ⚠ 這一支是補第一輪驗收的探針 P1 咬不到的那個縫：把
    ``conversation_service.py`` 那一行改回讀 import 期凍結的 ``settings``，
    當時 262 支測試全綠。原因是唯一的行為測試把 env 設成 ``"20"`` ——
    而 20 同時是登錄表預設**也是** ``config.py`` 的欄位預設，那一輪根本分不出
    「有讀到設定」與「讀到凍結的預設值」。**所以這裡用的每一個值都刻意不是 20。**

    釘兩個值而不是一個：一個證明擋得動、一個證明**同一個行程內**改了立刻改判。
    """
    from app.models.conversation import Conversation
    from app.models.message import Message
    from tests.conftest import make_user

    user = make_user(db, username="sibling_cap_probe")
    conv = Conversation(user_id=user.id, title="分支上限測試")
    db.add(conv)
    db.flush()
    root = Message(conversation_id=conv.id, parent_id=None, role="user", content="Q")
    db.add(root)
    db.flush()
    for i in range(3):
        db.add(
            Message(
                conversation_id=conv.id,
                parent_id=root.id,
                role="assistant",
                content=f"A{i}",
            )
        )
    db.flush()
    assert conversation_service._sibling_count(db, conv.id, root.id) == 3

    # 上限 5（≠ 預設 20）、已有 3 個 → 還開得動。
    set_setting(db, "limits.message_max_siblings", 5)
    conversation_service._enforce_sibling_cap(db, conv.id, root.id)

    # 同一個行程內降到 3（≠ 預設 20）→ 下一次呼叫就該擋。
    set_setting(db, "limits.message_max_siblings", 3)
    with pytest.raises(HTTPException) as exc:
        conversation_service._enforce_sibling_cap(db, conv.id, root.id)
    assert exc.value.status_code == 409
    # 送到使用者眼前的那個數字，也必須是存進去的那一個。
    assert "3" in exc.value.detail


def test_normalize_never_raises_when_the_setting_lookup_fails(db, monkeypatch, caplog):
    """設定讀不到時：原文落庫＋一行 warning，**不可以**往外拋。

    ⚠ 改造前 ``_enabled`` 讀的是 ``os.environ``，不可能拋，所以模組 docstring
    那句「永不拋出」是白拿的。改成每請求查 DB 之後，那句承諾要靠這一支才成立。
    正規化是錦上添花：設定查不到不可以變成使用者送不出訊息。
    """
    def _boom(*_args, **_kwargs):
        raise RuntimeError("platform_settings 讀取失敗（模擬 DB 抖動）")

    monkeypatch.setattr(zh_normalize_service, "get_setting", _boom)

    with caplog.at_level(logging.WARNING, logger=zh_normalize_service.__name__):
        result = zh_normalize_service.prepare_message_content(db, "assistant", "软件测试")

    assert result == ("软件测试", 0), "設定讀不到時應該原文落庫，不是改字也不是拋"
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "靜默吞掉比拋出更危險 —— 至少要留一行 warning"
    )


def test_department_depth_cap_blocks_by_the_stored_value(db):
    from app.api.departments import _validate_parent_assignment
    from app.models.department import Department

    root = Department(name="設定生效測試院", parent_id=None, is_active=True)
    db.add(root)
    db.flush()

    set_setting(db, "limits.department_max_depth", 3)
    _validate_parent_assignment(db, parent_id=root.id)

    set_setting(db, "limits.department_max_depth", 1)
    with pytest.raises(HTTPException) as exc:
        _validate_parent_assignment(db, parent_id=root.id)
    assert exc.value.status_code == 400


def test_icons_endpoint_reports_the_stored_body_cap(db, client):
    """畫面上顯示的上限與後端實際擋人的上限，必須是同一個數字。"""
    from tests.conftest import login, make_user

    make_user(db, username="knob_admin", role="admin")
    headers = {"Authorization": f"Bearer {login(client, username='knob_admin')}"}

    set_setting(db, "limits.action_max_body_chars", 4321)
    db.commit()

    resp = client.get("/api/message-actions/icons", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["max_body_chars"] == 4321
