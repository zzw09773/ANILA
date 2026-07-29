"""OW-3 WP-A — exec path tests (blueprint §5 cases 21–28).

docs/plans/ow3-message-actions-blueprint.md
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.services import message_action_exec as exec_mod
from app.services import message_action_service as svc
from app.services.message_action_service import reset_rate_limit_for_tests
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)
    reset_rate_limit_for_tests()
    exec_mod.reset_runtime_state_for_tests()
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", False)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(settings, "ANILA_ACTION_OUTPUT_MAX_CHARS", 20000)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(settings, "ANILA_ACTION_INVOKE_PER_MIN", 100)


def _auth(client, db, username, role="user"):
    user = make_user(db, username=username, role=role)
    return user, {"Authorization": f"Bearer {login(client, username=username)}"}


def _create_exec(client, headers, name, body, result_mode="direct", expect=201):
    resp = client.post(
        "/api/message-actions",
        json={
            "name": name,
            "label": name,
            "icon": "code",
            "kind": "exec",
            "result_mode": result_mode,
            "body": body,
            "choices": [],
        },
        headers=headers,
    )
    assert resp.status_code == expect, resp.text
    return resp


def _bind_user(client, headers, action_id, user_id):
    r = client.put(
        f"/api/message-actions/{action_id}/bindings",
        json={"bindings": [{"scope_type": "user", "user_id": user_id}]},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def _conv_assistant(client, headers, content="msg-content"):
    resp = client.post(
        "/api/conversations", json={"title": "exec-test"}, headers=headers,
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    assert client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "user", "content": "Q"},
        headers=headers,
    ).status_code == 201
    a = client.post(
        f"/api/conversations/{cid}/messages",
        json={"role": "assistant", "content": content},
        headers=headers,
    )
    assert a.status_code == 201, a.text
    return cid, a.json()["id"]


# ── 21. Flag off → absent + 404 ──────────────────────────────────────────────


def test_21_flag_off_invisible_and_404(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", False)
    _, oh = _auth(client, db, "ex21o", role="owner")
    u, uh = _auth(client, db, "ex21u", role="user")
    # Owner can still create (authoring) even when flag off.
    aid = _create_exec(
        client, oh, "flag21", "def run(ctx):\n    return 'hi'\n"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    vis = client.get("/api/message-actions/visible", headers=uh).json()
    assert aid not in {a["id"] for a in vis}
    # Owner console still lists it
    admin_list = client.get("/api/message-actions", headers=oh).json()
    assert any(a["id"] == aid for a in admin_list)

    cid, mid = _conv_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "動作不存在"


# ── 22. Flag on → run(ctx) + ctx keys ────────────────────────────────────────


def test_22_flag_on_ctx_and_outcome(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ex22o", role="owner")
    u, uh = _auth(client, db, "ex22u", role="user")

    src = (
        "def run(ctx):\n"
        "    keys = sorted(ctx.keys())\n"
        "    ukeys = sorted(ctx['user'].keys())\n"
        "    assert 'Session' not in str(type(ctx))\n"
        "    return f\"keys={keys};ukeys={ukeys};msg={ctx['message']}\"\n"
    )
    aid = _create_exec(
        client, oh, "ctx22", src, result_mode="direct"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh, content="HELLO")
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["outcome"] == "text"
    assert data["output"].startswith("keys=")
    assert "message" in data["output"]
    assert "message_id" in data["output"]
    assert "conversation_id" in data["output"]
    assert "choice" in data["output"]
    assert "input" in data["output"]
    assert "user" in data["output"]
    assert "HELLO" in data["output"]
    assert "ukeys=['department_id', 'id', 'username']" in data["output"]

    # to_model → outcome prompt
    aid2 = _create_exec(
        client,
        oh,
        "ctx22tm",
        "def run(ctx):\n    return 'PROMPT:' + ctx['message']\n",
        result_mode="to_model",
    ).json()["id"]
    _bind_user(client, oh, aid2, u.id)
    r = client.post(
        f"/api/message-actions/{aid2}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "prompt"
    assert r.json()["prompt"] == "PROMPT:HELLO"


# ── 23. Non-str return → 502 ─────────────────────────────────────────────────


def test_23_non_str_return(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ex23o", role="owner")
    u, uh = _auth(client, db, "ex23u", role="user")
    aid = _create_exec(
        client, oh, "nonstr23", "def run(ctx):\n    return 123\n"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "動作回傳值必須是字串"


# ── 24. Over-cap truncated ───────────────────────────────────────────────────


def test_24_output_truncated(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    monkeypatch.setattr(settings, "ANILA_ACTION_OUTPUT_MAX_CHARS", 50)
    _, oh = _auth(client, db, "ex24o", role="owner")
    u, uh = _auth(client, db, "ex24u", role="user")
    aid = _create_exec(
        client,
        oh,
        "cap24",
        "def run(ctx):\n    return 'X' * 200\n",
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["truncated"] is True
    assert len(data["output"]) == 50


# ── 25. Exception → 502 + traceback in audit only ────────────────────────────


def test_25_exception_502_traceback_audit(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ex25o", role="owner")
    u, uh = _auth(client, db, "ex25u", role="user")
    aid = _create_exec(
        client,
        oh,
        "boom25",
        "def run(ctx):\n    raise RuntimeError('secret-boom')\n",
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert "動作執行失敗（代號 " in detail
    assert "secret-boom" not in detail
    assert "RuntimeError" not in detail
    assert "Traceback" not in detail

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_exec_result")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.status == "failure"
    meta = json.loads(row.metadata_json)
    assert meta["error_type"] == "RuntimeError"
    assert "secret-boom" in (meta.get("traceback") or "")


# ── 26. Timeout → 504 honest wording ─────────────────────────────────────────


def test_26_timeout_504(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_TIMEOUT_SECONDS", 1)
    exec_mod.reset_runtime_state_for_tests()
    _, oh = _auth(client, db, "ex26o", role="owner")
    u, uh = _auth(client, db, "ex26u", role="user")
    aid = _create_exec(
        client,
        oh,
        "sleep26",
        "def run(ctx):\n"
        "    import time\n"
        "    time.sleep(5)\n"
        "    return 'late'\n",
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 504, r.text
    assert r.json()["detail"] == (
        "動作執行逾時（1 秒），已停止等待；背景可能仍在執行"
    )
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_exec_result")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    meta = json.loads(row.metadata_json)
    assert meta["error_type"] == "timeout"


# ── 27. Write-ahead survives SystemExit from runner ──────────────────────────


def test_27_write_ahead_survives_systemexit(
    client: TestClient, db: Session, monkeypatch
):
    """Write-ahead commit must survive runner raising SystemExit.

    Call the service directly — TestClient/ASGI can hang or abort the
    pytest process on SystemExit bubbling through the transport.
    """
    import asyncio

    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ex27o", role="owner")
    u, uh = _auth(client, db, "ex27u", role="user")
    aid = _create_exec(
        client, oh, "sysex27", "def run(ctx):\n    return 'ok'\n"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)

    async def _boom(*a, **k):
        raise SystemExit("forced")

    monkeypatch.setattr(exec_mod, "run_action", _boom)
    cid, mid = _conv_assistant(client, uh)
    before = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    with pytest.raises(SystemExit):
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
    # Write-ahead committed before runner — invoke row still present.
    # Use a fresh query against the same engine (commit was on `db`).
    db.expire_all()
    after = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .count()
    )
    assert after == before + 1


# ── 28. Compiled cache invalidates on version bump ───────────────────────────


def test_28_compiled_cache_invalidates(client: TestClient, db: Session, monkeypatch):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ex28o", role="owner")
    u, uh = _auth(client, db, "ex28u", role="user")
    aid = _create_exec(
        client, oh, "cache28", "def run(ctx):\n    return 'v1'\n"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)

    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200
    assert r.json()["output"] == "v1"
    assert (aid, 1) in exec_mod._COMPILED_CACHE

    r = client.put(
        f"/api/message-actions/{aid}",
        json={"body": "def run(ctx):\n    return 'v2'\n"},
        headers=oh,
    )
    assert r.status_code == 200
    assert r.json()["version"] == 2
    # Old version key gone after invalidate_cache
    assert (aid, 1) not in exec_mod._COMPILED_CACHE

    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200
    assert r.json()["output"] == "v2"
    assert (aid, 2) in exec_mod._COMPILED_CACHE


# ── 29. Timeout holds slot → next invoke 503 while abandoned worker runs ─────


def test_29_timeout_holds_slot_second_invoke_503(
    client: TestClient, db: Session, monkeypatch
):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_MAX_CONCURRENCY", 1)
    exec_mod.reset_runtime_state_for_tests()
    _, oh = _auth(client, db, "ex29o", role="owner")
    u, uh = _auth(client, db, "ex29u", role="user")
    aid = _create_exec(
        client,
        oh,
        "sleep29",
        "def run(ctx):\n"
        "    import time\n"
        "    time.sleep(30)\n"
        "    return 'late'\n",
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)
    r1 = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r1.status_code == 504, r1.text
    assert exec_mod._ABANDONED_WORKERS >= 1
    r2 = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r2.status_code == 503, r2.text
    assert r2.json()["detail"] == "平台忙碌中，請稍後再試"


# ── 30. exec invoke audit omits rendered_prompt_* ────────────────────────────


def test_30_exec_invoke_audit_omits_rendered_prompt(
    client: TestClient, db: Session, monkeypatch
):
    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    _, oh = _auth(client, db, "ex30o", role="owner")
    u, uh = _auth(client, db, "ex30u", role="user")
    aid = _create_exec(
        client, oh, "aud30", "def run(ctx):\n    return 'ok'\n"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)
    r = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200, r.text
    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "message_action_invoke")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None
    meta = json.loads(row.metadata_json)
    assert "rendered_prompt_sha256" not in meta
    assert "rendered_prompt_length" not in meta


# ── 31. sys.exit releases slot; abandoned stays accurate; next invoke ok ─────


def test_31_sys_exit_releases_slot_next_invoke_ok(
    client: TestClient, db: Session, monkeypatch
):
    """Worker SystemExit must return the slot (not leave abandoned=0 leak).

    Call the service directly — TestClient/ASGI can hang on SystemExit.
    """
    import asyncio

    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_MAX_CONCURRENCY", 1)
    exec_mod.reset_runtime_state_for_tests()
    _, oh = _auth(client, db, "ex31o", role="owner")
    u, uh = _auth(client, db, "ex31u", role="user")
    aid_bad = _create_exec(
        client,
        oh,
        "sysex31",
        "def run(ctx):\n"
        "    import sys\n"
        "    sys.exit(1)\n",
    ).json()["id"]
    aid_ok = _create_exec(
        client, oh, "ok31", "def run(ctx):\n    return 'after'\n"
    ).json()["id"]
    _bind_user(client, oh, aid_bad, u.id)
    _bind_user(client, oh, aid_ok, u.id)
    cid, mid = _conv_assistant(client, uh)

    with pytest.raises(SystemExit):
        asyncio.run(
            svc.invoke_action(
                db,
                action_id=aid_bad,
                conversation_id=cid,
                message_id=mid,
                choice_id=None,
                user_input=None,
                actor=u,
            )
        )

    assert exec_mod._ABANDONED_WORKERS == 0
    assert exec_mod._semaphore()._value == 1

    r = client.post(
        f"/api/message-actions/{aid_ok}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r.status_code == 200, r.text
    assert r.json()["output"] == "after"


# ── 32. Compile failure after acquire returns the slot ───────────────────────


def test_32_compile_failure_releases_slot(
    client: TestClient, db: Session, monkeypatch
):
    """Tampered unparsable body at invoke must not permanently burn a slot."""
    from app.models.message_action import MessageAction

    monkeypatch.setattr(settings, "ANILA_ENABLE_ACTION_EXEC", True)
    monkeypatch.setattr(settings, "ANILA_ACTION_EXEC_MAX_CONCURRENCY", 1)
    exec_mod.reset_runtime_state_for_tests()
    _, oh = _auth(client, db, "ex32o", role="owner")
    u, uh = _auth(client, db, "ex32u", role="user")
    aid = _create_exec(
        client, oh, "tamper32", "def run(ctx):\n    return 'ok'\n"
    ).json()["id"]
    _bind_user(client, oh, aid, u.id)
    cid, mid = _conv_assistant(client, uh)

    # Mimic out-of-band DB tampering: invalidate cache, store unparsable source.
    exec_mod.invalidate_cache(aid)
    row = db.query(MessageAction).filter(MessageAction.id == aid).one()
    row.body = "def run(ctx):\n    return (('unclosed\n"
    db.commit()

    r1 = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r1.status_code == 502, r1.text
    assert exec_mod._ABANDONED_WORKERS == 0
    assert exec_mod._semaphore()._value == 1

    # Restore a valid body so the next invoke can succeed (slot must be free).
    exec_mod.invalidate_cache(aid)
    row = db.query(MessageAction).filter(MessageAction.id == aid).one()
    row.body = "def run(ctx):\n    return 'recovered'\n"
    row.version = row.version + 1
    db.commit()

    r2 = client.post(
        f"/api/message-actions/{aid}/invoke",
        json={"conversation_id": cid, "message_id": mid},
        headers=uh,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["output"] == "recovered"
