"""Reserve-then-stream — the assistant row exists before the stream starts.

擋的是這個 bug：使用者送出 Q1，A1 還在串流時按 Enter 送出 Q2。舊流程要等
串流跑完才 append A1，所以整段串流期間 active leaf 都停在 Q1 上，Q2 於是
掛成 Q1 的子節點（user → user），A1 帶著正確的 parent_id=Q1 回來落庫時被
_enforce_explicit_parent_role 以 400 擋下，答案整段消失。

這裡釘住的不變式：先預留 A1 之後 leaf 前進到 A1，Q2 的預設 leaf 解析自然
落在 A1 底下，樹是 Q1 → A1 → Q2 → A2，而且不需要放寬任何既有不變式。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from app.services import conversation_service as svc
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _auth(client: TestClient, db: Session, username: str = "reserve_user"
          ) -> tuple[User, dict]:
    user = make_user(db, username=username)
    return user, {"Authorization": f"Bearer {login(client, username=username)}"}


def _create_conv(client: TestClient, headers: dict) -> dict:
    resp = client.post("/api/conversations", json={"title": "reserve-test"},
                       headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _append(client, headers, cid, role, content, **extra) -> dict:
    body = {"role": role, "content": content, **extra}
    resp = client.post(f"/api/conversations/{cid}/messages", json=body,
                       headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _reserve(client, headers, cid, parent_id, writer="w-token-abcdef", **extra):
    return client.post(
        f"/api/conversations/{cid}/messages/{parent_id}/reserve-reply",
        json={"stream_writer": writer, **extra},
        headers=headers,
    )


def _tree(client, headers, cid) -> list[tuple[int, str, int | None]]:
    """(id, role, parent_id) over every message in the conversation."""
    resp = client.get(f"/api/conversations/{cid}?view=all", headers=headers)
    assert resp.status_code == 200, resp.text
    return [(m["id"], m["role"], m["parent_id"]) for m in resp.json()["messages"]]


# ── the original sequence ────────────────────────────────────────────────────

def test_midstream_second_send_lands_under_reserved_reply(client, db):
    """Q1 → A1(reserved) → Q2 → A2 — the exact sequence that used to 400."""
    _user, headers = _auth(client, db)
    conv = _create_conv(client, headers)
    cid = conv["id"]

    q1 = _append(client, headers, cid, "user", "第一個問題")
    reserved = _reserve(client, headers, cid, q1["id"])
    assert reserved.status_code == 201, reserved.text
    a1 = reserved.json()
    assert a1["role"] == "assistant"
    assert a1["content"] == ""
    assert a1["parent_id"] == q1["id"]

    # 串流還沒結束，使用者就送出 Q2（不帶 parent_id，走預設 leaf 解析）。
    q2 = _append(client, headers, cid, "user", "插話的第二個問題")
    assert q2["parent_id"] == a1["id"], "Q2 必須掛在預留的 A1 底下，不是 Q1"

    a2 = _reserve(client, headers, cid, q2["id"], writer="w-token-second")
    assert a2.status_code == 201, a2.text

    # A1 的內容事後以 PUT 寫回，不改變樹的形狀。
    put = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={
            "content": "第一個答案",
            "stream_writer": "w-token-abcdef",
            "metadata": {"anila_stream": {"state": "complete"}},
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert put.json()["content"] == "第一個答案"

    assert _tree(client, headers, cid) == [
        (q1["id"], "user", None),
        (a1["id"], "assistant", q1["id"]),
        (q2["id"], "user", a1["id"]),
        (a2.json()["id"], "assistant", q2["id"]),
    ]


def test_reserving_advances_the_active_leaf(client, db):
    """The whole point: the leaf must not sit on the user message while streaming."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")

    before = client.get(f"/api/conversations/{cid}", headers=headers).json()
    assert before["active_leaf_message_id"] == q1["id"]

    a1 = _reserve(client, headers, cid, q1["id"]).json()
    after = client.get(f"/api/conversations/{cid}", headers=headers).json()
    assert after["active_leaf_message_id"] == a1["id"]


def test_sibling_role_invariant_is_untouched(client, db):
    """The guard that caught the bug still fires — we did not relax it."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    _append(client, headers, cid, "assistant", "答案", parent_id=q1["id"])

    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "角色不同的同層兄弟", "parent_id": q1["id"]},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "角色" in resp.json()["detail"]


# ── reservation is linear-only, never a fork ─────────────────────────────────

def test_reserve_rejects_a_parent_that_already_has_a_reply(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    assert _reserve(client, headers, cid, q1["id"]).status_code == 201

    second = _reserve(client, headers, cid, q1["id"], writer="w-token-other")
    assert second.status_code == 409
    assert "重複預留" in second.json()["detail"]


def test_reserve_rejects_an_assistant_parent(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _reserve(client, headers, cid, q1["id"]).json()

    resp = _reserve(client, headers, cid, a1["id"], writer="w-token-third")
    assert resp.status_code == 400
    assert "使用者訊息" in resp.json()["detail"]


def test_reserve_rejects_a_foreign_conversations_message(client, db):
    _user, headers = _auth(client, db)
    cid_a = _create_conv(client, headers)["id"]
    cid_b = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid_a, "user", "問題")

    assert _reserve(client, headers, cid_b, q1["id"]).status_code == 404


def test_reserve_requires_a_writer_token(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")

    resp = client.post(
        f"/api/conversations/{cid}/messages/{q1['id']}/reserve-reply",
        json={"stream_writer": ""},
        headers=headers,
    )
    assert resp.status_code == 422


# ── writer ownership on the update path ──────────────────────────────────────

def test_second_writer_cannot_overwrite_a_reserved_row(client, db):
    """Two tabs: whoever did not reserve the row is refused, loudly (409)."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _reserve(client, headers, cid, q1["id"], writer="w-owner-token").json()

    resp = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={"content": "別人寫的內容", "stream_writer": "w-intruder-token"},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "產生中" in resp.json()["detail"]

    # 原本的列沒有被動到。
    still = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    assert [m for m in still["messages"] if m["id"] == a1["id"]][0]["content"] == ""


def test_update_without_a_token_cannot_touch_a_reserved_row(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _reserve(client, headers, cid, q1["id"], writer="w-owner-token").json()

    resp = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={"content": "沒帶權杖"},
        headers=headers,
    )
    assert resp.status_code == 409


def test_owner_token_writes_and_terminal_state_releases_the_row(client, db):
    """Once terminal the token is spent — ordinary patches work again."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _reserve(client, headers, cid, q1["id"], writer="w-owner-token").json()

    ok = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={
            "content": "完整答案",
            "stream_writer": "w-owner-token",
            "metadata": {"anila_stream": {"state": "complete"}},
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    # writer 權杖在終局時被清掉，不會留在紀錄裡。
    assert "writer" not in ok.json()["metadata"]["anila_stream"]

    # Continue Response / ANILALM finalize 這類既有 patch 不受影響。
    again = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={"content": "完整答案（續寫）"},
        headers=headers,
    )
    assert again.status_code == 200, again.text


def test_update_rejects_an_unknown_stream_state(client, db):
    """A state nobody understands is how a half answer gets to look complete."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _reserve(client, headers, cid, q1["id"], writer="w-owner-token").json()

    resp = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={
            "content": "半截",
            "stream_writer": "w-owner-token",
            "metadata": {"anila_stream": {"state": "definitely-fine"}},
        },
        headers=headers,
    )
    assert resp.status_code == 400
    assert "串流狀態" in resp.json()["detail"]


@pytest.mark.parametrize("state", ["stopped", "failed", "interrupted"])
def test_incomplete_terminal_states_are_recorded_verbatim(client, db, state):
    """Stop / error / client-death keep the partial text AND say it is partial."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _reserve(client, headers, cid, q1["id"], writer="w-owner-token").json()

    resp = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={
            "content": "只講到一半",
            "stream_writer": "w-owner-token",
            "metadata": {"anila_stream": {"state": state}},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "只講到一半"
    assert body["metadata"]["anila_stream"]["state"] == state
    assert state in svc.STREAM_INCOMPLETE_STATES


# ── service-level helpers ────────────────────────────────────────────────────

def test_active_stream_writer_ignores_terminal_rows():
    live = {"anila_stream": {"state": "streaming", "writer": "tok"}}
    assert svc._active_stream_writer(live) == "tok"
    for state in svc.STREAM_TERMINAL_STATES:
        spent = {"anila_stream": {"state": state, "writer": "tok"}}
        assert svc._active_stream_writer(spent) is None
    assert svc._active_stream_writer(None) is None
    assert svc._active_stream_writer({"other": 1}) is None
