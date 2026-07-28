# -*- coding: utf-8 -*-
"""建立分享連結必須尊重 `ENABLE_PUBLIC_SHARE` —— 補救計畫 W3-7c(後端半邊)。

缺口
----
`conversation_service.create_share()` **從未讀** `settings.ENABLE_PUBLIC_SHARE`,
而讀取端有擋(`api/public_share.py:50-53` 回 404)。`prod-intranet-card` 的部署
姿態是 `ENABLE_PUBLIC_SHARE=false`,結果:

    使用者按下「建立分享連結」→ 成功、拿到 URL、UI 顯示成功
    → 把 URL 傳給同事 → 同事**一定**看到 404

沒有任何一端告訴使用者這個部署根本沒開分享。這是「看起來有在工作」的失敗:
平台給了一個保證壞掉的東西,而且是使用者主動要拿去給別人的東西。

CLAUDE.md §5.1 把它與分類面的缺陷明確分開記:「分類面已修」(`is_publicly_shareable`
只允許無機密)與「旗標面沒修」是**兩個不同缺陷**,別混為一談。這支修的是後者。

為什麼是 403 而不是 404
-----------------------
讀取端用 404 是刻意的 —— 那條路徑**未經認證**,404 讓分享 token 的存在無法被探測。
建立端不一樣:呼叫者已認證、操作的是自己的對話,沒有可探測的東西;而且
`GET /api/capabilities` 本來就會把 `enable_public_share` 告訴已認證的使用者
(W1-3 刻意放進去的,就是為了讓前端能隱藏這顆鈕)。所以這裡回 403 + 明確訊息,
讓使用者知道「不是你做錯,是這個部署沒開」。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.models.conversation import Conversation, ConversationShare
from tests.conftest import login, make_user


def _conv(db, user, level: str = "無機密") -> Conversation:
    conv = Conversation(user_id=user.id, title="可分享的對話",
                        classification_level=level)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


@pytest.fixture
def share_disabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", False)
    return settings


@pytest.fixture
def share_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", True)
    return settings


# ── 旗標關閉時不得建立 ────────────────────────────────────────────────────────

def test_cannot_create_share_when_flag_is_off(client, db, share_disabled):
    user = make_user(db, username="alice")
    conv = _conv(db, user)
    token = login(client, username="alice")

    resp = client.post(
        f"/api/conversations/{conv.id}/shares",
        json={"mode": "read_only"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403, resp.text
    # 訊息要讓使用者知道「不是你做錯」—— 否則他會一直重試
    assert "部署" in resp.text or "未啟用" in resp.text
    # 而且**真的沒有落列**(不是回 403 但已經建了一列)
    assert db.query(ConversationShare).filter(
        ConversationShare.conversation_id == conv.id
    ).count() == 0


def test_service_layer_also_refuses(db, share_disabled):
    """繞過 API 直呼 service 也要擋 —— 別把檢查只放在 router。"""
    from fastapi import HTTPException
    from app.services import conversation_service as svc

    user = make_user(db, username="bob")
    conv = _conv(db, user)
    with pytest.raises(HTTPException) as exc:
        svc.create_share(db, conv.id, user)
    assert exc.value.status_code == 403


# ── 旗標開啟時行為不變(不得誤擋)────────────────────────────────────────────

def test_can_create_share_when_flag_is_on(client, db, share_enabled):
    user = make_user(db, username="alice")
    conv = _conv(db, user)
    token = login(client, username="alice")

    resp = client.post(
        f"/api/conversations/{conv.id}/shares",
        json={"mode": "read_only"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["token"]
    assert db.query(ConversationShare).filter(
        ConversationShare.conversation_id == conv.id
    ).count() == 1


def test_classification_gate_still_independent(client, db, share_enabled):
    """旗標開著也不能分享受控對話 —— 兩個缺陷是獨立的兩道 gate。

    用**營業秘密**當案例:legacy `classified` boolean 對它是 False,所以吃
    boolean 的舊寫法在這裡會綠。
    """
    user = make_user(db, username="alice")
    conv = _conv(db, user, level="營業秘密")
    token = login(client, username="alice")

    resp = client.post(
        f"/api/conversations/{conv.id}/shares",
        json={"mode": "read_only"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text
    assert db.query(ConversationShare).count() == 0


def test_existing_shares_still_listable_when_flag_off(client, db, share_enabled,
                                                     monkeypatch):
    """旗標關掉之前建的連結仍要看得到 —— 否則使用者無法清理它們。

    這是刻意的不對稱:**建立**要擋(避免產出保證壞掉的東西),**列出**不擋
    (讓人看得到已經發出去的連結並撤銷)。
    """
    user = make_user(db, username="alice")
    conv = _conv(db, user)
    token = login(client, username="alice")
    created = client.post(
        f"/api/conversations/{conv.id}/shares",
        json={"mode": "read_only"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert created.status_code in (200, 201), created.text

    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", False)
    listed = client.get(
        f"/api/conversations/{conv.id}/shares",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200, listed.text
    assert len(listed.json()) == 1
