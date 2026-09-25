# -*- coding: utf-8 -*-
"""C 期：對話只留最新一份 compact 摘要＋邊界訊息。"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect as sa_inspect, text

from app.models.conversation import Conversation
from app.services.startup_migrations import _ensure_schema_backfills
from tests.conftest import login, make_user

CSP_ROOT = Path(__file__).resolve().parents[1]
_COMPACT_MAX = 20_000


def _headers(client, username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {login(client, username)}"}


def _open_conversation(client, headers: dict, title: str = "compact") -> dict:
    resp = client.post(
        "/api/conversations",
        headers=headers,
        json={"title": title, "origin": "anila-ui"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _append(client, headers: dict, conv_id: int, role: str, content: str) -> dict:
    resp = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers=headers,
        json={"role": role, "content": content},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _put_compact(client, headers, conv_id: int, summary: str, boundary_id: int):
    return client.put(
        f"/api/conversations/{conv_id}/compact",
        headers=headers,
        json={"summary": summary, "boundary_message_id": boundary_id},
    )


# ── PUT ──────────────────────────────────────────────────────────────────────


def test_put_compact_writes_three_fields_and_conversation_out(client, db):
    make_user(db, username="compact-owner")
    headers = _headers(client, "compact-owner")
    created = _open_conversation(client, headers)
    assert created["compact_summary"] is None
    assert created["compact_boundary_message_id"] is None
    assert created["compact_updated_at"] is None
    msg = _append(client, headers, created["id"], "user", "hello")

    resp = _put_compact(client, headers, created["id"], "  舊回合摘要  ", msg["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == created["id"]
    assert body["compact_summary"] == "舊回合摘要"
    assert body["compact_boundary_message_id"] == msg["id"]
    assert body["compact_updated_at"] is not None

    row = db.get(Conversation, created["id"])
    assert row.compact_summary == "舊回合摘要"
    assert row.compact_boundary_message_id == msg["id"]
    assert row.compact_updated_at is not None

    got = client.get(f"/api/conversations/{created['id']}", headers=headers)
    assert got.status_code == 200, got.text
    detail = got.json()
    assert detail["compact_summary"] == "舊回合摘要"
    assert detail["compact_boundary_message_id"] == msg["id"]
    assert detail["compact_updated_at"] is not None


def test_put_compact_foreign_404(client, db):
    make_user(db, username="compact-owner2")
    make_user(db, username="compact-intruder")
    owner = _headers(client, "compact-owner2")
    created = _open_conversation(client, owner)
    msg = _append(client, owner, created["id"], "user", "mine")
    denied = _put_compact(
        client, _headers(client, "compact-intruder"), created["id"], "偷寫", msg["id"],
    )
    assert denied.status_code == 404
    row = db.get(Conversation, created["id"])
    assert row.compact_summary is None


def test_put_compact_foreign_message_422(client, db):
    make_user(db, username="compact-a")
    make_user(db, username="compact-b")
    ha = _headers(client, "compact-a")
    hb = _headers(client, "compact-b")
    conv_a = _open_conversation(client, ha, title="a")
    conv_b = _open_conversation(client, hb, title="b")
    msg_b = _append(client, hb, conv_b["id"], "user", "other thread")
    resp = _put_compact(client, ha, conv_a["id"], "跨對話", msg_b["id"])
    assert resp.status_code == 422, resp.text


def test_put_compact_empty_or_whitespace_summary_422(client, db):
    make_user(db, username="compact-blank")
    headers = _headers(client, "compact-blank")
    created = _open_conversation(client, headers)
    msg = _append(client, headers, created["id"], "user", "q")
    empty = _put_compact(client, headers, created["id"], "", msg["id"])
    assert empty.status_code == 422
    blank = _put_compact(client, headers, created["id"], "   \n", msg["id"])
    assert blank.status_code == 422


def test_put_compact_overlong_summary_422(client, db):
    make_user(db, username="compact-long")
    headers = _headers(client, "compact-long")
    created = _open_conversation(client, headers)
    msg = _append(client, headers, created["id"], "user", "q")
    resp = _put_compact(client, headers, created["id"], "x" * (_COMPACT_MAX + 1), msg["id"])
    assert resp.status_code == 422


def test_put_compact_last_writer_wins_without_cas(client, db):
    make_user(db, username="compact-race")
    headers = _headers(client, "compact-race")
    created = _open_conversation(client, headers)
    first = _append(client, headers, created["id"], "user", "q1")
    second = _append(client, headers, created["id"], "assistant", "a1")
    a = _put_compact(client, headers, created["id"], "第一版", first["id"])
    b = _put_compact(client, headers, created["id"], "第二版", second["id"])
    assert a.status_code == 200, a.text
    assert b.status_code == 200, b.text
    assert b.json()["compact_summary"] == "第二版"
    assert b.json()["compact_boundary_message_id"] == second["id"]


# ── DELETE ───────────────────────────────────────────────────────────────────


def test_delete_compact_clears_three_fields(client, db):
    make_user(db, username="compact-del")
    headers = _headers(client, "compact-del")
    created = _open_conversation(client, headers)
    msg = _append(client, headers, created["id"], "user", "q")
    written = _put_compact(client, headers, created["id"], "要清掉", msg["id"])
    assert written.status_code == 200, written.text
    resp = client.delete(f"/api/conversations/{created['id']}/compact", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["compact_summary"] is None
    assert body["compact_boundary_message_id"] is None
    assert body["compact_updated_at"] is None
    row = db.get(Conversation, created["id"])
    assert row.compact_summary is None
    assert row.compact_boundary_message_id is None
    assert row.compact_updated_at is None


def test_delete_compact_foreign_404(client, db):
    make_user(db, username="compact-del-owner")
    make_user(db, username="compact-del-other")
    owner = _headers(client, "compact-del-owner")
    created = _open_conversation(client, owner)
    msg = _append(client, owner, created["id"], "user", "q")
    assert _put_compact(client, owner, created["id"], "留著", msg["id"]).status_code == 200
    denied = client.delete(
        f"/api/conversations/{created['id']}/compact",
        headers=_headers(client, "compact-del-other"),
    )
    assert denied.status_code == 404
    row = db.get(Conversation, created["id"])
    assert row.compact_summary == "留著"


def test_put_and_delete_compact_cookie_requires_csrf(client, db):
    from app.middleware.cookies import CSRF_COOKIE_NAME

    make_user(db, username="compact-cookie")
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "compact-cookie", "password": "password"},
    )
    assert login_resp.status_code == 200, login_resp.text
    csrf = client.cookies.get(CSRF_COOKIE_NAME)
    created = client.post(
        "/api/conversations",
        headers={"X-CSRF-Token": csrf},
        json={"title": "t", "origin": "anila-ui"},
    )
    assert created.status_code == 201, created.text
    conv_id = created.json()["id"]
    msg = client.post(
        f"/api/conversations/{conv_id}/messages",
        headers={"X-CSRF-Token": csrf},
        json={"role": "user", "content": "q"},
    )
    assert msg.status_code == 201, msg.text
    missing = client.put(
        f"/api/conversations/{conv_id}/compact",
        json={"summary": "摘要", "boundary_message_id": msg.json()["id"]},
    )
    assert missing.status_code == 403
    ok = client.put(
        f"/api/conversations/{conv_id}/compact",
        headers={"X-CSRF-Token": csrf},
        json={"summary": "摘要", "boundary_message_id": msg.json()["id"]},
    )
    assert ok.status_code == 200, ok.text
    missing_del = client.delete(f"/api/conversations/{conv_id}/compact")
    assert missing_del.status_code == 403
    cleared = client.delete(
        f"/api/conversations/{conv_id}/compact",
        headers={"X-CSRF-Token": csrf},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["compact_summary"] is None


# ── 子樹刪除清摘要 ───────────────────────────────────────────────────────────


def test_delete_boundary_subtree_clears_summary(client, db):
    make_user(db, username="compact-prune")
    headers = _headers(client, "compact-prune")
    created = _open_conversation(client, headers)
    cid = created["id"]
    q1 = _append(client, headers, cid, "user", "Q1")
    a1 = _append(client, headers, cid, "assistant", "A1")
    a2 = client.post(
        f"/api/conversations/{cid}/messages/{a1['id']}/branch",
        headers=headers,
        json={"role": "assistant", "content": "A2"},
    )
    assert a2.status_code == 201, a2.text
    boundary_id = a2.json()["id"]
    written = _put_compact(client, headers, cid, "含 A2 的摘要", boundary_id)
    assert written.status_code == 200, written.text

    deleted = client.delete(
        f"/api/conversations/{cid}/messages/{boundary_id}",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text

    row = db.get(Conversation, cid)
    assert row.compact_boundary_message_id is None
    assert row.compact_summary is None
    got = client.get(f"/api/conversations/{cid}", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["compact_summary"] is None
    assert got.json()["compact_boundary_message_id"] is None
    remaining = {m["id"] for m in got.json()["messages"]}
    assert q1["id"] in remaining
    assert a1["id"] in remaining
    assert boundary_id not in remaining


def test_delete_ancestor_clears_compact_when_boundary_is_descendant(client, db):
    """Boundary is a descendant of the deleted root, not the root itself."""
    make_user(db, username="compact-prune-desc")
    headers = _headers(client, "compact-prune-desc")
    created = _open_conversation(client, headers)
    cid = created["id"]
    q1 = _append(client, headers, cid, "user", "Q1")
    a1 = _append(client, headers, cid, "assistant", "A1")
    q2 = _append(client, headers, cid, "user", "Q2")
    a2 = _append(client, headers, cid, "assistant", "A2")
    written = _put_compact(client, headers, cid, "邊界在孫節點", a2["id"])
    assert written.status_code == 200, written.text
    assert written.json()["compact_boundary_message_id"] == a2["id"]
    assert a2["id"] != a1["id"]

    deleted = client.delete(
        f"/api/conversations/{cid}/messages/{a1['id']}",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.text

    row = db.get(Conversation, cid)
    assert row.compact_summary is None
    assert row.compact_boundary_message_id is None
    assert row.compact_updated_at is None
    got = client.get(f"/api/conversations/{cid}", headers=headers)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["compact_summary"] is None
    assert body["compact_boundary_message_id"] is None
    assert body["compact_updated_at"] is None
    remaining = {m["id"] for m in body["messages"]}
    assert q1["id"] in remaining
    assert {a1["id"], q2["id"], a2["id"]}.isdisjoint(remaining)


# ── 列表帶新欄 ───────────────────────────────────────────────────────────────


def test_list_conversations_includes_compact_fields(client, db):
    make_user(db, username="compact-list")
    headers = _headers(client, "compact-list")
    created = _open_conversation(client, headers, title="listed")
    msg = _append(client, headers, created["id"], "user", "q")
    assert _put_compact(client, headers, created["id"], "列表摘要", msg["id"]).status_code == 200

    listed = client.get("/api/conversations", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(item for item in listed.json() if item["id"] == created["id"])
    assert row["compact_summary"] == "列表摘要"
    assert row["compact_boundary_message_id"] == msg["id"]
    assert row["compact_updated_at"] is not None


# ── alembic / startup ────────────────────────────────────────────────────────


def test_alembic_heads_single_r1_0047():
    cfg = Config(str(CSP_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(CSP_ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    heads = list(script.get_heads())
    assert heads == ["r1_0047"], f"alembic head 應為 r1_0047，實得 {heads}"

    cli = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=CSP_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line for line in cli.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, cli.stdout
    assert "r1_0047" in cli.stdout

    head_src = (
        CSP_ROOT / "migrations" / "versions" / "r1_0047_wipe_conversation_summaries.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "r1_0047"' in head_src
    assert 'down_revision: Union[str, None] = "r1_0046"' in head_src

    text_src = (
        CSP_ROOT / "migrations" / "versions" / "r1_0043_model_registry_name_lower_unique.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "r1_0043"' in text_src
    assert 'down_revision: Union[str, None] = "r1_0042"' in text_src


def test_startup_migrations_add_compact_columns_idempotently():
    src = inspect.getsource(_ensure_schema_backfills)
    assert "compact_summary" in src
    assert "compact_boundary_message_id" in src
    assert "compact_updated_at" in src

    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE conversations (
                    id INTEGER PRIMARY KEY,
                    title VARCHAR(255) NOT NULL
                )
                """
            )
        )
        conn.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY)"))

    _ensure_schema_backfills(eng)
    _ensure_schema_backfills(eng)

    cols = {c["name"] for c in sa_inspect(eng).get_columns("conversations")}
    assert "compact_summary" in cols
    assert "compact_boundary_message_id" in cols
    assert "compact_updated_at" in cols


def test_startup_migrations_skips_existing_compact_columns_without_adding_fk():
    src = inspect.getsource(_ensure_schema_backfills)
    assert "FK 由 Alembic r1_0042 負責" in src
    assert "startup 只是欄位後援" in src

    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                """
                CREATE TABLE conversations (
                    id INTEGER PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    compact_summary TEXT,
                    compact_boundary_message_id INTEGER,
                    compact_updated_at TIMESTAMP
                )
                """
            )
        )

    before = sa_inspect(eng).get_columns("conversations")
    before_names = [c["name"] for c in before]
    assert before_names.count("compact_summary") == 1
    assert before_names.count("compact_boundary_message_id") == 1
    assert before_names.count("compact_updated_at") == 1

    _ensure_schema_backfills(eng)

    after = sa_inspect(eng).get_columns("conversations")
    after_names = [c["name"] for c in after]
    assert after_names.count("compact_summary") == 1
    assert after_names.count("compact_boundary_message_id") == 1
    assert after_names.count("compact_updated_at") == 1
    with eng.connect() as conn:
        fks = conn.execute(text("PRAGMA foreign_key_list('conversations')")).fetchall()
    assert fks == []
