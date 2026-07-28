# -*- coding: utf-8 -*-
"""`POST /api/attachments` 的擁有權驗證 —— 補救計畫 W3-12f。

缺口
----
`api/attachments.py:35` 的 `upload` 把 `conversation_id` / `message_id` 直接從
form 收下,原封不動交給 `attachment_service.upload_attachment()`,而該函式**只**
把值寫進 `Attachment` 列:

    att = Attachment(
        conversation_id=conversation_id,   # ← 沒驗過屬於誰
        message_id=message_id,             # ← 同上
        uploaded_by=user.id,
    )

所以 A 可以把附件綁到 B 的對話上。**下載端有擋**(`get_attachment` 檢查
`uploaded_by`),所以 bytes 不會洩漏 —— 這個缺口的方向是**寫入**而不是讀取:

1. A 上傳一個檔名為任意字串的附件、綁 B 的 `conversation_id`;
2. B 的對話從此多出一列 `attachments` —— W3-12d 要接的前端 chip 會把
   **A 控制的 `filename`** 渲染在 B 的對話裡(跨使用者內容注入);
3. 若 B 的對話是機密等級,這是一筆**未經稽核**、未經分級判定的寫入,直接落進
   受管容器 —— 而附件本身在分類/retention 體系外(W3-12e)。

`message_id` 是同一個缺口的第二個入口,而且更隱蔽:即使把 `conversation_id`
擋住,只給 `message_id` 也能達成同樣效果。所以兩個欄位都要驗,**而且兩者同時
給的時候要驗一致性**,否則可以用自己的 conversation_id 配上別人的 message_id
繞過。

這支檔案在修正前的實際結果是 3 failed 4 passed(見 commit message)。
"""
from __future__ import annotations

import io

from app.models.conversation import Conversation
from app.models.message import Message
from tests.conftest import login, make_user


def _mk_conversation(db, user, title="受害者的對話") -> Conversation:
    conv = Conversation(user_id=user.id, title=title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _mk_message(db, conv, content="hello") -> Message:
    msg = Message(conversation_id=conv.id, role="user", content=content)
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _upload(client, token, *, conversation_id=None, message_id=None,
            filename="note.txt"):
    data = {}
    if conversation_id is not None:
        data["conversation_id"] = str(conversation_id)
    if message_id is not None:
        data["message_id"] = str(message_id)
    return client.post(
        "/api/attachments",
        files={"file": (filename, io.BytesIO(b"payload"), "text/plain")},
        data=data,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── 缺口本體 ──────────────────────────────────────────────────────────────────

def test_cannot_attach_to_another_users_conversation(client, db):
    """A 綁 B 的 conversation_id → 403,且 B 的對話不得多出附件。"""
    victim = make_user(db, username="victim")
    make_user(db, username="attacker")
    conv = _mk_conversation(db, victim)

    token = login(client, username="attacker")
    resp = _upload(client, token, conversation_id=conv.id,
                   filename="請點我-invoice.txt")

    assert resp.status_code == 403, resp.text
    # 光看 status code 不夠 —— 要確認真的沒寫進去(fail-closed 而非「回 403 但已落庫」)
    from app.models.attachment import Attachment
    leaked = (
        db.query(Attachment)
        .filter(Attachment.conversation_id == conv.id)
        .count()
    )
    assert leaked == 0


def test_cannot_attach_to_another_users_message(client, db):
    """只給 message_id(不給 conversation_id)是同一缺口的第二個入口。"""
    victim = make_user(db, username="victim")
    make_user(db, username="attacker")
    conv = _mk_conversation(db, victim)
    msg = _mk_message(db, conv)

    token = login(client, username="attacker")
    resp = _upload(client, token, message_id=msg.id)

    assert resp.status_code == 403, resp.text
    from app.models.attachment import Attachment
    assert db.query(Attachment).filter(Attachment.message_id == msg.id).count() == 0


def test_cannot_mix_own_conversation_with_foreign_message(client, db):
    """兩者同時給時要驗一致性,否則自己的 conv_id 配別人的 msg_id 就能繞過。"""
    victim = make_user(db, username="victim")
    attacker = make_user(db, username="attacker")
    victim_conv = _mk_conversation(db, victim)
    victim_msg = _mk_message(db, victim_conv)
    own_conv = _mk_conversation(db, attacker, title="攻擊者自己的對話")

    token = login(client, username="attacker")
    resp = _upload(client, token, conversation_id=own_conv.id,
                   message_id=victim_msg.id)

    assert resp.status_code in (403, 400), resp.text


# ── 不得誤擋既有正常用法(回歸護欄)────────────────────────────────────────────

def test_owner_can_attach_to_own_conversation(client, db):
    user = make_user(db, username="alice")
    conv = _mk_conversation(db, user, title="自己的對話")

    token = login(client, username="alice")
    resp = _upload(client, token, conversation_id=conv.id)

    assert resp.status_code == 201, resp.text
    assert resp.json()["conversation_id"] == conv.id


def test_owner_can_attach_to_own_message(client, db):
    user = make_user(db, username="alice")
    conv = _mk_conversation(db, user, title="自己的對話")
    msg = _mk_message(db, conv)

    token = login(client, username="alice")
    resp = _upload(client, token, conversation_id=conv.id, message_id=msg.id)

    assert resp.status_code == 201, resp.text
    assert resp.json()["message_id"] == msg.id


def test_unbound_upload_still_allowed(client, db):
    """兩個欄位都不給是正常用法(composer 先上傳、append 之後才綁)。"""
    make_user(db, username="alice")
    token = login(client, username="alice")

    resp = _upload(client, token)

    assert resp.status_code == 201, resp.text
    assert resp.json()["conversation_id"] is None
    assert resp.json()["message_id"] is None


def test_nonexistent_conversation_is_rejected(client, db):
    """不存在的 id 不該被當成「沒綁」默默接受 —— 那會讓錯字變成靜默資料遺失。"""
    make_user(db, username="alice")
    token = login(client, username="alice")

    resp = _upload(client, token, conversation_id=999999)

    assert resp.status_code in (403, 404), resp.text
