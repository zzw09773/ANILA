# -*- coding: utf-8 -*-
"""刷卡之後、被核准之前,使用者讀到的那一句話。

為什麼值得一個檔案
==================
八月上線那一週,全院三千人第一次刷卡,絕大多數人會停在「等核准」這一頁。
他們在那一刻只想知道兩件事:**我到底登記到了沒有**、**還要不要再做什麼**。
原本的文案(「註冊資料已記錄」「一旦管理員核准…」)只回答了第二件事的一半,
沒有明講第一件事 —— 於是有人會反覆刷卡,或以為自己漏了一步。

卡登流程會回三種 pending 姿態,對應的處境**不一樣**,所以文案不能合併:

============================  ==================================  =========
來源                          狀態                                使用者還要做什麼
============================  ==================================  =========
``/card/verify``              ``pending_registration``            要選單位並送出
``/card/complete-registration``  ``pending_approval``             什麼都不用做
``/card/verify``(已填單位)   ``pending_approval``                什麼都不用做
============================  ==================================  =========

本檔守的是**語意**,不是字面:pending_registration 不可以宣稱使用者已完成
註冊(他還沒),pending_approval 一定要明講已完成註冊、正在等核准。

作法:替換掉 ``verify_card_and_resolve_user``(卡片密碼學驗證由
``test_card_endpoints.py`` / ``test_card_auth.py`` 用合成 PKI 守著,本檔一支
測試不碰它),只行使 endpoint 的 pending 分支與文案。

⚠ repo 是 PUBLIC:以下工號 / 姓名 / email 全是合成的假資料。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.auth import card as card_api
from app.config import settings
from app.models.department import Department
from app.services.card_auth import CardClaims
from app.services.card_auth_service import issue_registration_token

from tests.conftest import make_user


EMP_ID = "9100001"
DISPLAY_NAME = "測試員丙"
EMAIL = "pending-wording@example.invalid"

VERIFY_URL = "/api/auth/card/verify"
COMPLETE_URL = "/api/auth/card/complete-registration"


@pytest.fixture
def card_login_on(monkeypatch):
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "mixed")
    monkeypatch.setattr(settings, "CARD_INITIAL_OWNERS", "")


def _claims() -> CardClaims:
    return CardClaims(
        employee_id=EMP_ID,
        display_name=DISPLAY_NAME,
        email=EMAIL,
        card_serial="CSTEST0000000099",
    )


def _stub_card_verification(monkeypatch, user):
    """讓 ``/card/verify`` 直接解析到指定的 user,跳過密碼學那一段。"""

    def _fake(db, *, signature_b64, challenge_token, card_serial):
        return user, _claims()

    monkeypatch.setattr(card_api, "verify_card_and_resolve_user", _fake)


def _swipe(client: TestClient) -> dict:
    resp = client.post(
        VERIFY_URL,
        json={"challenge_token": "stub", "signature": "stub", "card_serial": None},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def _make_department(db: Session, name: str = "測試單位") -> Department:
    dept = Department(name=name, is_active=True)
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


# ── 狀態 ①:pending_registration —— 還沒註冊完,不可以說「已完成註冊」 ────────


def test_pending_registration_says_registration_is_not_finished_yet(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    user = make_user(db, username=EMP_ID, is_approved=False, department_id=None)
    _stub_card_verification(monkeypatch, user)

    body = _swipe(client)

    assert body["status"] == "pending_registration"
    message = body["message"]
    # 還要他自己動手:必須說出「選單位 / 送出」這件事。
    assert "單位" in message
    assert "送出" in message
    # 這一步最容易寫錯的方向 —— 對還沒填單位的人謊稱已經註冊好了。
    assert "已完成註冊" not in message
    assert "已註冊" not in message


# ── 狀態 ②:送出單位當下的回應 ──────────────────────────────────────────────


def test_completing_registration_says_you_are_registered_and_waiting(
    client: TestClient, db: Session, card_login_on
):
    user = make_user(db, username=EMP_ID, is_approved=False, department_id=None)
    dept = _make_department(db)
    token, _ = issue_registration_token(user.id)

    resp = client.post(
        COMPLETE_URL,
        json={"registration_token": token, "department_id": dept.id},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending_approval"
    _assert_registered_and_waiting(body["message"])


# ── 狀態 ③:之後再刷一次卡 —— 同樣的處境,同樣的一句話 ──────────────────────


def test_pending_approval_swipe_says_you_are_registered_and_waiting(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    dept = _make_department(db, "測試單位二")
    user = make_user(
        db, username=EMP_ID, is_approved=False, department_id=dept.id
    )
    _stub_card_verification(monkeypatch, user)

    body = _swipe(client)

    assert body["status"] == "pending_approval"
    assert body["registration_token"] is None  # 已經沒有事情可以做了
    _assert_registered_and_waiting(body["message"])


def test_the_two_pending_approval_paths_say_exactly_the_same_thing(
    client: TestClient, db: Session, monkeypatch, card_login_on
):
    """同一個處境(已註冊、等核准)只該有一句話。

    兩支 endpoint 各自寫死字串的話,改一邊忘了改另一邊,使用者會在同一天的
    兩個畫面上讀到兩種說法,還會以為自己的狀態變了。
    """
    dept = _make_department(db, "測試單位三")
    user = make_user(db, username=EMP_ID, is_approved=False, department_id=None)
    token, _ = issue_registration_token(user.id)
    completed = client.post(
        COMPLETE_URL, json={"registration_token": token, "department_id": dept.id}
    ).json()

    # complete-registration 是在另一個 session 上寫的 —— 不 refresh 的話,下面
    # 刷卡拿到的還是 department_id=None 的舊 instance,會走錯分支。
    db.refresh(user)
    assert user.department_id == dept.id
    _stub_card_verification(monkeypatch, user)
    swiped = _swipe(client)

    assert completed["message"] == swiped["message"]


def _assert_registered_and_waiting(message: str) -> None:
    """「已經登記到了」+「不用再做什麼,等核准」——兩件事都要說出口。"""
    assert "已完成註冊" in message
    assert "核准" in message
    # 舊文案只講管理員將來會核准,沒回答「我登記到了沒」。這行擋它回來。
    assert message != "註冊資料已記錄，請等待管理員核准。"
