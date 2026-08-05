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


def _turn(client, headers, cid, content, writer="w-turn-abcdef", **extra):
    return client.post(
        f"/api/conversations/{cid}/turn",
        json={"content": content, "stream_writer": writer, **extra},
        headers=headers,
    )


def _tree(client, headers, cid) -> list[tuple[int, str, int | None]]:
    """(id, role, parent_id) over every message in the conversation."""
    resp = client.get(f"/api/conversations/{cid}?view=all", headers=headers)
    assert resp.status_code == 200, resp.text
    return [(m["id"], m["role"], m["parent_id"]) for m in resp.json()["messages"]]


def _unanswered_user_messages(tree) -> list[int]:
    """User messages that can never receive an answer (no child, or a user child)."""
    dead = []
    for mid, role, _parent in tree:
        if role != "user":
            continue
        children = [(c, r) for c, r, p in tree if p == mid]
        if not children or any(r != "assistant" for _c, r in children):
            dead.append(mid)
    return dead


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


# ── 兩個分頁同一瞬間按 Enter ─────────────────────────────────────────────────
#
# 獨立驗證者實測 5 次有 3 次壞掉。API 層 100% 重現，不需要瀏覽器：
#   A: POST /messages(user)              → leaf = U_A
#   B: POST /messages(user)              → 預設 leaf 解析 → 掛在 U_A 底下
#   A: POST /reserve-reply(parent=U_A)   → U_A 已經有子訊息 → 409
# 結果 U_A 這則使用者訊息永遠拿不到答案，而它明明已經在資料庫裡。
#
# ⚠ 這個測試環境是 SQLite，`FOR UPDATE` 在上面是 no-op，而 TestClient 是單
# 執行緒依序送——所以「兩條連線真的同時打進來」在這裡量不到。正因為量不到，
# start_turn 沒有把正確性押在 `_lock_conversation` 上：leaf 的推進走
# compare-and-swap，任何引擎語意都一樣，而且下面 test_start_turn_retries_...
# 直接把 CAS 失敗那條路跑出來。
#
# 實測補充（2026-08-03，真 uvicorn＋真 socket＋SQLite，兩個併發 client）：
# 只靠 `_lock_conversation` 時 5 回合有 3 回合在根部長出兩個分岔；
# 加上 CAS 之後 10 回合全部線性、單一根、沒有落單的使用者訊息。
# PostgreSQL 上的行為仍未實測。

def test_two_round_trip_head_orphans_a_user_message(client, db):
    """The bug, as an API-layer control: append+append+reserve kills a question."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]

    # A 送出，使用者訊息落庫（leaf 停在它身上）。
    u_a = _append(client, headers, cid, "user", "A 同時送出")
    # B 在 A 預留之前送出，走預設 leaf 解析 → 掛在 U_A 底下。
    u_b = _append(client, headers, cid, "user", "B 同時送出")
    assert u_b["parent_id"] == u_a["id"], "這就是窗口：B 掛在 A 的使用者訊息底下"
    # B 預留自己的回覆，成功。
    assert _reserve(client, headers, cid, u_b["id"], writer="w-tab-b-xxxx").status_code == 201
    # A 這時才回頭預留 —— U_A 已經有子訊息了。
    late = _reserve(client, headers, cid, u_a["id"], writer="w-tab-a-xxxx")
    assert late.status_code == 409

    dead = _unanswered_user_messages(_tree(client, headers, cid))
    assert dead == [u_a["id"]], "A 的問題落單了，而且它已經在資料庫裡"


def test_start_turn_closes_the_window_under_the_same_interleaving(client, db):
    """The same two clients, one atomic head each — nothing is left unanswered."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]

    a = _turn(client, headers, cid, "A 同時送出", writer="w-tab-a-xxxx")
    assert a.status_code == 201, a.text
    b = _turn(client, headers, cid, "B 同時送出", writer="w-tab-b-xxxx")
    assert b.status_code == 201, b.text

    # B 的使用者訊息掛在 A 預留好的助理列底下，樹是線性的。
    assert b.json()["user"]["parent_id"] == a.json()["assistant"]["id"]
    assert _unanswered_user_messages(_tree(client, headers, cid)) == []
    assert _tree(client, headers, cid) == [
        (a.json()["user"]["id"], "user", None),
        (a.json()["assistant"]["id"], "assistant", a.json()["user"]["id"]),
        (b.json()["user"]["id"], "user", a.json()["assistant"]["id"]),
        (b.json()["assistant"]["id"], "assistant", b.json()["user"]["id"]),
    ]


def test_start_turn_retries_when_the_leaf_moves_under_it(client, db, monkeypatch):
    """The compare-and-swap, exercised — not just reasoned about.

    ``FOR UPDATE`` is a no-op on SQLite, so a row lock alone cannot be measured
    here (and real concurrent clients on SQLite did fork the tree at the root
    before the CAS went in). This drives the CAS directly: someone advances the
    leaf between the read and the write, so the swap must fail, the whole
    attempt must roll back, and the retry must thread onto the NEW leaf.
    """
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    # 已經有一輪在裡面；它的助理列就是「別人剛推進到的那個 leaf」。
    first = _turn(client, headers, cid, "第一輪", writer="w-first-token").json()
    other_leaf = first["assistant"]["id"]

    # 讓 leaf 在我們讀完之後、寫回去之前被別人動掉，只發生一次。
    real_cap = svc._enforce_sibling_cap
    state = {"fired": False}

    def move_the_leaf(db_, conversation_id, parent_id):
        real_cap(db_, conversation_id, parent_id)
        if state["fired"]:
            return
        state["fired"] = True
        conv = db_.query(svc.Conversation).filter(
            svc.Conversation.id == conversation_id,
        ).one()
        conv.active_leaf_message_id = other_leaf
        db_.commit()

    # 把 leaf 先退回第一輪的使用者訊息，這樣讀到的 expected_leaf 會是舊的。
    from app.models.conversation import Conversation

    conv_row = db.query(Conversation).filter(Conversation.id == cid).one()
    conv_row.active_leaf_message_id = first["user"]["id"]
    db.commit()

    monkeypatch.setattr(svc, "_enforce_sibling_cap", move_the_leaf)
    resp = _turn(client, headers, cid, "第二輪", writer="w-second-token")
    assert resp.status_code == 201, resp.text
    assert state["fired"] is True

    # 重試之後掛在「被別人推進到的那個 leaf」底下，不是我們一開始讀到的舊值。
    assert resp.json()["user"]["parent_id"] == other_leaf
    # 失敗的那一次整個回滾了 —— 沒有留下任何半截的列。
    tree = _tree(client, headers, cid)
    assert len(tree) == 4, tree
    assert _unanswered_user_messages(tree) == []


def test_start_turn_returns_both_rows_and_advances_the_leaf(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]

    resp = _turn(client, headers, cid, "問題")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["role"] == "user"
    assert body["user"]["content"] == "問題"
    assert body["assistant"]["role"] == "assistant"
    assert body["assistant"]["content"] == ""
    assert body["assistant"]["parent_id"] == body["user"]["id"]
    assert body["assistant"]["metadata"]["anila_stream"]["state"] == "reserved"

    conv = client.get(f"/api/conversations/{cid}", headers=headers).json()
    assert conv["active_leaf_message_id"] == body["assistant"]["id"]


def test_start_turn_requires_a_writer_token(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    resp = client.post(
        f"/api/conversations/{cid}/turn",
        json={"content": "問題", "stream_writer": ""},
        headers=headers,
    )
    assert resp.status_code == 422


def test_start_turn_writer_owns_the_reserved_row(client, db):
    """The reserved row is still the writer's alone — no weakening here."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    body = _turn(client, headers, cid, "問題", writer="w-owner-token").json()

    intruder = client.put(
        f"/api/conversations/{cid}/messages/{body['assistant']['id']}",
        json={"content": "別人寫的", "stream_writer": "w-intruder-token"},
        headers=headers,
    )
    assert intruder.status_code == 409


def test_start_turn_writer_can_write_the_row_it_reserved(client, db):
    """M-A：權杖從 request body 到儲存列的那一段接線，本身要有往返測試。

    ⚠ 這是整個設計唯一承重的一行。把 ``api/conversations.py`` 的
    ``writer=body.stream_writer`` 換成任何常數，兩套測試（csp 1822、
    anila-shell 436）原本會全綠，而瀏覽器裡的每一則回答都寫不回去：409
    「這則回覆正由其他來源產生中，無法覆寫」，資料庫留下一列空白的
    reserved。原因是只有「入侵者被擋」被測過，「正主寫得進去」沒有。

    不變式：預留時發出的那個權杖，必須就是後續放行寫入的那一個；
    動到這條接線，一定要有測試變紅。
    """
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    body = _turn(client, headers, cid, "問題", writer="w-roundtrip-token").json()
    reserved_id = body["assistant"]["id"]

    # 伺服器存下來的權杖就是我送出去的那一個（不是常數、不是別的欄位）。
    assert (
        body["assistant"]["metadata"]["anila_stream"]["writer"]
        == "w-roundtrip-token"
    )

    ok = client.put(
        f"/api/conversations/{cid}/messages/{reserved_id}",
        json={
            "content": "串流寫回來的答案",
            "stream_writer": "w-roundtrip-token",
            "metadata": {"anila_stream": {"state": "complete"}},
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["content"] == "串流寫回來的答案"

    # 真的落庫了，不是只有回應好看。
    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = [m for m in stored["messages"] if m["id"] == reserved_id][0]
    assert row["content"] == "串流寫回來的答案"
    assert row["metadata"]["anila_stream"]["state"] == "complete"
    assert "writer" not in row["metadata"]["anila_stream"]


def test_start_turn_on_a_foreign_conversation_is_404(client, db):
    _user, headers = _auth(client, db, username="turn_owner")
    cid = _create_conv(client, headers)["id"]
    _other, other_headers = _auth(client, db, username="turn_stranger")
    assert _turn(client, other_headers, cid, "偷看").status_code == 404


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
    """A state nobody understands is how a half answer gets to look complete.

    ⚠ 斷言必須指名「不合法」那一句。原本只斷言 detail 含「串流狀態」——
    未終局那一條的訊息也含這三個字，所以把未知 state 的 400 整段刪掉，
    這個測試照樣綠（未知 state 會掉進未終局那條路）。而未知 state 一旦
    寫進資料庫，前端的 streamStateNotice 回 null，那一列就渲染得跟完整
    答案一模一樣。
    """
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
    assert resp.json()["detail"] == "串流狀態不合法"

    # 而且沒有任何東西被寫進去 —— 那一列還是原樣。
    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = [m for m in stored["messages"] if m["id"] == a1["id"]][0]
    assert row["content"] == ""
    assert row["metadata"]["anila_stream"]["state"] == "reserved"


def test_a_finished_answer_cannot_be_downgraded(client, db):
    """終局→終局原本完全沒有閘門（驗證者 2026-08-05 實測 200）。

    情境不是攻擊，是前端一個 bug 就會發生：串流結束後忘了把那一列從
    in-flight 清單移除，關分頁時 pagehide 就替**每一列**送出
    ``interrupted``——使用者親眼看完的兩則答案，重整後變成「沒有產生完成」。
    終局後 writer 權杖已經清掉，所以那個 PUT 連權杖都不用帶。

    不變式：已經結束的那一列，狀態不得再被改成別的狀態。
    """
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    body = _turn(client, headers, cid, "問題", writer="w-done-token").json()
    mid = body["assistant"]["id"]
    done = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={
            "content": "完整的答案",
            "stream_writer": "w-done-token",
            "metadata": {"anila_stream": {"state": "complete"}},
        },
        headers=headers,
    )
    assert done.status_code == 200, done.text

    for state in ("interrupted", "stopped", "failed"):
        resp = client.put(
            f"/api/conversations/{cid}/messages/{mid}",
            json={"metadata": {"anila_stream": {"state": state}}},
            headers=headers,
        )
        assert resp.status_code == 409, (state, resp.text)
        assert "已經結束" in resp.json()["detail"]

    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = [m for m in stored["messages"] if m["id"] == mid][0]
    assert row["metadata"]["anila_stream"]["state"] == "complete"
    assert row["content"] == "完整的答案"

    # 同一個狀態重送是冪等的，不是衝突 —— 重試不該變成錯誤。
    again = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"metadata": {"anila_stream": {"state": "complete"}}},
        headers=headers,
    )
    assert again.status_code == 200, again.text


def test_a_later_metadata_patch_keeps_the_partial_marker(client, db):
    """metadata 是整包取代 —— 半截標記不能被一次無關的 patch 抹掉。

    ``update_message_content`` 直接 ``msg.metadata_ = metadata``：先把一則
    答案 finalize 成 ``stopped``，再 PUT 一包只有 citations 的 metadata，
    ``anila_stream`` 就整個不見了，一則被停掉的半截答案從此看起來完整。
    """
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    body = _turn(client, headers, cid, "問題", writer="w-stop-token").json()
    mid = body["assistant"]["id"]
    stopped = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={
            "content": "只講到一半",
            "stream_writer": "w-stop-token",
            "metadata": {"anila_stream": {"state": "stopped"}},
        },
        headers=headers,
    )
    assert stopped.status_code == 200, stopped.text

    later = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"metadata": {"citations": [{"title": "來源"}]}},
        headers=headers,
    )
    assert later.status_code == 200, later.text
    assert later.json()["metadata"]["citations"] == [{"title": "來源"}]
    assert later.json()["metadata"]["anila_stream"]["state"] == "stopped"

    stored = client.get(f"/api/conversations/{cid}?view=all", headers=headers).json()
    row = [m for m in stored["messages"] if m["id"] == mid][0]
    assert row["metadata"]["anila_stream"]["state"] == "stopped"


def test_a_metadata_patch_keeps_a_live_reservation(client, db):
    """未終局的列被 patch 掉 envelope，等於任何人都能接手寫它。"""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    body = _turn(client, headers, cid, "問題", writer="w-live-token").json()
    mid = body["assistant"]["id"]

    patched = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"stream_writer": "w-live-token", "metadata": {"citations": []}},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["metadata"]["anila_stream"]["state"] == "reserved"

    intruder = client.put(
        f"/api/conversations/{cid}/messages/{mid}",
        json={"content": "別人寫的"},
        headers=headers,
    )
    assert intruder.status_code == 409


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


# ── ANILALM：不支援分支的規則，實際評估過而不只是「沒碰到」 ──────────────────
#
# reserve-reply 在 origin='anilalm' 的對話上回 201。這不是漏網：預留只可能
# 是線性接續（parent 必須是一則還沒有任何子訊息的 user 訊息），所以它建不出
# 分支；而線性 append 本來就對 ANILALM 開放（_require_branchable 只在明確給
# parent_id 時才跑）。下面三條把這個結論釘住——真的會建分支的那條路仍然 409。

def _anilalm_conv(client, headers, db) -> int:
    from app.models.conversation import Conversation

    cid = _create_conv(client, headers)["id"]
    conv = db.query(Conversation).filter(Conversation.id == cid).one()
    conv.origin = "anilalm"
    db.commit()
    return cid


def test_reserve_on_an_anilalm_conversation_creates_no_branch(client, db):
    _user, headers = _auth(client, db)
    cid = _anilalm_conv(client, headers, db)
    q1 = _append(client, headers, cid, "user", "問題")

    resp = _reserve(client, headers, cid, q1["id"])
    assert resp.status_code == 201, resp.text
    # 線性：那則使用者訊息底下只有這一列，沒有第二個變體。
    assert resp.json()["sibling_count"] == 1
    assert [r for _i, r, _p in _tree(client, headers, cid)] == ["user", "assistant"]


def test_start_turn_on_an_anilalm_conversation_creates_no_branch(client, db):
    _user, headers = _auth(client, db)
    cid = _anilalm_conv(client, headers, db)

    resp = _turn(client, headers, cid, "問題")
    assert resp.status_code == 201, resp.text
    assert resp.json()["assistant"]["sibling_count"] == 1
    assert [r for _i, r, _p in _tree(client, headers, cid)] == ["user", "assistant"]


def test_anilalm_branching_is_still_refused_on_the_same_row(client, db):
    """The no-branching rule itself is untouched — /branch still 409s."""
    _user, headers = _auth(client, db)
    cid = _anilalm_conv(client, headers, db)
    body = _turn(client, headers, cid, "問題").json()

    resp = client.post(
        f"/api/conversations/{cid}/messages/{body['user']['id']}/branch",
        json={"role": "assistant", "content": "第二個變體"},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "分支" in resp.json()["detail"]


# ── 自鎖：不能替任意一列裝上只有自己知道的權杖 ───────────────────────────────

def test_patch_cannot_install_a_live_writer_token(client, db):
    """Owner self-lock: PUTting a non-terminal envelope used to 200 and then
    409 every later patch forever. Only the reserve path may open a row."""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    q1 = _append(client, headers, cid, "user", "問題")
    a1 = _append(client, headers, cid, "assistant", "答案", parent_id=q1["id"])

    for state in ("streaming", "reserved"):
        resp = client.put(
            f"/api/conversations/{cid}/messages/{a1['id']}",
            json={"metadata": {"anila_stream": {"state": state, "writer": "mine"}}},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        assert "串流狀態" in resp.json()["detail"]

    # 那一列仍然可以正常 patch —— 沒有被鎖死。
    after = client.put(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        json={"content": "改過的答案"},
        headers=headers,
    )
    assert after.status_code == 200, after.text


# ── service-level helpers ────────────────────────────────────────────────────

def test_active_stream_writer_ignores_terminal_rows():
    live = {"anila_stream": {"state": "streaming", "writer": "tok"}}
    assert svc._active_stream_writer(live) == "tok"
    for state in svc.STREAM_TERMINAL_STATES:
        spent = {"anila_stream": {"state": state, "writer": "tok"}}
        assert svc._active_stream_writer(spent) is None
    assert svc._active_stream_writer(None) is None
    assert svc._active_stream_writer({"other": 1}) is None


# ── 編輯重問：同一個保證，另一條路徑 ─────────────────────────────────────────
#
# 驗證者 2026-08-05 在瀏覽器裡實測到的事故：編輯重問先 branch 出新的使用者
# 訊息（leaf 落在一則 **user** 上），才開始串流。串流途中打一句送出，新的
# 使用者訊息就掛到編輯後的問題底下（user → user）；編輯後那題的答案接著被
# 400「分支訊息的角色必須與既有子訊息相同」擋掉，重整之後就沒了 —— 而它一
# 秒鐘前還完整地顯示在螢幕上。
#
# 觀測到的樹：#2530 user 'ER2 edited question' → #2531 user 'ER3 followup'
#             → #2532 assistant
#
# 不變式：任何會產生一輪問答的路徑，都不得在串流期間把 active leaf 留在
# 使用者訊息上。

def _branch_turn(client, headers, cid, message_id, content,
                 writer="w-branch-abcdef", **extra):
    return client.post(
        f"/api/conversations/{cid}/messages/{message_id}/branch-turn",
        json={"content": content, "stream_writer": writer, **extra},
        headers=headers,
    )


def test_branch_turn_reserves_the_answer_and_moves_the_leaf_off_the_user_row(
    client, db,
):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    first = _turn(client, headers, cid, "原本的問題", writer="w-first-token").json()

    resp = _branch_turn(client, headers, cid, first["user"]["id"], "編輯後的問題")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["user"]["role"] == "user"
    assert body["user"]["content"] == "編輯後的問題"
    # 同層兄弟：parent 與被編輯的那一則相同。
    assert body["user"]["parent_id"] == first["user"]["parent_id"]
    assert body["assistant"]["role"] == "assistant"
    assert body["assistant"]["content"] == ""
    assert body["assistant"]["parent_id"] == body["user"]["id"]
    assert body["assistant"]["metadata"]["anila_stream"]["state"] == "reserved"

    conv = client.get(f"/api/conversations/{cid}", headers=headers).json()
    # leaf 落在助理列上 —— 不是使用者訊息。這一行就是整條路徑的重點。
    assert conv["active_leaf_message_id"] == body["assistant"]["id"]


def test_edit_and_reask_survives_a_midstream_follow_up(client, db):
    """完整重演事故序列，斷言沒有任何答案落單。"""
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    first = _turn(client, headers, cid, "ER1 原問題", writer="w-er1-token").json()
    client.put(
        f"/api/conversations/{cid}/messages/{first['assistant']['id']}",
        json={
            "content": "原問題的答案",
            "stream_writer": "w-er1-token",
            "metadata": {"anila_stream": {"state": "complete"}},
        },
        headers=headers,
    )

    # 編輯重問：使用者訊息分支 ＋ 助理列預留，一個交易。
    edited = _branch_turn(
        client, headers, cid, first["user"]["id"], "ER2 編輯後的問題",
        writer="w-er2-token",
    ).json()

    # 串流還在跑，使用者插話（不帶 parent_id，走預設 leaf 解析）。
    follow_up = _turn(client, headers, cid, "ER3 插話", writer="w-er3-token").json()
    assert follow_up["user"]["parent_id"] == edited["assistant"]["id"], (
        "插話必須掛在預留的助理列底下，不是掛在編輯後的問題底下"
    )

    # 編輯後那題的答案現在寫得回去 —— 這正是原本 400 消失的那一則。
    put = client.put(
        f"/api/conversations/{cid}/messages/{edited['assistant']['id']}",
        json={
            "content": "編輯後那題的答案",
            "stream_writer": "w-er2-token",
            "metadata": {"anila_stream": {"state": "complete"}},
        },
        headers=headers,
    )
    assert put.status_code == 200, put.text
    assert put.json()["content"] == "編輯後那題的答案"

    tree = _tree(client, headers, cid)
    assert _unanswered_user_messages(tree) == []
    # 沒有 user → user 這種邊。
    roles = {mid: role for mid, role, _p in tree}
    for _mid, role, parent in tree:
        if role == "user" and parent is not None:
            assert roles[parent] != "user"


def test_branch_turn_refuses_an_assistant_target(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    first = _turn(client, headers, cid, "問題", writer="w-a-token-x").json()
    resp = _branch_turn(client, headers, cid, first["assistant"]["id"], "亂分支")
    assert resp.status_code == 400
    assert "使用者訊息" in resp.json()["detail"]


def test_branch_turn_requires_a_writer_token(client, db):
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    first = _turn(client, headers, cid, "問題", writer="w-a-token-x").json()
    resp = client.post(
        f"/api/conversations/{cid}/messages/{first['user']['id']}/branch-turn",
        json={"content": "編輯後", "stream_writer": ""},
        headers=headers,
    )
    assert resp.status_code == 422


def test_branch_turn_on_a_foreign_conversation_is_404(client, db):
    _user, headers = _auth(client, db, username="bt_owner")
    cid = _create_conv(client, headers)["id"]
    first = _turn(client, headers, cid, "問題", writer="w-a-token-x").json()
    _other, other_headers = _auth(client, db, username="bt_stranger")
    resp = _branch_turn(client, other_headers, cid, first["user"]["id"], "偷改")
    assert resp.status_code == 404


def test_branch_turn_is_still_refused_on_an_anilalm_conversation(client, db):
    """ANILALM 不支援分支 —— 新端點不得是那條規則的後門。"""
    _user, headers = _auth(client, db)
    cid = _anilalm_conv(client, headers, db)
    first = _turn(client, headers, cid, "問題", writer="w-a-token-x").json()
    resp = _branch_turn(client, headers, cid, first["user"]["id"], "編輯後")
    # _require_branchable 的既有回應碼是 409，與 POST /branch 一致。
    assert resp.status_code == 409, resp.text


# ── 卡住的預留列：使用者的出口 ───────────────────────────────────────────────

def test_a_stuck_reserved_row_can_still_be_answered_by_branching(client, db):
    """孤兒 reserved 列寫不進去（409），但重新產生走得通。

    這是 UI 上「重新產生」那顆按鈕背後的伺服器行為：在同一則使用者訊息底下
    長出一列新的助理訊息，完全不去動卡住的那一列。conversation_service.py
    開頭的已知缺陷區塊記的就是這條出口。
    """
    _user, headers = _auth(client, db)
    cid = _create_conv(client, headers)["id"]
    body = _turn(client, headers, cid, "問題", writer="w-dead-tab").json()
    stuck_id = body["assistant"]["id"]

    # 分頁被殺掉，權杖跟著消失：這一列從此寫不進去。
    blocked = client.put(
        f"/api/conversations/{cid}/messages/{stuck_id}",
        json={"content": "接手寫"},
        headers=headers,
    )
    assert blocked.status_code == 409

    # 出口：branch 一列新的助理訊息（同層角色一致）。
    fresh = client.post(
        f"/api/conversations/{cid}/messages/{stuck_id}/branch",
        json={"role": "assistant", "content": "重新產生的答案"},
        headers=headers,
    )
    assert fresh.status_code == 201, fresh.text
    assert fresh.json()["parent_id"] == body["user"]["id"]

    conv = client.get(f"/api/conversations/{cid}", headers=headers).json()
    assert conv["active_leaf_message_id"] == fresh.json()["id"]
    # active path 走新的那一列，空白的那一列留在樹上當兄弟。
    assert [m["id"] for m in conv["messages"]] == [
        body["user"]["id"], fresh.json()["id"],
    ]

    # 另一條出口確實存在：整棵子樹刪得掉（沒有 writer 閘門）。
    gone = client.delete(
        f"/api/conversations/{cid}/messages/{stuck_id}", headers=headers,
    )
    assert gone.status_code == 200, gone.text
