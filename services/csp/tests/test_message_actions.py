"""OW-3 WP-A — message actions backend tests (blueprint §5 cases 1–20).

docs/plans/ow3-message-actions-blueprint.md
"""
from __future__ import annotations

import hashlib
import json
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.message_action import MessageAction
from app.models.user import User
from app.services import message_action_service as svc
from app.services.message_action_service import reset_rate_limit_for_tests
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)
    reset_rate_limit_for_tests()
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", False)
    monkeypatch.setattr(settings, "ANILA_ACTION_INVOKE_PER_MIN", 20)


def _auth(client: TestClient, db: Session, username: str, role: str = "user",
          department_id=None) -> tuple[User, dict]:
    user = make_user(db, username=username, role=role, department_id=department_id)
    return user, {"Authorization": f"Bearer {login(client, username=username)}"}


def _decl_body(**over) -> dict:
    base = {
        "name": "translate-en",
        "label": "翻譯成英文",
        "icon": "translate",
        "kind": "declarative",
        "result_mode": "to_model",
        "body": "請翻譯：\n\n{content}\n選項:{choice}\n輸入:{input}",
        "choices": [],
    }
    base.update(over)
    return base


def _create(client, headers, body=None, expect=201):
    resp = client.post(
        "/api/message-actions",
        json=body or _decl_body(),
        headers=headers,
    )
    assert resp.status_code == expect, resp.text
    return resp


def _bind(client, headers, action_id, bindings, expect=200):
    resp = client.put(
        f"/api/message-actions/{action_id}/bindings",
        json={"bindings": bindings},
        headers=headers,
    )
    assert resp.status_code == expect, resp.text
    return resp


def _conv_with_assistant(client, headers, content="Hello {world}", **extra):
    resp = client.post(
        "/api/conversations", json={"title": "ma-test", **extra}, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    u = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "Q"},
        headers=headers,
    )
    assert u.status_code == 201, u.text
    a = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": content},
        headers=headers,
    )
    assert a.status_code == 201, a.text
    return cid, a.json()["id"]


# ── 1. POST auth ─────────────────────────────────────────────────────────────


def test_01_post_auth_owner_only(client: TestClient, db: Session):
    _, uh = _auth(client, db, "ma01u", role="user")
    _, ah = _auth(client, db, "ma01a", role="admin")
    _, oh = _auth(client, db, "ma01o", role="owner")
    assert _create(client, uh, expect=403).status_code == 403
    assert client.post(
        "/api/message-actions", json=_decl_body(name="x"), headers=ah,
    ).status_code == 403
    assert _create(client, oh, _decl_body(name="ok01")).status_code == 201


# ── 2. Create declarative + audit snapshot ───────────────────────────────────


def test_02_create_declarative_version_sha_audit(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma02o", role="owner")
    body = _decl_body(name="sum02", body="摘要：{content}")
    data = _create(client, oh, body).json()
    assert data["version"] == 1
    expect_sha = hashlib.sha256(body["body"].encode()).hexdigest()
    assert data["body_sha256"] == expect_sha
    assert data["body"] == body["body"]

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_create")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    meta = json.loads(row.metadata_json)
    assert meta["body"] == body["body"]
    assert meta["body_sha256"] == expect_sha


# ── 3. PUT body bumps version ────────────────────────────────────────────────


def test_03_put_body_version_and_prev_sha(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma03o", role="owner")
    created = _create(client, oh, _decl_body(name="v03")).json()
    old_sha = created["body_sha256"]
    resp = client.put(
        f"/api/message-actions/{created['id']}",
        json={"body": "新模板 {content}"},
        headers=oh,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == 2
    assert data["body_sha256"] != old_sha
    meta = json.loads(
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_update")
        .order_by(AuditLog.id.desc())
        .first()
        .metadata_json
    )
    assert meta["previous_body_sha256"] == old_sha
    assert meta["body_sha256"] == data["body_sha256"]


# ── 4. Validation errors ─────────────────────────────────────────────────────


def test_04_unknown_icon_kind_choices(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma04o", role="owner")
    r = client.post(
        "/api/message-actions",
        json=_decl_body(name="badicon", icon="not-an-icon"),
        headers=oh,
    )
    assert r.status_code == 400
    assert "未知的圖示" in r.json()["detail"]

    r = client.post(
        "/api/message-actions",
        json=_decl_body(name="badkind", kind="magic"),
        headers=oh,
    )
    assert r.status_code == 400
    assert "未知的動作類型" in r.json()["detail"]

    choices = [
        {"id": f"c{i}", "label": f"L{i}", "prompt": "p"} for i in range(21)
    ]
    r = client.post(
        "/api/message-actions",
        json=_decl_body(name="too-many", choices=choices),
        headers=oh,
    )
    assert r.status_code == 400
    assert "選項數量超過上限" in r.json()["detail"]

    r = client.post(
        "/api/message-actions",
        json=_decl_body(
            name="dupcid",
            choices=[
                {"id": "same", "label": "A", "prompt": "p"},
                {"id": "same", "label": "B", "prompt": "q"},
            ],
        ),
        headers=oh,
    )
    assert r.status_code == 400
    assert "重複" in r.json()["detail"]


# ── 5. exec validate_source never executes ───────────────────────────────────


def test_05_exec_validate_no_execution(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ma05o", role="owner")

    import tempfile

    # Module-level canary: would create a file if validate_source executed.
    marker = tempfile.mktemp(prefix="ma05_")
    src_canary = (
        f"open({marker!r}, 'w').write('fired')\n"
        "def run(ctx):\n    return 'ok'\n"
    )
    r = client.post(
        "/api/message-actions",
        json={
            "name": "exec05",
            "label": "E",
            "icon": "code",
            "kind": "exec",
            "result_mode": "direct",
            "body": src_canary,
            "choices": [],
        },
        headers=oh,
    )
    # validate_source compiles but does not exec → create succeeds, marker absent
    assert r.status_code == 201, r.text
    assert not os.path.exists(marker)

    r = client.post(
        "/api/message-actions",
        json={
            "name": "norun05",
            "label": "E",
            "icon": "code",
            "kind": "exec",
            "result_mode": "direct",
            "body": "x = 1\n",
            "choices": [],
        },
        headers=oh,
    )
    assert r.status_code == 400
    assert "必須定義 run(ctx)" in r.json()["detail"]

    r = client.post(
        "/api/message-actions",
        json={
            "name": "syn05",
            "label": "E",
            "icon": "code",
            "kind": "exec",
            "result_mode": "direct",
            "body": "def run(ctx)\n    return 'x'\n",
            "choices": [],
        },
        headers=oh,
    )
    assert r.status_code == 400
    assert "語法錯誤" in r.json()["detail"]
    assert "第" in r.json()["detail"]


# ── 6. Fail-closed authoring ─────────────────────────────────────────────────


def test_06_fail_closed_authoring(client: TestClient, db: Session, monkeypatch):
    _, oh = _auth(client, db, "ma06o", role="owner")

    def _none(*a, **k):
        return None

    monkeypatch.setattr(svc, "log_audit_event", _none)
    r = client.post(
        "/api/message-actions",
        json=_decl_body(name="failclosed06"),
        headers=oh,
    )
    assert r.status_code == 500
    assert "稽核紀錄寫入失敗" in r.json()["detail"]
    assert (
        db.query(MessageAction)
        .filter(MessageAction.name == "failclosed06")
        .first()
        is None
    )


# ── 7. /visible no bindings ──────────────────────────────────────────────────


def test_07_visible_no_bindings(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma07o", role="owner")
    _, uh = _auth(client, db, "ma07u", role="user")
    created = _create(client, oh, _decl_body(name="nobind07")).json()
    vis_u = client.get("/api/message-actions/visible", headers=uh)
    assert vis_u.status_code == 200
    assert vis_u.json() == []
    vis_o = client.get("/api/message-actions/visible", headers=oh)
    assert any(a["id"] == created["id"] for a in vis_o.json())


# ── 8. Bind by user ──────────────────────────────────────────────────────────


def test_08_bind_by_user(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma08o", role="owner")
    ua, uh = _auth(client, db, "ma08a", role="user")
    _, bh = _auth(client, db, "ma08b", role="user")
    aid = _create(client, oh, _decl_body(name="userbind08")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": ua.id}])
    ids_a = {a["id"] for a in client.get("/api/message-actions/visible", headers=uh).json()}
    ids_b = {a["id"] for a in client.get("/api/message-actions/visible", headers=bh).json()}
    assert aid in ids_a
    assert aid not in ids_b


# ── 9. Bind department parent → grandchild ───────────────────────────────────


def test_09_bind_department_subtree(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma09o", role="owner")
    root = Department(name="ma09院", parent_id=None, is_active=True)
    db.add(root)
    db.commit()
    db.refresh(root)
    mid = Department(name="ma09所", parent_id=root.id, is_active=True)
    db.add(mid)
    db.commit()
    db.refresh(mid)
    leaf = Department(name="ma09組", parent_id=mid.id, is_active=True)
    sib = Department(name="ma09他院", parent_id=None, is_active=True)
    db.add_all([leaf, sib])
    db.commit()
    db.refresh(leaf)
    db.refresh(sib)

    _, gh = _auth(client, db, "ma09g", role="user", department_id=leaf.id)
    _, sh = _auth(client, db, "ma09s", role="user", department_id=sib.id)
    aid = _create(client, oh, _decl_body(name="dept09")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "department", "department_id": root.id}])
    assert aid in {
        a["id"] for a in client.get("/api/message-actions/visible", headers=gh).json()
    }
    assert aid not in {
        a["id"] for a in client.get("/api/message-actions/visible", headers=sh).json()
    }


# ── 10. Bind role ────────────────────────────────────────────────────────────


def test_10_bind_role_developer(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma10o", role="owner")
    _, dh = _auth(client, db, "ma10d", role="developer")
    _, uh = _auth(client, db, "ma10u", role="user")
    aid = _create(client, oh, _decl_body(name="role10")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "role", "role": "developer"}])
    assert aid in {
        a["id"] for a in client.get("/api/message-actions/visible", headers=dh).json()
    }
    assert aid not in {
        a["id"] for a in client.get("/api/message-actions/visible", headers=uh).json()
    }


# ── 11. is_enabled=false ─────────────────────────────────────────────────────


def test_11_disabled_gone(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma11o", role="owner")
    u, uh = _auth(client, db, "ma11u", role="user")
    aid = _create(client, oh, _decl_body(name="dis11")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    client.put(
        f"/api/message-actions/{aid}",
        json={"is_enabled": False},
        headers=oh,
    )
    assert client.get("/api/message-actions/visible", headers=uh).json() == []
    cid, mid = _conv_with_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "動作不存在"


# ── 12. invoke unbound → 404 ─────────────────────────────────────────────────


def test_12_invoke_unbound_404(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma12o", role="owner")
    _, uh = _auth(client, db, "ma12u", role="user")
    aid = _create(client, oh, _decl_body(name="unb12")).json()["id"]
    cid, mid = _conv_with_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "動作不存在"


# ── 13. invoke declarative substitution ──────────────────────────────────────


def test_13_invoke_declarative_substitution(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma13o", role="owner")
    u, uh = _auth(client, db, "ma13u", role="user")
    body = (
        "T:{content}|C:{choice}|I:{input}|brace-in-msg-ok"
    )
    aid = _create(
        client,
        oh,
        _decl_body(
            name="sub13",
            body=body,
            choices=[
                {
                    "id": "en",
                    "label": "EN",
                    "prompt": "to-english",
                    "input": True,
                    "input_label": "備註",
                }
            ],
        ),
    ).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    cid, mid = _conv_with_assistant(client, uh, content="Hello {world}")
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={
            "conversation_id": cid,
            "message_id": mid,
            "choice_id": "en",
            "input": "note1",
        },
        headers=uh,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["outcome"] == "prompt"
    assert data["prompt"] == "T:Hello {world}|C:to-english|I:note1|brace-in-msg-ok"


# ── 14. invoke audit row shape ───────────────────────────────────────────────


def test_14_invoke_audit_no_body(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma14o", role="owner")
    u, uh = _auth(client, db, "ma14u", role="user")
    created = _create(client, oh, _decl_body(name="aud14")).json()
    aid = created["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    cid, mid = _conv_with_assistant(client, uh)
    before = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .all()
    )
    assert len(rows) == before + 1
    meta = json.loads(rows[-1].metadata_json)
    assert meta["version"] == created["version"]
    assert meta["body_sha256"] == created["body_sha256"]
    assert "body" not in meta


# ── 15. foreign / missing conv / wrong message ───────────────────────────────


def test_15_conversation_gates(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma15o", role="owner")
    ua, uh = _auth(client, db, "ma15a", role="user")
    ub, bh = _auth(client, db, "ma15b", role="user")
    aid = _create(client, oh, _decl_body(name="cg15")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": ua.id}])
    # Also bind B so visibility passes and we hit conversation gate.
    _bind(
        client,
        oh,
        aid,
        [
            {"scope_type": "user", "user_id": ua.id},
            {"scope_type": "user", "user_id": ub.id},
        ],
    )
    cid_a, mid_a = _conv_with_assistant(client, uh)
    cid_b, mid_b = _conv_with_assistant(client, bh)

    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid_b, "message_id": mid_b},
        headers=uh,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "無權存取此對話"

    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": 999999, "message_id": mid_a},
        headers=uh,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "找不到此對話"

    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid_a, "message_id": mid_b},
        headers=uh,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "訊息不屬於此對話"


# ── 16. ANILALM 409 before execution ─────────────────────────────────────────


def test_16_anilalm_409(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma16o", role="owner")
    u, uh = _auth(client, db, "ma16u", role="user")
    aid = _create(client, oh, _decl_body(name="ani16")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    # ANILALM conversations require collection_id — create via API may 400;
    # set origin on a normal conv.
    cid, mid = _conv_with_assistant(client, uh)
    from app.models.conversation import Conversation

    conv = db.query(Conversation).filter(Conversation.id == cid).first()
    conv.origin = "anilalm"
    db.commit()
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "ANILALM 對話不支援訊息分支"


# ── 17. classification gate ──────────────────────────────────────────────────


def test_17_classification_gate(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma17o", role="owner")
    u, uh = _auth(client, db, "ma17u", role="user")
    aid = _create(client, oh, _decl_body(name="cls17")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    from app.models.conversation import Conversation

    cid, mid = _conv_with_assistant(client, uh)
    conv = db.query(Conversation).filter(Conversation.id == cid).first()
    conv.classification_level = "密"
    db.commit()
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "此對話密等為「密」，不可執行自訂動作"

    cid2, mid2 = _conv_with_assistant(client, uh)
    conv2 = db.query(Conversation).filter(Conversation.id == cid2).first()
    conv2.classification_level = "營業秘密"
    db.commit()
    before = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid2, "message_id": mid2},
        headers=uh,
    )
    assert r.status_code == 200, r.text
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
        == before + 1
    )


def test_classification_refusal_writes_refused_audit(
    client: TestClient, db: Session,
):
    """Controlled-conv refusal → exactly one refuse row, no success row."""
    _, oh = _auth(client, db, "ma_ref_o", role="owner")
    u, uh = _auth(client, db, "ma_ref_u", role="user")
    created = _create(client, oh, _decl_body(name="refcls")).json()
    aid = created["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    from app.models.conversation import Conversation

    cid, mid = _conv_with_assistant(client, uh)
    conv = db.query(Conversation).filter(Conversation.id == cid).first()
    conv.classification_level = "密"
    db.commit()

    before_ok = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    before_ref = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 403
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
        == before_ok
    )
    refused = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .order_by(AuditLog.id.asc())
        .all()
    )
    assert len(refused) == before_ref + 1
    row = refused[-1]
    assert row.actor_user_id == u.id
    assert row.resource_id == str(aid)
    assert row.status == "refused"
    meta = json.loads(row.metadata_json)
    assert meta["version"] == created["version"]
    assert meta["conversation_id"] == cid
    assert meta["conversation_level"] == "密"
    assert meta["outcome"] == "refused"
    assert meta["reason"] == "classification"


def test_successful_invoke_no_refusal_audit(
    client: TestClient, db: Session,
):
    """Success path still writes exactly one invoke row and no refusal."""
    _, oh = _auth(client, db, "ma_sok_o", role="owner")
    u, uh = _auth(client, db, "ma_sok_u", role="user")
    created = _create(client, oh, _decl_body(name="sokok")).json()
    aid = created["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    cid, mid = _conv_with_assistant(client, uh)

    before_ok = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    before_ref = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200, r.text
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
        == before_ok + 1
    )
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
        == before_ref
    )


# ── 18. user-role target → 400 ───────────────────────────────────────────────


def test_18_user_role_target_400(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma18o", role="owner")
    u, uh = _auth(client, db, "ma18u", role="user")
    aid = _create(client, oh, _decl_body(name="role18")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    cid, _mid = _conv_with_assistant(client, uh)
    # Use the user message id
    msgs = client.get(f"/api/conversations/{cid}?view=all", headers=uh).json()["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": user_msg["id"]},
        headers=uh,
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "只能對助理訊息執行動作"


# ── 19. rate limit 429 ───────────────────────────────────────────────────────


def test_19_rate_limit(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ACTION_INVOKE_PER_MIN", 20)
    reset_rate_limit_for_tests()
    _, oh = _auth(client, db, "ma19o", role="owner")
    u, uh = _auth(client, db, "ma19u", role="user")
    aid = _create(client, oh, _decl_body(name="rl19")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    cid, mid = _conv_with_assistant(client, uh)
    for i in range(20):
        r = client.post(
            f"/api/message-actions/{aid}/invoke",
            json={"conversation_id": cid, "message_id": mid},
            headers=uh,
        )
        assert r.status_code == 200, f"call {i+1}: {r.text}"
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "動作呼叫過於頻繁，請稍候再試"


# ── 20. Export ───────────────────────────────────────────────────────────────


def test_20_export_owner_only(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ma20o", role="owner")
    _, ah = _auth(client, db, "ma20a", role="admin")
    body = _decl_body(name="exp20", body="EXPORT_BODY_MARKER {content}")
    _create(client, oh, body)

    r = client.get("/api/message-actions/audit/export", headers=ah)
    assert r.status_code == 403

    before_export = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_audit_export")
        .count()
    )
    r = client.get("/api/message-actions/audit/export", headers=oh)
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers.get("content-type", "")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert any("EXPORT_BODY_MARKER" in ln for ln in lines)
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_audit_export")
        .count()
        == before_export + 1
    )


def test_single_pass_substitution_preserves_tokens_in_content(
    client: TestClient, db: Session
):
    """Tokens inside substituted message content must not be re-scanned."""
    _, oh = _auth(client, db, "ma_sp_o", role="owner")
    u, uh = _auth(client, db, "ma_sp_u", role="user")
    aid = _create(
        client,
        oh,
        _decl_body(
            name="singlepass",
            body="OUT:{content}|C:{choice}",
            choices=[
                {
                    "id": "c1",
                    "label": "C1",
                    "prompt": "CHOICE_PROMPT",
                }
            ],
        ),
    ).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    cid, mid = _conv_with_assistant(
        client, uh, content="literal {choice} stays"
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={
            "conversation_id": cid,
            "message_id": mid,
            "choice_id": "c1",
        },
        headers=uh,
    )
    assert r.status_code == 200, r.text
    assert r.json()["prompt"] == "OUT:literal {choice} stays|C:CHOICE_PROMPT"


def test_unknown_classification_level_fails_closed(
    client: TestClient, db: Session
):
    """Unparseable stored level must not fall back to UNCLASSIFIED."""
    import asyncio

    _, oh = _auth(client, db, "ma_ucl_o", role="owner")
    u, uh = _auth(client, db, "ma_ucl_u", role="user")
    aid = _create(client, oh, _decl_body(name="ucl")).json()["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    from app.models.conversation import Conversation

    cid, mid = _conv_with_assistant(client, uh)
    conv = db.query(Conversation).filter(Conversation.id == cid).first()
    conv.classification_level = "NOT_A_REAL_LEVEL"
    db.commit()
    before = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    with pytest.raises(ValueError, match="未知的分類等級"):
        asyncio.run(
            svc.invoke_action(
                db,
                action_id=aid,
                conversation_id=cid,
                message_id=mid,
                choice_id=None,
                user_input=None,
                actor=u,
            )
        )
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
        == before
    )


def test_duplicate_bindings_deduped(client: TestClient, db: Session):
    """Duplicate specs in one PUT must not 500 (IntegrityError)."""
    _, oh = _auth(client, db, "ma_dup_o", role="owner")
    ua, _uh = _auth(client, db, "ma_dup_u", role="user")
    aid = _create(client, oh, _decl_body(name="dupbind")).json()["id"]
    r = _bind(
        client,
        oh,
        aid,
        [
            {"scope_type": "user", "user_id": ua.id},
            {"scope_type": "user", "user_id": ua.id},
        ],
    )
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


def test_admin_no_binding_no_bypass(client: TestClient, db: Session):
    """Admin without a binding: empty /visible and 404 on invoke."""
    _, oh = _auth(client, db, "ma_anb_o", role="owner")
    _, ah = _auth(client, db, "ma_anb_a", role="admin")
    aid = _create(client, oh, _decl_body(name="adminnb")).json()["id"]
    vis = client.get("/api/message-actions/visible", headers=ah)
    assert vis.status_code == 200
    assert vis.json() == []
    cid, mid = _conv_with_assistant(client, ah)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=ah,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "動作不存在"


def test_admin_body_redacted(client: TestClient, db: Session):
    """Blueprint §4: non-owner admin reads body as '<owner-only>'."""
    _, oh = _auth(client, db, "ma_red_o", role="owner")
    _, ah = _auth(client, db, "ma_red_a", role="admin")
    secret = "SECRET_BODY_CONTENT {content}"
    _create(client, oh, _decl_body(name="redact", body=secret))
    rows = client.get("/api/message-actions", headers=ah).json()
    hit = next(r for r in rows if r["name"] == "redact")
    assert hit["body"] == "<owner-only>"
    rows_o = client.get("/api/message-actions", headers=oh).json()
    hit_o = next(r for r in rows_o if r["name"] == "redact")
    assert hit_o["body"] == secret


def test_access_denied_refusal_writes_refused_audit(
    client: TestClient, db: Session,
):
    """Foreign conversation → access_denied refuse row with expected shape."""
    _, oh = _auth(client, db, "ma_ad_o", role="owner")
    ua, uh = _auth(client, db, "ma_ad_a", role="user")
    ub, bh = _auth(client, db, "ma_ad_b", role="user")
    created = _create(client, oh, _decl_body(name="accden")).json()
    aid = created["id"]
    _bind(
        client,
        oh,
        aid,
        [
            {"scope_type": "user", "user_id": ua.id},
            {"scope_type": "user", "user_id": ub.id},
        ],
    )
    _cid_a, _mid_a = _conv_with_assistant(client, uh)
    cid_b, mid_b = _conv_with_assistant(client, bh)

    before_ok = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    before_ref = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid_b, "message_id": mid_b},
        headers=uh,
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "無權存取此對話"
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
        == before_ok
    )
    refused = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .order_by(AuditLog.id.asc())
        .all()
    )
    assert len(refused) == before_ref + 1
    row = refused[-1]
    assert row.actor_user_id == ua.id
    assert row.resource_id == str(aid)
    assert row.status == "refused"
    meta = json.loads(row.metadata_json)
    assert meta["version"] == created["version"]
    assert meta["conversation_id"] == cid_b
    assert meta["outcome"] == "refused"
    assert meta["reason"] == "access_denied"
    assert "conversation_level" in meta


def test_not_branchable_refusal_writes_refused_audit(
    client: TestClient, db: Session,
):
    """Non-branchable conversation → not_branchable refuse row."""
    _, oh = _auth(client, db, "ma_nb_o", role="owner")
    u, uh = _auth(client, db, "ma_nb_u", role="user")
    created = _create(client, oh, _decl_body(name="notbr")).json()
    aid = created["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    from app.models.conversation import Conversation

    cid, mid = _conv_with_assistant(client, uh)
    conv = db.query(Conversation).filter(Conversation.id == cid).first()
    conv.origin = "anilalm"
    db.commit()

    before_ok = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    before_ref = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
    )
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "ANILALM 對話不支援訊息分支"
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
        == before_ok
    )
    refused = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .order_by(AuditLog.id.asc())
        .all()
    )
    assert len(refused) == before_ref + 1
    row = refused[-1]
    assert row.actor_user_id == u.id
    assert row.resource_id == str(aid)
    assert row.status == "refused"
    meta = json.loads(row.metadata_json)
    assert meta["version"] == created["version"]
    assert meta["conversation_id"] == cid
    assert meta["outcome"] == "refused"
    assert meta["reason"] == "not_branchable"


def test_export_includes_refused_row(client: TestClient, db: Session):
    """NDJSON export contains the refused row (name, status, reason)."""
    _, oh = _auth(client, db, "ma_exr_o", role="owner")
    u, uh = _auth(client, db, "ma_exr_u", role="user")
    created = _create(client, oh, _decl_body(name="expref")).json()
    aid = created["id"]
    _bind(client, oh, aid, [{"scope_type": "user", "user_id": u.id}])
    from app.models.conversation import Conversation

    cid, mid = _conv_with_assistant(client, uh)
    conv = db.query(Conversation).filter(Conversation.id == cid).first()
    conv.classification_level = "密"
    db.commit()
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 403

    exp = client.get("/api/message-actions/audit/export", headers=oh)
    assert exp.status_code == 200
    assert "application/x-ndjson" in exp.headers.get("content-type", "")
    lines = [ln for ln in exp.text.splitlines() if ln.strip()]
    refused_lines = []
    for ln in lines:
        obj = json.loads(ln)
        if obj.get("action") == "message_action_invoke_refused":
            refused_lines.append(obj)
    assert refused_lines, "export must include refused row"
    hit = next(
        o for o in refused_lines
        if o.get("resource_id") == str(aid)
        and (o.get("metadata") or {}).get("reason") == "classification"
    )
    assert hit["status"] == "refused"
    assert hit["action"] == "message_action_invoke_refused"
    assert (hit.get("metadata") or {}).get("outcome") == "refused"


def test_rate_limit_stops_refusal_audit_rows(
    client: TestClient, db: Session, monkeypatch,
):
    """Repeated refused attempts stop writing rows once the limit is hit."""
    monkeypatch.setattr(settings, "ANILA_ACTION_INVOKE_PER_MIN", 3)
    reset_rate_limit_for_tests()
    _, oh = _auth(client, db, "ma_rlr_o", role="owner")
    ua, uh = _auth(client, db, "ma_rlr_a", role="user")
    ub, bh = _auth(client, db, "ma_rlr_b", role="user")
    aid = _create(client, oh, _decl_body(name="rlref")).json()["id"]
    _bind(
        client,
        oh,
        aid,
        [
            {"scope_type": "user", "user_id": ua.id},
            {"scope_type": "user", "user_id": ub.id},
        ],
    )
    # Distinct foreign conversations — same access-denied gate each time.
    foreign = [_conv_with_assistant(client, bh) for _ in range(5)]

    before_ref = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
    )
    for i in range(3):
        cid, mid = foreign[i]
        r = client.post(
            f"/api/message-actions/{aid}/invoke",
            json={"conversation_id": cid, "message_id": mid},
            headers=uh,
        )
        assert r.status_code == 403, f"call {i+1}: {r.text}"
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
        == before_ref + 3
    )

    cid, mid = foreign[3]
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 429
    assert r.json()["detail"] == "動作呼叫過於頻繁，請稍候再試"
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke_refused")
        .count()
        == before_ref + 3
    )


def test_icons_endpoint_shape_and_exec_flag(client: TestClient, db: Session):
    """GET /icons returns object with icons list and boolean exec flag."""
    from app.schemas.message_action import ALLOWED_ACTION_ICONS

    _, oh = _auth(client, db, "ma_ico_o", role="owner")
    _, ah = _auth(client, db, "ma_ico_a", role="admin")

    denied = client.get("/api/message-actions/icons", headers=ah)
    assert denied.status_code == 403

    r = client.get("/api/message-actions/icons", headers=oh)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    assert set(data.keys()) >= {"icons", "action_exec_enabled"}
    assert isinstance(data["icons"], list)
    assert sorted(data["icons"]) == sorted(ALLOWED_ACTION_ICONS)
    assert isinstance(data["action_exec_enabled"], bool)
    assert data["action_exec_enabled"] is False  # autouse fixture default
