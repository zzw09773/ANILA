# -*- coding: utf-8 -*-
"""「交給同事」端到端 —— 通訊錄查詢 ＋ 交接真的換手。

驗的是**行為**不是資料列：接受之後接收者能不能真的讀、真的再送一則訊息；
原擁有者是不是還看得到。每個 class 的 docstring 標明「把哪一行 production
改掉會讓它轉紅」(mutation evidence)。

背景：2026-07-30 這顆按鈕被拿掉，因為它送出的酬載沒有 `to_user_id`，
accept/reject 又只翻狀態不換手 —— 兩段都是靜默成功。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.department import Department
from app.models.handoff import Handoff
from app.models.user import User
from app.utils.security import create_access_token
from tests.conftest import make_user


def _bearer(user: User) -> dict:
    token = create_access_token({
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _make_dept(db: Session, name: str) -> Department:
    d = Department(name=name, parent_id=None, is_active=True)
    db.add(d)
    db.commit()
    db.refresh(d)
    return d


def _make_conv(db: Session, owner: User, level: str = "無機密") -> Conversation:
    from app.schemas.contracts.classification import ClassificationLevel as CL

    conv = Conversation(user_id=owner.id, title="交接測試對話")
    db.add(conv)
    db.commit()
    conv.classification_level = level
    conv.classified = CL.from_storage(level) >= CL.RESTRICTED
    db.commit()
    db.refresh(conv)
    return conv


def _say(client: TestClient, user: User, conv_id: int, content: str):
    return client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=_bearer(user),
        json={"role": "user", "content": content},
    )


def _set_level(db: Session, conv: Conversation, level: str) -> None:
    from app.schemas.contracts.classification import ClassificationLevel as CL

    row = db.query(Conversation).filter(Conversation.id == conv.id).one()
    row.classification_level = level
    row.classified = CL.from_storage(level) >= CL.RESTRICTED
    db.commit()


# ── 驗收 1 / 5：通訊錄只回姓名與單位，未登入拿不到東西 ────────────────────────


class TestDirectoryLookup:
    """Mutant: 在 `DirectoryEntry` 加一個 `email` 欄位並在 query 裡 select
    `User.email` → `test_directory_returns_exactly_name_and_unit` 轉紅
    (欄位集斷言故意是等號，不是包含)。
    Mutant: 把 `Depends(get_current_user)` 拿掉 → 未登入那條轉紅。
    """

    def test_directory_returns_exactly_name_and_unit(
        self, client: TestClient, db: Session
    ):
        dept = _make_dept(db, "dir-資訊所")
        asker = make_user(db, username="dir_asker")
        make_user(db, username="dir_colleague", department_id=dept.id)

        resp = client.get(
            "/api/directory/users",
            headers=_bearer(asker),
            params={"q": "dir_colleague"},
        )
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1, rows
        row = rows[0]
        # 欄位集是等號：日後有人多回一個欄位，這條就會故意壞掉。
        assert set(row.keys()) == {"id", "username", "department"}
        assert row["username"] == "dir_colleague"
        assert row["department"] == "dir-資訊所"

    def test_ordinary_user_is_not_blocked_by_admin_gate(
        self, client: TestClient, db: Session
    ):
        """一般使用者查得到人 —— `GET /api/users` 對他們是 403。"""
        asker = make_user(db, username="dir_plain")
        make_user(db, username="dir_target_a")
        make_user(db, username="dir_target_b")

        admin_view = client.get("/api/users", headers=_bearer(asker))
        assert admin_view.status_code == 403

        resp = client.get(
            "/api/directory/users", headers=_bearer(asker), params={"q": "dir_target"}
        )
        assert resp.status_code == 200, resp.text
        assert {r["username"] for r in resp.json()} == {
            "dir_target_a",
            "dir_target_b",
        }

    def test_directory_hides_self_inactive_and_unapproved(
        self, client: TestClient, db: Session
    ):
        asker = make_user(db, username="dirx_self")
        make_user(db, username="dirx_pending", is_approved=False)
        gone = make_user(db, username="dirx_gone")
        gone.is_active = False
        db.commit()
        make_user(db, username="dirx_ok")

        resp = client.get(
            "/api/directory/users", headers=_bearer(asker), params={"q": "dirx_"}
        )
        assert resp.status_code == 200, resp.text
        assert [r["username"] for r in resp.json()] == ["dirx_ok"]

    def test_unauthenticated_gets_nothing(self, client: TestClient, db: Session):
        make_user(db, username="dir_secretish")
        resp = client.get("/api/directory/users", params={"q": "dir_"})
        assert resp.status_code == 401, resp.text
        assert resp.headers["content-type"].startswith("application/json")
        assert "dir_secretish" not in resp.text


# ── 驗收 2：整趟來回 —— B 接受之後真的讀得到、續得下去；A 也還在 ─────────────


class TestAcceptTransfersForReal:
    """Mutant: 把 `handoff_transfer.accept_handoff` 裡的
    `conv.user_id = recipient.id` 那一行刪掉 → 接收者續聊那步變 404，轉紅。
    Mutant: 把 `_keep_previous_owner_reading(...)` 呼叫刪掉 → 原擁有者
    讀取那步變 404，轉紅。
    """

    def test_round_trip_hand_over_and_continue(
        self, client: TestClient, db: Session
    ):
        alice = make_user(db, username="ho_alice")
        bob = make_user(db, username="ho_bob")
        conv = _make_conv(db, alice)
        assert _say(client, alice, conv.id, "第一則").status_code == 201

        # 接受之前，B 什麼都看不到 —— 交接請求本身不等於存取權。
        early = client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(bob)
        )
        assert early.status_code == 404

        created = client.post(
            "/api/handoffs",
            headers=_bearer(alice),
            json={"conversation_id": conv.id, "to_user_id": bob.id, "note": "換你顧"},
        )
        assert created.status_code == 201, created.text
        handoff_id = created.json()["id"]
        assert created.json()["to_user_id"] == bob.id

        inbox = client.get("/api/handoffs", headers=_bearer(bob))
        assert inbox.status_code == 200, inbox.text
        mine = [h for h in inbox.json() if h["id"] == handoff_id]
        assert mine and mine[0]["status"] == "pending"

        accepted = client.post(
            f"/api/handoffs/{handoff_id}/accept", headers=_bearer(bob)
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "accepted"

        # B 真的讀得到。
        read = client.get(f"/api/conversations/{conv.id}", headers=_bearer(bob))
        assert read.status_code == 200, read.text
        assert [m["content"] for m in read.json()["messages"]] == ["第一則"]

        # B 真的續得下去(不是只能看)。
        cont = _say(client, bob, conv.id, "我接手了")
        assert cont.status_code == 201, cont.text

        # A 沒有被踢掉：讀得到、而且新訊息也看得到。
        still = client.get(f"/api/conversations/{conv.id}", headers=_bearer(alice))
        assert still.status_code == 200, still.text
        assert "我接手了" in [m["content"] for m in still.json()["messages"]]

        # A 的側欄還留著這串對話。
        listing = client.get("/api/conversations", headers=_bearer(alice))
        assert listing.status_code == 200, listing.text
        assert conv.id in [c["id"] for c in listing.json()]


# ── 驗收 3：拒絕什麼都不變；第三人既接不了也看不到 ───────────────────────────


class TestRejectAndThirdParty:
    """Mutant: 讓 `reject_handoff` 改呼叫 `transfer.accept_handoff`
    → 「拒絕後 B 讀得到」轉紅。
    Mutant: 拿掉 `_load_pending_for_recipient` 的 `to_user_id != user.id`
    判斷 → 第三人接受那條轉紅。
    """

    def _pending(self, client: TestClient, alice: User, bob: User, conv_id: int) -> int:
        created = client.post(
            "/api/handoffs",
            headers=_bearer(alice),
            json={"conversation_id": conv_id, "to_user_id": bob.id},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    def test_reject_leaves_everything_unchanged(
        self, client: TestClient, db: Session
    ):
        alice = make_user(db, username="rj_alice")
        bob = make_user(db, username="rj_bob")
        conv = _make_conv(db, alice)
        handoff_id = self._pending(client, alice, bob, conv.id)

        resp = client.post(
            f"/api/handoffs/{handoff_id}/reject", headers=_bearer(bob)
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"

        assert client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(bob)
        ).status_code == 404
        assert _say(client, bob, conv.id, "偷寫").status_code == 404

        db.expire_all()
        assert db.query(Conversation).filter(
            Conversation.id == conv.id
        ).one().user_id == alice.id
        assert _say(client, alice, conv.id, "還是我的").status_code == 201

    def test_third_party_can_neither_see_nor_accept(
        self, client: TestClient, db: Session
    ):
        alice = make_user(db, username="tp_alice")
        bob = make_user(db, username="tp_bob")
        carol = make_user(db, username="tp_carol")
        conv = _make_conv(db, alice)
        handoff_id = self._pending(client, alice, bob, conv.id)

        listing = client.get("/api/handoffs", headers=_bearer(carol))
        assert listing.status_code == 200, listing.text
        assert handoff_id not in [h["id"] for h in listing.json()]

        for verb in ("accept", "reject"):
            resp = client.post(
                f"/api/handoffs/{handoff_id}/{verb}", headers=_bearer(carol)
            )
            assert resp.status_code == 404, (verb, resp.text)

        assert client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(carol)
        ).status_code == 404
        db.expire_all()
        assert db.query(Handoff).filter(
            Handoff.id == handoff_id
        ).one().status == "pending"


# ── 驗收 4：列管對話交不出去(這條不可跳過)─────────────────────────────────


class TestClassificationGate:
    """Mutant: 把 `handoff_transfer.guard_transferable` 裡的
    `if not outbound_action_allowed(level): raise ...` 整段刪掉 →
    兩條都轉紅(建立那條變 201、接受那條變 200 且 B 讀得到)。
    """

    def test_classified_conversation_cannot_be_handed_off(
        self, client: TestClient, db: Session
    ):
        alice = make_user(db, username="cls_alice")
        bob = make_user(db, username="cls_bob")
        conv = _make_conv(db, alice, level="密")

        resp = client.post(
            "/api/handoffs",
            headers=_bearer(alice),
            json={"conversation_id": conv.id, "to_user_id": bob.id},
        )
        assert resp.status_code == 403, resp.text
        assert "密" in resp.json()["detail"]
        assert db.query(Handoff).count() == 0
        assert client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(bob)
        ).status_code == 404

    def test_latched_after_request_cannot_be_accepted(
        self, client: TestClient, db: Session
    ):
        """送出時還沒列管、接受前才閂上去 —— 接受端必須再擋一次。"""
        alice = make_user(db, username="cls2_alice")
        bob = make_user(db, username="cls2_bob")
        conv = _make_conv(db, alice, level="營業秘密")

        created = client.post(
            "/api/handoffs",
            headers=_bearer(alice),
            json={"conversation_id": conv.id, "to_user_id": bob.id},
        )
        assert created.status_code == 201, created.text
        handoff_id = created.json()["id"]

        _set_level(db, conv, "機密")

        resp = client.post(
            f"/api/handoffs/{handoff_id}/accept", headers=_bearer(bob)
        )
        assert resp.status_code == 403, resp.text

        db.expire_all()
        assert db.query(Conversation).filter(
            Conversation.id == conv.id
        ).one().user_id == alice.id
        assert db.query(Handoff).filter(
            Handoff.id == handoff_id
        ).one().status == "pending"
        assert client.get(
            f"/api/conversations/{conv.id}", headers=_bearer(bob)
        ).status_code == 404


# ── 收件人必須是真的人(那顆假按鈕的根因)────────────────────────────────────


class TestRecipientMustResolve:
    """Mutant: 拿掉 `transfer.resolve_new_owner(...)` 呼叫 → 兩條都轉紅
    (交給不存在的 id 會變成 201，正是 2026-07-30 拿掉那顆按鈕的原因)。
    """

    def test_unknown_recipient_is_refused(self, client: TestClient, db: Session):
        alice = make_user(db, username="rcp_alice")
        conv = _make_conv(db, alice)
        resp = client.post(
            "/api/handoffs",
            headers=_bearer(alice),
            json={"conversation_id": conv.id, "to_user_id": 987654},
        )
        assert resp.status_code == 404, resp.text
        assert db.query(Handoff).count() == 0

    def test_cannot_hand_to_self(self, client: TestClient, db: Session):
        alice = make_user(db, username="rcp_self")
        conv = _make_conv(db, alice)
        resp = client.post(
            "/api/handoffs",
            headers=_bearer(alice),
            json={"conversation_id": conv.id, "to_user_id": alice.id},
        )
        assert resp.status_code == 400, resp.text
        assert db.query(Handoff).count() == 0
