"""OW-1 — message history tree backend tests.

Pins docs/plans/ow1-message-tree-blueprint.md §5 (20 cases): append chaining,
branch (edit-re-ask + regenerate), view=active|all, active-leaf switch,
sibling cap, subtree delete, ANILALM exclusion, attachments, public share,
search stays all-branches.
"""
from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.attachment import Attachment
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.utils.security import create_access_token
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _bearer(user: User) -> dict:
    token = create_access_token({
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _auth(client: TestClient, db: Session, username: str = "tree_user",
          role: str = "user") -> tuple[User, dict]:
    user = make_user(db, username=username, role=role)
    return user, {"Authorization": f"Bearer {login(client, username=username)}"}


def _create_conv(client: TestClient, headers: dict, **extra) -> dict:
    body = {"title": "tree-test", **extra}
    resp = client.post("/api/conversations", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _append(client: TestClient, headers: dict, cid: int, role: str,
            content: str, **extra) -> dict:
    body = {"role": role, "content": content, **extra}
    resp = client.post(
        f"/api/conversations/{cid}/messages", json=body, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _branch(client: TestClient, headers: dict, cid: int, mid: int,
            role: str, content: str, expect: int = 201):
    resp = client.post(
        f"/api/conversations/{cid}/messages/{mid}/branch",
        json={"role": role, "content": content},
        headers=headers,
    )
    assert resp.status_code == expect, resp.text
    return resp


def _get(client: TestClient, headers: dict, cid: int, view: str = "active"):
    resp = client.get(
        f"/api/conversations/{cid}?view={view}", headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── 1. Linear append chains to active leaf ────────────────────────────────────


def test_01_append_chains_to_active_leaf(client: TestClient, db: Session):
    _, h = _auth(client, db, "t01")
    conv = _create_conv(client, h)
    cid = conv["id"]
    m1 = _append(client, h, cid, "user", "Q1")
    assert m1["parent_id"] is None
    m2 = _append(client, h, cid, "assistant", "A1")
    assert m2["parent_id"] == m1["id"]
    m3 = _append(client, h, cid, "user", "Q2")
    assert m3["parent_id"] == m2["id"]
    detail = _get(client, h, cid, "active")
    assert detail["active_leaf_message_id"] == m3["id"]
    assert [m["id"] for m in detail["messages"]] == [m1["id"], m2["id"], m3["id"]]
    assert len(_get(client, h, cid, "all")["messages"]) == 3


# ── 2. Branch on user message (edit-re-ask) ───────────────────────────────────


def test_02_branch_user_sibling_preserves_original(client: TestClient, db: Session):
    _, h = _auth(client, db, "t02")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    before_all = len(_get(client, h, cid, "all")["messages"])
    q1_prime = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    assert q1_prime["parent_id"] == q1["parent_id"]
    # Original byte-identical
    orig = next(
        m for m in _get(client, h, cid, "all")["messages"] if m["id"] == q1["id"]
    )
    assert orig["content"] == "Q1"
    assert orig["role"] == "user"
    # Old subtree (A1) still present
    assert before_all == 2
    assert len(_get(client, h, cid, "all")["messages"]) == 3
    assert any(m["id"] == a1["id"] for m in _get(client, h, cid, "all")["messages"])


# ── 3. Branch on assistant (regenerate) — PLAN criterion ──────────────────────


def test_03_branch_assistant_siblings(client: TestClient, db: Session):
    _, h = _auth(client, db, "t03")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A1bis").json()
    assert a2["parent_id"] == q1["id"]
    # Reload a1 from view=all for parent_id (append response may predate sibling fields)
    a1_row = next(
        m for m in _get(client, h, cid, "all")["messages"] if m["id"] == a1["id"]
    )
    assert a1_row["parent_id"] == q1["id"]
    all_msgs = _get(client, h, cid, "all")["messages"]
    assistants = [m for m in all_msgs if m["role"] == "assistant"]
    assert len(assistants) == 2
    for a in assistants:
        assert a["sibling_count"] == 2
        assert set(a["sibling_ids"]) == {a1["id"], a2["id"]}


# ── 4. view=active one branch; view=all both ──────────────────────────────────


def test_04_view_active_vs_all(client: TestClient, db: Session):
    _, h = _auth(client, db, "t04")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2").json()
    active = _get(client, h, cid, "active")
    assert [m["id"] for m in active["messages"]] == [q1["id"], a2["id"]]
    all_ids = {m["id"] for m in _get(client, h, cid, "all")["messages"]}
    assert all_ids == {q1["id"], a1["id"], a2["id"]}


# ── 5. active-leaf switch + canonicalization ──────────────────────────────────


def test_05_active_leaf_switch_and_canonicalize(client: TestClient, db: Session):
    _, h = _auth(client, db, "t05")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2").json()
    # Switch to a1 branch
    resp = client.put(
        f"/api/conversations/{cid}/active-leaf",
        json={"message_id": a1["id"]},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    path = resp.json()
    assert path["active_leaf_message_id"] == a1["id"]
    assert [m["id"] for m in path["messages"]] == [q1["id"], a1["id"]]
    # Canonicalize: target=q1 (has children) → newest leaf under q1
    # After switch to a1, newest under q1 is still a2 (created later) if we
    # point at q1 — descend newest-child.
    resp2 = client.put(
        f"/api/conversations/{cid}/active-leaf",
        json={"message_id": q1["id"]},
        headers=h,
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["active_leaf_message_id"] == a2["id"]


# ── 6. foreign-conv message id → 404 ──────────────────────────────────────────


def test_06_active_leaf_foreign_conv_404(client: TestClient, db: Session):
    u1, h1 = _auth(client, db, "t06a")
    u2, h2 = _auth(client, db, "t06b")
    c1 = _create_conv(client, h1)
    c2 = _create_conv(client, h2)
    m2 = _append(client, h2, c2["id"], "user", "other")
    resp = client.put(
        f"/api/conversations/{c1['id']}/active-leaf",
        json={"message_id": m2["id"]},
        headers=h1,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "訊息不存在"


# ── 7. Sibling cap 20 → 21st is 409 ───────────────────────────────────────────


def test_07_sibling_cap(client: TestClient, db: Session, monkeypatch):
    _, h = _auth(client, db, "t07")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A0")
    # a1 is sibling #1 under q1; create 19 more → 20 total; 21st fails.
    last = a1
    for i in range(1, 20):
        last = _branch(
            client, h, cid, a1["id"], "assistant", f"A{i}",
        ).json()
    assert last["sibling_count"] == 20
    resp = _branch(client, h, cid, a1["id"], "assistant", "overflow", expect=409)
    assert "上限（20）" in resp.json()["detail"]


# ── 8. parent_id from another conversation → 400 ──────────────────────────────


def test_08_foreign_parent_id_400(client: TestClient, db: Session):
    _, h1 = _auth(client, db, "t08a")
    _, h2 = _auth(client, db, "t08b")
    c1 = _create_conv(client, h1)
    c2 = _create_conv(client, h2)
    m2 = _append(client, h2, c2["id"], "user", "foreign")
    resp = client.post(
        f"/api/conversations/{c1['id']}/messages",
        json={"role": "user", "content": "x", "parent_id": m2["id"]},
        headers=h1,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "父訊息不屬於此對話"


# ── 9. Sibling ordering stable (created_at, id) ───────────────────────────────


def test_09_sibling_ordering_stable(client: TestClient, db: Session):
    _, h = _auth(client, db, "t09")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    ids = []
    a0 = _append(client, h, cid, "assistant", "A0")
    ids.append(a0["id"])
    for i in range(1, 4):
        ids.append(
            _branch(client, h, cid, a0["id"], "assistant", f"A{i}").json()["id"]
        )
    all_msgs = _get(client, h, cid, "all")["messages"]
    assistants = [m for m in all_msgs if m["role"] == "assistant"]
    assert [m["id"] for m in assistants] == ids
    assert assistants[0]["sibling_ids"] == ids
    assert [m["sibling_index"] for m in assistants] == [0, 1, 2, 3]


# ── 10. Subtree delete; sibling intact ────────────────────────────────────────


def test_10_subtree_delete_keeps_sibling(client: TestClient, db: Session):
    _, h = _auth(client, db, "t10")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    # Continue on a1
    q2 = _append(client, h, cid, "user", "Q2")
    # Branch a2 sibling of a1, then continue there (active)
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2").json()
    # Delete a2 (leaf) — a1+q2 subtree intact? Wait: q2 is child of a1.
    # Structure: q1 → a1 → q2; and q1 → a2. Delete a2.
    resp = client.delete(
        f"/api/conversations/{cid}/messages/{a2['id']}", headers=h,
    )
    assert resp.status_code == 200, resp.text
    all_ids = {m["id"] for m in _get(client, h, cid, "all")["messages"]}
    assert a2["id"] not in all_ids
    assert {q1["id"], a1["id"], q2["id"]}.issubset(all_ids)


# ── 11. Deleting active-leaf subtree repoints ─────────────────────────────────


def test_11_delete_active_repoints(client: TestClient, db: Session):
    _, h = _auth(client, db, "t11")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2").json()
    assert _get(client, h, cid)["active_leaf_message_id"] == a2["id"]
    resp = client.delete(
        f"/api/conversations/{cid}/messages/{a2['id']}", headers=h,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["active_leaf_message_id"] is not None
    assert data["active_leaf_message_id"] != a2["id"]
    # Should repoint to a1 (parent=q1, newest surviving under q1)
    assert data["active_leaf_message_id"] == a1["id"]
    leaf = db.get(Conversation, cid).active_leaf_message_id
    assert leaf == a1["id"]
    assert db.get(Message, leaf) is not None


# ── 12. Sole root delete 409; one of two roots 200 ────────────────────────────


def test_12_sole_root_guard(client: TestClient, db: Session):
    _, h = _auth(client, db, "t12")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "only")
    resp = client.delete(
        f"/api/conversations/{cid}/messages/{q1['id']}", headers=h,
    )
    assert resp.status_code == 409
    assert "至少需保留一則訊息" in resp.json()["detail"]

    # Two roots: plant a second root via explicit parent_id after nulling leaf
    # trick — append with parent_id pointing nowhere creates under active leaf.
    # Create sibling root by branching q1? Branch keeps same parent (None) →
    # two roots.
    q2 = _branch(client, h, cid, q1["id"], "user", "second-root").json()
    assert q2["parent_id"] is None
    resp2 = client.delete(
        f"/api/conversations/{cid}/messages/{q2['id']}", headers=h,
    )
    assert resp2.status_code == 200, resp2.text
    assert len(_get(client, h, cid, "all")["messages"]) == 1


# ── 13. delete_conversation on branched conv ──────────────────────────────────


def test_13_delete_conversation_branched(client: TestClient, db: Session):
    user, h = _auth(client, db, "t13")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    _branch(client, h, cid, a1["id"], "assistant", "A2")
    att = Attachment(
        conversation_id=cid,
        message_id=a1["id"],
        uploaded_by=user.id,
        filename="x.pdf",
        content_type="application/pdf",
        size_bytes=10,
        storage_path="test/x.pdf",
    )
    db.add(att)
    db.commit()
    att_id = att.id

    resp = client.delete(f"/api/conversations/{cid}", headers=h)
    assert resp.status_code == 204, resp.text
    db.expire_all()
    assert db.get(Conversation, cid) is None
    assert db.query(Message).filter(Message.conversation_id == cid).count() == 0
    # Attachment CASCADE on conversation_id — Python bulk-delete (SQLite-safe)
    assert db.get(Attachment, att_id) is None


# ── 14. Attachment survives branch delete ─────────────────────────────────────


def test_14_attachment_survives_branch_delete(client: TestClient, db: Session):
    user, h = _auth(client, db, "t14")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2").json()
    att = Attachment(
        conversation_id=cid,
        message_id=a2["id"],
        uploaded_by=user.id,
        filename="keep.pdf",
        content_type="application/pdf",
        size_bytes=42,
        storage_path="test/keep.pdf",
    )
    db.add(att)
    db.commit()
    att_id = att.id

    resp = client.delete(
        f"/api/conversations/{cid}/messages/{a2['id']}", headers=h,
    )
    assert resp.status_code == 200, resp.text
    db.expire_all()
    att2 = db.get(Attachment, att_id)
    assert att2 is not None
    assert att2.message_id is None
    assert att2.conversation_id == cid


# ── 15. ANILALM exclusion ─────────────────────────────────────────────────────


def test_15_anilalm_branch_blocked_plain_append_ok(client: TestClient, db: Session):
    from app.models.ingestion import IngestionCollection

    user, h = _auth(client, db, "t15")
    coll = IngestionCollection(
        name="t15-kb",
        created_by=user.id,
        embedding_model="nv-embed",
        embedding_dim=4000,
        chunking_config={},
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    conv = _create_conv(
        client, h, origin="anilalm", collection_id=coll.id, title="lm",
    )
    cid = conv["id"]
    m1 = _append(client, h, cid, "user", "plain")
    assert m1["id"]
    # branch blocked
    resp = _branch(client, h, cid, m1["id"], "user", "fork", expect=409)
    assert resp.json()["detail"] == "ANILALM 對話不支援訊息分支"
    # active-leaf blocked
    r2 = client.put(
        f"/api/conversations/{cid}/active-leaf",
        json={"message_id": m1["id"]},
        headers=h,
    )
    assert r2.status_code == 409
    # delete blocked
    r3 = client.delete(
        f"/api/conversations/{cid}/messages/{m1['id']}", headers=h,
    )
    assert r3.status_code == 409
    # explicit parent_id on POST blocked
    r4 = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": "x", "parent_id": m1["id"]},
        headers=h,
    )
    assert r4.status_code == 409


# ── 16. Foreign conv 403; admin-tier allowed ──────────────────────────────────


def test_16_foreign_403_admin_allowed(client: TestClient, db: Session):
    owner, h_owner = _auth(client, db, "t16owner")
    other, h_other = _auth(client, db, "t16other")
    admin, h_admin = _auth(client, db, "t16admin", role="admin")
    conv = _create_conv(client, h_owner)
    cid = conv["id"]
    m = _append(client, h_owner, cid, "user", "Q")

    for method, path, body in [
        ("post", f"/api/conversations/{cid}/messages/{m['id']}/branch",
         {"role": "user", "content": "x"}),
        ("put", f"/api/conversations/{cid}/active-leaf",
         {"message_id": m["id"]}),
        ("delete", f"/api/conversations/{cid}/messages/{m['id']}", None),
    ]:
        if method == "post":
            r = client.post(path, json=body, headers=h_other)
        elif method == "put":
            r = client.put(path, json=body, headers=h_other)
        else:
            r = client.delete(path, headers=h_other)
        assert r.status_code == 404, (method, path, r.text)
        assert r.json()["detail"] == "找不到此對話"

    # admin can switch
    r_ok = client.put(
        f"/api/conversations/{cid}/active-leaf",
        json={"message_id": m["id"]},
        headers=h_admin,
    )
    assert r_ok.status_code == 200, r_ok.text


# ── 17. NULL-origin legacy conv allows branch ─────────────────────────────────


def test_17_null_origin_allows_branch(client: TestClient, db: Session):
    user, h = _auth(client, db, "t17")
    # Create via API then null out origin to simulate legacy
    conv = _create_conv(client, h)
    row = db.get(Conversation, conv["id"])
    row.origin = None
    db.commit()
    cid = conv["id"]
    m = _append(client, h, cid, "user", "legacy")
    resp = _branch(client, h, cid, m["id"], "user", "forked")
    assert resp.status_code == 201


# ── 18. Classify after branch — reads + message classification untouched ──────


def test_18_classify_after_branch(client: TestClient, db: Session):
    _, h = _auth(client, db, "t18")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2").json()
    # Switch to A1 branch
    client.put(
        f"/api/conversations/{cid}/active-leaf",
        json={"message_id": a1["id"]},
        headers=h,
    )
    r = client.post(f"/api/conversations/{cid}/classify", headers=h)
    assert r.status_code == 200, r.text
    # Branch A (active=a1) still readable
    detail = _get(client, h, cid, "active")
    assert detail["classification_level"] == "密"
    assert [m["id"] for m in detail["messages"]] == [q1["id"], a1["id"]]
    # Message-level classification untouched (still default 無機密)
    for mid in (q1["id"], a1["id"], a2["id"]):
        msg = db.get(Message, mid)
        assert msg.classification_level == "無機密"


# ── 19. Named share recipient sees active path; 密 blocks create ─────────────


def test_19_named_share_active_path(client: TestClient, db: Session):
    owner, h = _auth(client, db, "t19")
    peer = make_user(db, username="t19_peer")
    peer_h = _bearer(peer)
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "A1-secret-branch")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "A2-active").json()
    share_resp = client.post(
        f"/api/conversations/{cid}/shares",
        json={"mode": "read_only", "target_username": peer.username},
        headers=h,
    )
    assert share_resp.status_code == 201, share_resp.text
    assert share_resp.json().get("token") is None

    detail = client.get(f"/api/conversations/{cid}?view=active", headers=peer_h)
    assert detail.status_code == 200, detail.text
    bodies = [m["content"] for m in detail.json()["messages"]]
    assert bodies == ["Q1", "A2-active"]
    assert "A1-secret-branch" not in bodies

    # Classified (≥密) → further share create refused (outbound gate).
    client.post(f"/api/conversations/{cid}/classify", headers=h)
    share2 = client.post(
        f"/api/conversations/{cid}/shares",
        json={"mode": "read_only", "target_username": "someone-else"},
        headers=h,
    )
    assert share2.status_code == 403
    assert "密" in share2.json()["detail"]


# ── 20. /search still matches non-active-branch content ───────────────────────


def test_20_search_matches_inactive_branch(client: TestClient, db: Session):
    _, h = _auth(client, db, "t20")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    a1 = _append(client, h, cid, "assistant", "uniqueNeedleInactiveBranch")
    a2 = _branch(client, h, cid, a1["id"], "assistant", "active-side").json()
    # active = a2; needle only on a1
    assert _get(client, h, cid)["active_leaf_message_id"] == a2["id"]
    resp = client.get(
        "/api/conversations/search?q=uniqueNeedleInactiveBranch",
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    hits = [x for x in resp.json() if x["id"] == cid]
    assert len(hits) == 1
    assert hits[0]["snippet"] is not None
    assert "uniqueNeedleInactiveBranch" in hits[0]["snippet"]


# ── Extra: role mismatch on branch → 400 ──────────────────────────────────────


def test_branch_role_mismatch_400(client: TestClient, db: Session):
    _, h = _auth(client, db, "trole")
    conv = _create_conv(client, h)
    m = _append(client, h, conv["id"], "user", "Q")
    resp = _branch(
        client, h, conv["id"], m["id"], "assistant", "wrong", expect=400,
    )
    assert resp.json()["detail"] == "分支訊息的角色必須與原訊息相同"


# ── Rev: set_active=false first append still sets pointer ─────────────────────


def test_rev_set_active_false_first_append_sets_pointer(
    client: TestClient, db: Session,
):
    _, h = _auth(client, db, "trev_sa")
    conv = _create_conv(client, h)
    cid = conv["id"]
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "first", "set_active": False},
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    detail = _get(client, h, cid, "active")
    assert detail["active_leaf_message_id"] == mid
    assert [m["id"] for m in detail["messages"]] == [mid]
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id == mid


# ── Rev: NULL pointer with chained rows → flat fallback + heal ────────────────


def test_rev_null_pointer_fallback_and_heal(
    client: TestClient, db: Session,
):
    owner, h = _auth(client, db, "trev_null")
    peer = make_user(db, username="trev_null_peer")
    peer_h = _bearer(peer)
    conv = _create_conv(client, h)
    cid = conv["id"]
    m1 = _append(client, h, cid, "user", "Q1")
    m2 = _append(client, h, cid, "assistant", "A1")
    # Direct assignment: simulate NULL pointer with rows present.
    row = db.get(Conversation, cid)
    row.active_leaf_message_id = None
    db.commit()
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id is None

    detail = _get(client, h, cid, "active")
    assert [m["id"] for m in detail["messages"]] == [m1["id"], m2["id"]]
    assert detail["active_leaf_message_id"] == m2["id"]
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id == m2["id"]

    # Reset pointer again and confirm named-share recipient GET also heals.
    row = db.get(Conversation, cid)
    row.active_leaf_message_id = None
    db.commit()
    share_resp = client.post(
        f"/api/conversations/{cid}/shares",
        json={"mode": "read_only", "target_username": peer.username},
        headers=h,
    )
    assert share_resp.status_code == 201, share_resp.text
    row = db.get(Conversation, cid)
    row.active_leaf_message_id = None
    db.commit()

    shared = client.get(f"/api/conversations/{cid}?view=active", headers=peer_h)
    assert shared.status_code == 200, shared.text
    bodies = [m["content"] for m in shared.json()["messages"]]
    assert bodies == ["Q1", "A1"]
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id == m2["id"]


# ── Rev: un-backfilled flat rows (all parent_id NULL) — no heal write ─────────


def test_rev_unbackfilled_flat_rows_no_heal_persist(
    client: TestClient, db: Session,
):
    """create_all+stamp / pre-r1_0012 restore: all parent_id NULL, pointer NULL.

    Fallback must return every row, must NOT persist a leaf pointer (would
    truncate subsequent reads to one message), and a second read must still
    return the full flat list.
    """
    user, h = _auth(client, db, "trev_flat")
    conv = _create_conv(client, h)
    cid = conv["id"]
    # Plant rows with parent_id=NULL directly — not via _append (which chains).
    r1 = Message(conversation_id=cid, parent_id=None, role="user", content="Q1")
    r2 = Message(conversation_id=cid, parent_id=None, role="assistant", content="A1")
    r3 = Message(conversation_id=cid, parent_id=None, role="user", content="Q2")
    db.add_all([r1, r2, r3])
    row = db.get(Conversation, cid)
    row.active_leaf_message_id = None
    db.commit()
    db.refresh(r1)
    db.refresh(r2)
    db.refresh(r3)
    expected_ids = [r1.id, r2.id, r3.id]
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id is None

    detail = _get(client, h, cid, "active")
    assert [m["id"] for m in detail["messages"]] == expected_ids
    # Response may expose NULL (no heal) — never a one-message truncation.
    assert detail["active_leaf_message_id"] is None
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id is None

    detail2 = _get(client, h, cid, "active")
    assert [m["id"] for m in detail2["messages"]] == expected_ids
    assert detail2["active_leaf_message_id"] is None
    db.expire_all()
    assert db.get(Conversation, cid).active_leaf_message_id is None


# ── Rev: explicit parent role integrity on append ─────────────────────────────


def test_rev_explicit_parent_role_mismatch_400(client: TestClient, db: Session):
    _, h = _auth(client, db, "trev_role")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    _append(client, h, cid, "assistant", "A1")
    # Explicit parent_id under q1 (already has assistant child) with wrong role.
    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "bad", "parent_id": q1["id"]},
        headers=h,
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "分支訊息的角色必須與既有子訊息相同"


# ── Rev: no branching from system/tool ────────────────────────────────────────


def test_rev_branch_system_tool_rejected(client: TestClient, db: Session):
    _, h = _auth(client, db, "trev_sys")
    conv = _create_conv(client, h)
    cid = conv["id"]
    sys_msg = _append(client, h, cid, "system", "sys-note")
    resp = _branch(
        client, h, cid, sys_msg["id"], "system", "fork", expect=400,
    )
    assert resp.json()["detail"] == "不可從 system/tool 訊息建立分支"
    tool_msg = _append(client, h, cid, "tool", "tool-out")
    resp2 = _branch(
        client, h, cid, tool_msg["id"], "tool", "fork2", expect=400,
    )
    assert resp2.json()["detail"] == "不可從 system/tool 訊息建立分支"


# ── Q18: read-side sibling-group attachment chips (no row clone) ───────────────


def _plant_attachment(
    db: Session,
    *,
    user: User,
    conversation_id: int,
    message_id: int | None,
    filename: str = "doc.pdf",
    storage_path: str = "q18/doc.pdf",
    uploaded_by: int | None = None,
    extracted_text: str | None = "pdf body",
    token_count: int = 12,
    extract_status: str = "ok",
) -> Attachment:
    att = Attachment(
        conversation_id=conversation_id,
        message_id=message_id,
        uploaded_by=uploaded_by if uploaded_by is not None else user.id,
        filename=filename,
        content_type="application/pdf",
        size_bytes=100,
        storage_path=storage_path,
        extracted_text=extracted_text,
        token_count=token_count,
        extract_status=extract_status,
        page_count=3,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def _att_refs(msg: dict) -> list[str]:
    return [a["reference_id"] for a in msg.get("attachments", [])]


def test_q18_branch_surfaces_sibling_attachments_one_row(
    client: TestClient, db: Session,
):
    """Upload on A, branch A→A′: A′ shows same chips; attachments table stays 1."""
    user, h = _auth(client, db, "tq18a")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1 with pdf")
    att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
        storage_path="q18/shared.pdf",
    )
    before = db.query(Attachment).filter(Attachment.conversation_id == cid).count()
    assert before == 1

    q1p = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    assert q1p["parent_id"] == q1["parent_id"]
    assert q1p["sibling_count"] == 2
    assert _att_refs(q1p) == [att.reference_id]
    assert q1p["attachments"][0]["filename"] == "doc.pdf"

    db.expire_all()
    assert db.query(Attachment).filter(Attachment.conversation_id == cid).count() == 1
    row = db.get(Attachment, att.id)
    assert row is not None
    assert row.message_id == q1["id"]
    assert row.storage_path == "q18/shared.pdf"
    assert (
        db.query(Attachment).filter(Attachment.message_id == q1p["id"]).count()
        == 0
    )


def test_q18_original_still_reports_attachments(client: TestClient, db: Session):
    """Original message A still reports its attachments unchanged after branch."""
    user, h = _auth(client, db, "tq18b")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
    )
    before_refs = _att_refs(
        next(m for m in _get(client, h, cid, "all")["messages"] if m["id"] == q1["id"])
    )
    assert before_refs == [att.reference_id]

    _branch(client, h, cid, q1["id"], "user", "Q1′")
    detail = _get(client, h, cid, "all")
    orig = next(m for m in detail["messages"] if m["id"] == q1["id"])
    assert _att_refs(orig) == before_refs
    assert orig["attachments"][0]["filename"] == "doc.pdf"
    assert orig["sibling_count"] == 2
    db.expire_all()
    assert db.get(Attachment, att.id).message_id == q1["id"]


def test_q18_solo_message_unaffected(client: TestClient, db: Session):
    """Message with no siblings keeps ORM-bound attachments only (today's path)."""
    user, h = _auth(client, db, "tq18c")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "solo")
    att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
    )
    # Unrelated attachment on a later message must not leak onto the solo root.
    a1 = _append(client, h, cid, "assistant", "A1")
    _plant_attachment(
        db, user=user, conversation_id=cid, message_id=a1["id"],
        filename="other.pdf", storage_path="q18/other.pdf",
        extracted_text="other",
    )

    detail = _get(client, h, cid, "all")
    solo = next(m for m in detail["messages"] if m["id"] == q1["id"])
    assert solo["sibling_count"] == 1
    assert solo["sibling_ids"] == [q1["id"]]
    assert _att_refs(solo) == [att.reference_id]

    # Branch response itself is the only API that could invent chips; solo path
    # via append/get must stay relationship-only.
    refreshed = _append(client, h, cid, "user", "Q2")
    assert refreshed["sibling_count"] == 1
    assert refreshed["attachments"] == []


def test_q18_branch_does_not_change_prompt_injection(
    client: TestClient, db: Session, monkeypatch,
):
    """Injection stays conversation-scoped: one doc body, used_tokens unchanged."""
    from app.api.proxy import _inject_attachments
    from app.services import attachment_service as att_svc
    from app.services.attachment_context import (
        effective_cost,
        get_conversation_attachment_usage,
    )


    user, h = _auth(client, db, "tq18d")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    body_text = "UNIQUE_Q18_DOC_BODY_ONCE"
    att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
        extracted_text=body_text, token_count=100,
    )

    def _snap():
        chat = {
            "model": "gpt-test",
            "messages": [
                {"role": "system", "content": "系統"},
                {"role": "user", "content": "看附件"},
            ],
        }
        result = _inject_attachments(db, cid, chat, None)
        sys_content = chat["messages"][0]["content"]
        usage = get_conversation_attachment_usage(db, cid, 1000)
        return sys_content, usage, result

    before_sys, before_usage, before_result = _snap()
    assert before_sys.count(body_text) == 1
    assert before_usage["attachment_count"] == 1
    assert before_usage["used_tokens"] == effective_cost(db, att.token_count)
    assert before_result is not None
    assert before_result.injected_count == 1

    _branch(client, h, cid, q1["id"], "user", "Q1′")
    # Re-ask again — still one row / one injection (clone regression pin).
    _branch(client, h, cid, q1["id"], "user", "Q1″")

    db.expire_all()
    assert db.query(Attachment).filter(Attachment.conversation_id == cid).count() == 1
    after_sys, after_usage, after_result = _snap()
    assert after_sys.count(body_text) == 1
    assert after_usage["attachment_count"] == 1
    assert after_usage["used_tokens"] == before_usage["used_tokens"]
    assert after_result.injected_count == before_result.injected_count


def test_q18_delete_attachment_unlinks_one_file_no_dangling(
    client: TestClient, db: Session, tmp_path, monkeypatch,
):
    """DELETE /api/attachments/{ref} removes exactly one file; no shared-path hazard."""
    from app.services import attachment_service as att_svc

    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(att_svc, "ATTACHMENT_STORAGE_ROOT", root)
    monkeypatch.setattr(att_svc, "_storage_root", lambda: root)

    user, h = _auth(client, db, "tq18e")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    rel = f"{user.id}/q18-only.bin"
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_bytes(b"%PDF-only-one")
    att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
        storage_path=rel, extracted_text="only",
    )
    ref = att.reference_id
    att_id = att.id

    q1p = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    assert _att_refs(q1p) == [ref]
    assert full.is_file()
    assert db.query(Attachment).filter(Attachment.conversation_id == cid).count() == 1

    resp = client.delete(f"/api/attachments/{ref}", headers=h)
    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert db.get(Attachment, att_id) is None
    assert not full.exists()
    # No second row left pointing at the unlinked path.
    assert (
        db.query(Attachment).filter(Attachment.storage_path == rel).count() == 0
    )
    detail = _get(client, h, cid, "all")
    for mid in (q1["id"], q1p["id"]):
        msg = next(m for m in detail["messages"] if m["id"] == mid)
        assert msg["attachments"] == []


def test_q18_mutation_sibling_lookup_break_hides_chips(
    client: TestClient, db: Session, monkeypatch,
):
    """Break sibling-group lookup → branched message shows no chips (RED proof)."""
    import app.api.conversations as conv_api

    user, h = _auth(client, db, "tq18f")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
    )

    # Control: branch surfaces the chip.
    control = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    assert _att_refs(control) == [att.reference_id]

    monkeypatch.setattr(
        conv_api, "_attachments_for_sibling_group", lambda *_a, **_k: [],
    )
    broken = _branch(client, h, cid, q1["id"], "user", "Q1″").json()
    assert broken["sibling_count"] >= 2
    assert broken["attachments"] == []
    # Row still exists — display path only.
    db.expire_all()
    assert db.get(Attachment, att.id) is not None
    assert db.get(Attachment, att.id).message_id == q1["id"]


def test_q18_branched_chips_scoped_to_own_conversation(
    client: TestClient, db: Session,
):
    """F1: two users / two convs, both branched — only own conversation refs."""
    u1, h1 = _auth(client, db, "tq18g1")
    u2, h2 = _auth(client, db, "tq18g2")
    c1 = _create_conv(client, h1)
    c2 = _create_conv(client, h2)
    q1 = _append(client, h1, c1["id"], "user", "U1 Q1")
    q2 = _append(client, h2, c2["id"], "user", "U2 Q1")
    a1 = _plant_attachment(
        db, user=u1, conversation_id=c1["id"], message_id=q1["id"],
        filename="u1-secret.pdf", storage_path="q18/u1-secret.pdf",
        extracted_text="u1 body",
    )
    a2 = _plant_attachment(
        db, user=u2, conversation_id=c2["id"], message_id=q2["id"],
        filename="u2-secret.pdf", storage_path="q18/u2-secret.pdf",
        extracted_text="u2 body",
    )

    b1 = _branch(client, h1, c1["id"], q1["id"], "user", "U1 Q1′").json()
    b2 = _branch(client, h2, c2["id"], q2["id"], "user", "U2 Q1′").json()
    assert b1["sibling_count"] == 2
    assert b2["sibling_count"] == 2
    assert _att_refs(b1) == [a1.reference_id]
    assert _att_refs(b2) == [a2.reference_id]
    assert a2.reference_id not in _att_refs(b1)
    assert a1.reference_id not in _att_refs(b2)

    d1 = _get(client, h1, c1["id"], "all")
    d2 = _get(client, h2, c2["id"], "all")
    for mid in (q1["id"], b1["id"]):
        msg = next(m for m in d1["messages"] if m["id"] == mid)
        assert _att_refs(msg) == [a1.reference_id]
    for mid in (q2["id"], b2["id"]):
        msg = next(m for m in d2["messages"] if m["id"] == mid)
        assert _att_refs(msg) == [a2.reference_id]


def test_q18_branched_chips_exclude_other_turns_attachments(
    client: TestClient, db: Session,
):
    """Branched turn chips stay turn-scoped — later-turn files must not leak in.

    Pins Attachment.message_id.in_(sibling_ids) in _attachments_for_sibling_group:
    without it, conversation_id alone would paint every file onto every branched
    bubble (solo-path tests never enter that helper).
    """
    user, h = _auth(client, db, "tq18turn")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1 with attach")
    turn1_att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
        filename="turn1.pdf", storage_path="q18/turn1.pdf",
        extracted_text="turn1 body",
    )
    q1p = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    assert q1p["sibling_count"] == 2

    # Later, separate turn (not a sibling of turn 1).
    _append(client, h, cid, "assistant", "A1")
    q2 = _append(client, h, cid, "user", "Q2 later turn")
    later_att = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q2["id"],
        filename="report.xlsx", storage_path="q18/report.xlsx",
        extracted_text="later body",
    )

    detail = _get(client, h, cid, "all")
    for mid in (q1["id"], q1p["id"]):
        msg = next(m for m in detail["messages"] if m["id"] == mid)
        assert msg["sibling_count"] == 2
        assert _att_refs(msg) == [turn1_att.reference_id]
        assert later_att.reference_id not in _att_refs(msg)


def test_q18_symmetric_union_branch_upload_visible_on_original(
    client: TestClient, db: Session,
):
    """F2: attachment on the branch also renders on the original turn."""
    user, h = _auth(client, db, "tq18h")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    orig = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
        filename="orig.txt", storage_path="q18/orig.txt",
        extracted_text="orig body",
    )
    q1p = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    branch = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1p["id"],
        filename="branchonly.txt", storage_path="q18/branchonly.txt",
        extracted_text="branch body",
    )

    detail = _get(client, h, cid, "all")
    orig_msg = next(m for m in detail["messages"] if m["id"] == q1["id"])
    branch_msg = next(m for m in detail["messages"] if m["id"] == q1p["id"])
    expected = [orig.reference_id, branch.reference_id]
    assert _att_refs(orig_msg) == expected
    assert _att_refs(branch_msg) == expected
    assert [a["filename"] for a in orig_msg["attachments"]] == [
        "orig.txt", "branchonly.txt",
    ]
    db.expire_all()
    assert db.query(Attachment).filter(Attachment.conversation_id == cid).count() == 2


def test_q18_sibling_attachment_chip_order_created_at_then_id(
    client: TestClient, db: Session,
):
    """F5: branched path orders chips by created_at, id (not bare PK order)."""
    user, h = _auth(client, db, "tq18i")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    q1p = _branch(client, h, cid, q1["id"], "user", "Q1′").json()

    # Insert so id(first_pdf) < id(second_pdf), then invert created_at.
    later_row = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1["id"],
        filename="second.pdf", storage_path="q18/second.pdf",
        extracted_text="second",
    )
    earlier_row = _plant_attachment(
        db, user=user, conversation_id=cid, message_id=q1p["id"],
        filename="first.pdf", storage_path="q18/first.pdf",
        extracted_text="first",
    )
    earlier = datetime.now(timezone.utc) - timedelta(hours=2)
    later = datetime.now(timezone.utc) - timedelta(hours=1)
    earlier_row.created_at = earlier
    later_row.created_at = later
    db.commit()
    assert later_row.id < earlier_row.id

    detail = _get(client, h, cid, "all")
    for mid in (q1["id"], q1p["id"]):
        msg = next(m for m in detail["messages"] if m["id"] == mid)
        assert [a["filename"] for a in msg["attachments"]] == [
            "first.pdf", "second.pdf",
        ]
        assert _att_refs(msg) == [
            earlier_row.reference_id, later_row.reference_id,
        ]


def test_q18_message_branch_create_has_no_attachment_field():
    """Branch body cannot carry attachments — display union is read-side only."""
    from app.api.conversations import MessageBranchCreate

    fields = MessageBranchCreate.model_fields
    assert "attachment" not in fields
    assert "attachments" not in fields
    assert "attachment_ids" not in fields
    assert "reference_id" not in fields


def test_q18_real_upload_branch_keeps_one_row(
    client: TestClient, db: Session, tmp_path, monkeypatch,
):
    """Real POST /api/attachments → branch: still exactly one Attachment row."""
    from app.services import attachment_service as att_svc

    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(att_svc, "ATTACHMENT_STORAGE_ROOT", root)
    monkeypatch.setattr(att_svc, "_storage_root", lambda: root)

    _, h = _auth(client, db, "tq18j")
    conv = _create_conv(client, h)
    cid = conv["id"]
    q1 = _append(client, h, cid, "user", "Q1")
    up = client.post(
        "/api/attachments",
        headers=h,
        data={"message_id": str(q1["id"])},
        files={"file": ("real-orig.txt", io.BytesIO(b"hello-q18"), "text/plain")},
    )
    assert up.status_code == 201, up.text
    ref = up.json()["reference_id"]
    assert db.query(Attachment).filter(Attachment.conversation_id == cid).count() == 1

    q1p = _branch(client, h, cid, q1["id"], "user", "Q1′").json()
    assert _att_refs(q1p) == [ref]
    db.expire_all()
    assert db.query(Attachment).filter(Attachment.conversation_id == cid).count() == 1
    assert (
        db.query(Attachment).filter(Attachment.message_id == q1p["id"]).count()
        == 0
    )
