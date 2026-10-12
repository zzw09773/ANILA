"""技能疊加、釘選、複製、用量與治理開關。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from anila_core.security.external_content import PRIORITY_RULE_ZH
from app.api import proxy as proxy_api
from app.middleware.caller import Caller
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.department import Department
from app.models.platform_setting import PlatformSetting, set_setting
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user_skill import UserSkillFavorite, UserSkillUsage
from app.services import user_skill_service as skills
from app.services.attachment_retention import _purge_once
from app.services.user_skill_service import (
    SKILL_LIMIT_DETAIL,
    SKILLS_DISABLED_DETAIL,
    create_skill,
    purge_expired_skill_usage,
)
from tests.conftest import login, make_model, make_user


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _payload(**overrides):
    data = {
        "name": "週報",
        "description": "整理本週重點",
        "body": "請用三點列出本週重點",
        "scope": "personal",
        "auto_apply": False,
        "submit": False,
    }
    data.update(overrides)
    return data


def _dept(db: Session, name: str) -> Department:
    row = Department(name=name, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _unit_admin(db: Session, user, department: Department, grantor) -> None:
    db.add(
        UnitAdminAssignment(
            user_id=user.id,
            department_id=department.id,
            granted_by=grantor.id,
        )
    )
    db.commit()


class _Request:
    def __init__(self, body: dict, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return dict(self._body)


def _install_upstream(monkeypatch):
    captured: dict = {"calls": 0, "bodies": []}

    async def fake_proxy_request(**kwargs):
        captured["calls"] += 1
        captured["bodies"].append(kwargs.get("request_body"))
        return {
            "choices": [{"message": {"role": "assistant", "content": "好"}}],
            "anila_meta": {},
        }

    async def no_memory(*args, **kwargs):
        return None

    monkeypatch.setattr(proxy_api, "proxy_request", fake_proxy_request)
    monkeypatch.setattr(proxy_api, "_schedule_memory_write", lambda **kwargs: None)
    monkeypatch.setattr(proxy_api, "_inject_memory", no_memory)
    return captured


async def _chat(db, user, model, *, headers=None, content="你好", extra=None):
    body = {
        "model": model.name,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
    }
    if extra:
        body.update(extra)
    return await proxy_api.chat_completions(
        _Request(body, headers),
        caller=Caller(user=user, api_key_id=None),
        db=db,
    )


def _system_text(payload: dict) -> str:
    message = payload["messages"][0]
    assert message["role"] == "system"
    return message["content"]


def _usage_rows(user_id: int) -> list[UserSkillUsage]:
    from app.database import SessionLocal

    audit_db = SessionLocal()
    try:
        rows = (
            audit_db.query(UserSkillUsage)
            .filter(UserSkillUsage.user_id == user_id)
            .order_by(UserSkillUsage.id.asc())
            .all()
        )
        audit_db.expunge_all()
        return rows
    finally:
        audit_db.close()


def _skill(db, user, *, name, body, scope="personal", department_id=None, auto_apply=False):
    return create_skill(
        db,
        user,
        name=name,
        description=f"{name}的用途",
        body=body,
        scope=scope,
        department_id=department_id,
        auto_apply=auto_apply,
        submit=False,
    )


def test_r1_0073_upgrades_and_downgrades():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    import migrations.versions.r1_0073_skill_usage_favorites as rev

    assert rev.revision == "r1_0073"
    assert rev.down_revision == "r1_0072"
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL)"
        ))
        conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'stack')"))

    def _run(direction: str) -> None:
        with engine.begin() as conn:
            context = MigrationContext.configure(conn)
            with Operations.context(context):
                getattr(rev, direction)()

    _run("upgrade")
    with engine.begin() as conn:
        usage_cols = [
            row[1] for row in conn.execute(text("PRAGMA table_info(user_skill_usage)"))
        ]
        assert usage_cols == ["id", "lineage_id", "user_id", "used_at", "mode"]
        favorite_info = list(conn.execute(text("PRAGMA table_info(user_skill_favorites)")))
        assert [row[1] for row in favorite_info] == ["user_id", "lineage_id"]
        assert [row[1] for row in favorite_info if row[5]] == ["user_id", "lineage_id"]
        indexes = {
            row[1] for row in conn.execute(text("PRAGMA index_list(user_skill_usage)"))
        }
        assert "ix_user_skill_usage_user_used" in indexes
        assert "ix_user_skill_usage_lineage" in indexes
        user_used = [
            row[2]
            for row in conn.execute(text("PRAGMA index_info(ix_user_skill_usage_user_used)"))
        ]
        assert user_used == ["user_id", "used_at"]
        lineage = [
            row[2]
            for row in conn.execute(text("PRAGMA index_info(ix_user_skill_usage_lineage)"))
        ]
        assert lineage == ["lineage_id"]
        sql = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE name = 'user_skill_usage'"
        )).scalar()
        assert "ck_user_skill_usage_mode" in (sql or "")
        conn.execute(text(
            "INSERT INTO user_skill_usage (lineage_id, user_id, used_at, mode) "
            "VALUES (3, 1, '2026-10-12 00:00:00', 'manual')"
        ))
        conn.execute(text(
            "INSERT INTO user_skill_favorites (user_id, lineage_id) VALUES (1, 3)"
        ))
        with pytest.raises(Exception):
            conn.execute(text(
                "INSERT INTO user_skill_usage (lineage_id, user_id, used_at, mode) "
                "VALUES (3, 1, '2026-10-12 00:00:00', 'other')"
            ))
    _run("downgrade")
    with engine.begin() as conn:
        names = {
            row[0]
            for row in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ))
        }
        assert "user_skill_usage" not in names
        assert "user_skill_favorites" not in names


def test_usage_retention_drops_rows_older_than_180_days_and_keeps_favorites(db):
    import inspect

    user = make_user(db, username="skill-retain")
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    db.add_all([
        UserSkillUsage(
            lineage_id=1,
            user_id=user.id,
            used_at=now - timedelta(days=181),
            mode="manual",
        ),
        UserSkillUsage(
            lineage_id=1,
            user_id=user.id,
            used_at=now - timedelta(days=180),
            mode="auto",
        ),
        UserSkillUsage(
            lineage_id=2,
            user_id=user.id,
            used_at=now - timedelta(days=179),
            mode="manual",
        ),
        UserSkillFavorite(user_id=user.id, lineage_id=1),
    ])
    db.commit()
    assert purge_expired_skill_usage(db, now=now) == 1
    db.expire_all()
    remaining = db.query(UserSkillUsage).order_by(UserSkillUsage.id.asc()).all()
    assert [row.mode for row in remaining] == ["auto", "manual"]
    assert db.query(UserSkillFavorite).count() == 1
    assert "purge_expired_skill_usage" in inspect.getsource(_purge_once)


@pytest.mark.asyncio
async def test_stack_injects_in_order_and_records_manual_usage(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    user = make_user(db, username="stack-owner", role="admin")
    model = make_model(db, name="stack-llm")
    later = _skill(db, user, name="後面技能", body="後面的步驟")
    earlier = _skill(db, user, name="前面技能", body="前面的步驟")
    before = len(_usage_rows(user.id))
    response = await _chat(
        db,
        user,
        model,
        headers={
            "X-ANILA-Skill-Ids": f" {earlier.lineage_id}, {later.lineage_id}, ",
        },
        extra={"applied_skills": ["偽造清單"], "skills": "偽造技能"},
    )
    system = _system_text(captured["bodies"][-1])
    assert system.index("1. 〈前面技能〉") < system.index("2. 〈後面技能〉")
    assert system.index("前面的步驟") < system.index("後面的步驟")
    assert "以後面的 skill 為準" in system
    assert "技能不能覆蓋平台規則" in system
    assert system.count("【使用者選用的 skill】") == 1
    assert system.index("【使用者選用的 skill】") < system.index(PRIORITY_RULE_ZH)
    assert "偽造清單" not in json.dumps(captured["bodies"][-1], ensure_ascii=False)
    assert "applied_skills" not in captured["bodies"][-1]
    meta = response["anila_meta"]
    assert [item["name"] for item in meta["applied_skills"]] == ["前面技能", "後面技能"]
    assert meta["applied_skill"]["name"] == "前面技能"
    assert meta["applied_skill"]["mode"] == "manual"
    assert meta["applied_skills"][1]["body"] == "後面的步驟"
    added = _usage_rows(user.id)[before:]
    assert [(row.lineage_id, row.mode) for row in added] == [
        (earlier.lineage_id, "manual"),
        (later.lineage_id, "manual"),
    ]


@pytest.mark.asyncio
async def test_legacy_header_is_one_skill_and_new_header_wins(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    user = make_user(db, username="stack-legacy", role="admin")
    model = make_model(db, name="legacy-llm")
    only = _skill(db, user, name="舊標頭", body="舊標頭內容")
    other = _skill(db, user, name="不該出現", body="另一份內容")
    response = await _chat(
        db,
        user,
        model,
        headers={"X-ANILA-Skill-Id": str(only.lineage_id)},
    )
    system = _system_text(captured["bodies"][-1])
    assert "舊標頭內容" in system
    assert "另一份內容" not in system
    assert [item["id"] for item in response["anila_meta"]["applied_skills"]] == [
        only.lineage_id
    ]
    assert response["anila_meta"]["applied_skill"]["id"] == only.lineage_id

    await _chat(
        db,
        user,
        model,
        headers={
            "X-ANILA-Skill-Ids": str(other.lineage_id),
            "X-ANILA-Skill-Id": str(only.lineage_id),
        },
    )
    system = _system_text(captured["bodies"][-1])
    assert "另一份內容" in system
    assert "舊標頭內容" not in system


@pytest.mark.asyncio
async def test_any_unusable_skill_rejects_the_whole_stack(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    owner = make_user(db, username="stack-ok", role="admin")
    stranger = make_user(db, username="stack-secret")
    model = make_model(db, name="forbid-llm")
    mine = _skill(db, owner, name="我的", body="我的內容")
    secret = _skill(db, stranger, name="別人的", body="別人的內容")
    before = captured["calls"]
    usage_before = len(_usage_rows(owner.id))
    with pytest.raises(HTTPException) as caught:
        await _chat(
            db,
            owner,
            model,
            headers={"X-ANILA-Skill-Ids": f"{mine.lineage_id},{secret.lineage_id}"},
        )
    assert caught.value.status_code == 403
    assert captured["calls"] == before
    assert len(_usage_rows(owner.id)) == usage_before
    with pytest.raises(HTTPException) as missing:
        await _chat(
            db,
            owner,
            model,
            headers={"X-ANILA-Skill-Id": "nope"},
        )
    assert missing.value.status_code == 403


@pytest.mark.asyncio
async def test_oversized_stack_with_unusable_id_is_403(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    owner = make_user(db, username="stack-order", role="admin")
    stranger = make_user(db, username="stack-order-secret")
    model = make_model(db, name="order-llm")
    own = [
        _skill(db, owner, name=f"順序{index}", body=f"順序內容{index}")
        for index in range(3)
    ]
    secret = _skill(db, stranger, name="順序外人", body="外人內容")
    header = ",".join(str(row.lineage_id) for row in own + [secret])
    before = captured["calls"]
    with pytest.raises(HTTPException) as caught:
        await _chat(db, owner, model, headers={"X-ANILA-Skill-Ids": header})
    assert caught.value.status_code == 403
    assert caught.value.detail != SKILL_LIMIT_DETAIL
    assert captured["calls"] == before


@pytest.mark.asyncio
async def test_present_malformed_skill_ids_do_not_auto_select(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    user = make_user(db, username="stack-bad-header", role="admin")
    model = make_model(db, name="bad-header-llm")
    _skill(db, user, name="會自動", body="不該自動注入", auto_apply=True)
    conv = Conversation(user_id=user.id, title="壞標頭", origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)

    async def explode(*args, **kwargs):
        raise AssertionError("壞掉的技能標頭不該改走自動選")

    monkeypatch.setattr(skills, "_summary_complete", explode)
    before = captured["calls"]
    for raw in (",", "abc", "   "):
        with pytest.raises(HTTPException) as caught:
            await _chat(
                db,
                user,
                model,
                headers={
                    "X-ANILA-Skill-Ids": raw,
                    "X-ANILA-Conversation-Id": str(conv.id),
                },
                content="請整理週報",
            )
        assert caught.value.status_code == 403
    assert captured["calls"] == before


@pytest.mark.asyncio
async def test_over_stack_or_chars_returns_422(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    user = make_user(db, username="stack-limit", role="admin")
    model = make_model(db, name="limit-llm")
    rows = [
        _skill(db, user, name=f"技能{index}", body=f"內容{index}")
        for index in range(4)
    ]
    header = ",".join(str(row.lineage_id) for row in rows)
    before = captured["calls"]
    with pytest.raises(HTTPException) as too_many:
        await _chat(db, user, model, headers={"X-ANILA-Skill-Ids": header})
    assert too_many.value.status_code == 422
    assert too_many.value.detail == SKILL_LIMIT_DETAIL
    assert captured["calls"] == before

    pair = rows[:2]
    text = skills.skills_guidance_text(pair)
    set_setting(db, "skills.max_total_chars", len(text) - 1)
    db.commit()
    with pytest.raises(HTTPException) as too_long:
        await _chat(
            db,
            user,
            model,
            headers={"X-ANILA-Skill-Ids": f"{pair[0].lineage_id},{pair[1].lineage_id}"},
        )
    assert too_long.value.status_code == 422
    assert too_long.value.detail == SKILL_LIMIT_DETAIL
    assert captured["calls"] == before

    set_setting(db, "skills.max_total_chars", len(text))
    db.commit()
    response = await _chat(
        db,
        user,
        model,
        headers={"X-ANILA-Skill-Ids": f"{pair[0].lineage_id},{pair[1].lineage_id}"},
    )
    assert [item["name"] for item in response["anila_meta"]["applied_skills"]] == [
        "技能0",
        "技能1",
    ]


@pytest.mark.asyncio
async def test_explicit_skills_skip_auto_select_and_auto_still_picks_one(db, monkeypatch):
    captured = _install_upstream(monkeypatch)
    user = make_user(db, username="stack-auto", role="admin")
    model = make_model(db, name="auto-stack-llm")
    manual = _skill(db, user, name="手動", body="手動內容", auto_apply=False)
    auto = _skill(db, user, name="自動", body="自動內容", auto_apply=True)
    other = _skill(db, user, name="另一個自動", body="不該同時出現", auto_apply=True)
    conv = Conversation(user_id=user.id, title="疊加自動", origin="anila-ui")
    db.add(conv)
    db.commit()
    db.refresh(conv)

    async def explode(*args, **kwargs):
        raise AssertionError("明確指定時不該自動選")

    monkeypatch.setattr(skills, "_summary_complete", explode)
    await _chat(
        db,
        user,
        model,
        headers={
            "X-ANILA-Skill-Ids": str(manual.lineage_id),
            "X-ANILA-Conversation-Id": str(conv.id),
        },
        content="請整理週報",
    )
    sent = json.dumps(captured["bodies"][-1], ensure_ascii=False)
    assert "手動內容" in sent
    assert "自動內容" not in sent

    async def pick_one(db_, user_, prompt):
        assert "自動內容" not in prompt
        assert "不該同時出現" not in prompt
        return json.dumps({"id": auto.lineage_id})

    monkeypatch.setattr(skills, "_summary_complete", pick_one)
    before = len(_usage_rows(user.id))
    response = await _chat(
        db,
        user,
        model,
        headers={"X-ANILA-Conversation-Id": str(conv.id)},
        content="請整理週報",
    )
    system = _system_text(captured["bodies"][-1])
    assert "自動內容" in system
    assert "不該同時出現" not in system
    assert "手動內容" not in system
    assert [item["mode"] for item in response["anila_meta"]["applied_skills"]] == ["auto"]
    added = _usage_rows(user.id)[before:]
    assert [(row.lineage_id, row.mode) for row in added] == [(auto.lineage_id, "auto")]
    assert other.lineage_id != auto.lineage_id


@pytest.mark.asyncio
async def test_disabled_switch_ignores_headers_and_blocks_the_api(db, monkeypatch, client):
    captured = _install_upstream(monkeypatch)
    user = make_user(db, username="stack-off", role="admin")
    model = make_model(db, name="off-llm")
    skill = _skill(db, user, name="關掉", body="不該注入")
    set_setting(db, "skills.enabled", False)
    db.commit()
    response = await _chat(
        db,
        user,
        model,
        headers={"X-ANILA-Skill-Ids": f"{skill.lineage_id},999999"},
    )
    assert "不該注入" not in json.dumps(captured["bodies"][-1], ensure_ascii=False)
    assert "applied_skill" not in (response.get("anila_meta") or {})
    assert "applied_skills" not in (response.get("anila_meta") or {})
    token = login(client, "stack-off")
    denied = client.get("/api/skills", headers=_auth(token))
    assert denied.status_code == 403
    assert denied.json()["detail"] == SKILLS_DISABLED_DETAIL
    created = client.post("/api/skills", json=_payload(), headers=_auth(token))
    assert created.status_code == 403


def test_copy_requires_a_usable_unit_or_campus_skill(client, db):
    east = _dept(db, "複製東")
    west = _dept(db, "複製西")
    grantor = make_user(db, username="copy-grantor", role="admin")
    east_admin = make_user(db, username="copy-east-admin", department_id=east.id)
    east_member = make_user(db, username="copy-east", department_id=east.id)
    west_member = make_user(db, username="copy-west", department_id=west.id)
    _unit_admin(db, east_admin, east, grantor)
    admin_token = login(client, "copy-east-admin")
    east_token = login(client, "copy-east")
    west_token = login(client, "copy-west")
    created = client.post(
        "/api/skills",
        json=_payload(name="東區步驟", scope="unit", department_id=east.id, body="只給東區"),
        headers=_auth(admin_token),
    )
    assert created.status_code == 201, created.text
    lineage_id = created.json()["id"]
    denied = client.post(f"/api/skills/{lineage_id}/copy", headers=_auth(west_token))
    assert denied.status_code == 403
    copied = client.post(f"/api/skills/{lineage_id}/copy", headers=_auth(east_token))
    assert copied.status_code == 201, copied.text
    body = copied.json()
    assert body["scope"] == "personal"
    assert body["status"] == "draft"
    assert body["body"] == "只給東區"
    assert body["owner_user_id"] == east_member.id
    assert body["id"] != lineage_id
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "skill_copy", AuditLog.actor_username == "copy-east")
        .one()
    )
    meta = json.loads(audit.metadata_json)
    assert meta["source_lineage_id"] == lineage_id
    assert meta["source_scope"] == "unit"
    personal = client.post("/api/skills", json=_payload(name="自己的"), headers=_auth(east_token))
    assert personal.status_code == 201, personal.text
    own = client.post(
        f"/api/skills/{personal.json()['id']}/copy",
        headers=_auth(east_token),
    )
    assert own.status_code == 403


def test_favorite_only_changes_the_caller_and_usable_list_sorts(client, db):
    east = _dept(db, "釘選東")
    grantor = make_user(db, username="fav-grantor", role="admin")
    lead = make_user(db, username="fav-lead", department_id=east.id)
    member = make_user(db, username="fav-member", department_id=east.id)
    other = make_user(db, username="fav-other", department_id=east.id)
    _unit_admin(db, lead, east, grantor)
    lead_token = login(client, "fav-lead")
    member_token = login(client, "fav-member")
    other_token = login(client, "fav-other")
    pinned = client.post(
        "/api/skills",
        json=_payload(name="釘選", scope="unit", department_id=east.id, body="釘"),
        headers=_auth(lead_token),
    )
    recent = client.post(
        "/api/skills",
        json=_payload(name="最近", scope="unit", department_id=east.id, body="近"),
        headers=_auth(lead_token),
    )
    older = client.post(
        "/api/skills",
        json=_payload(name="較早", scope="unit", department_id=east.id, body="早"),
        headers=_auth(lead_token),
    )
    unused = client.post(
        "/api/skills",
        json=_payload(name="沒用", scope="unit", department_id=east.id, body="無"),
        headers=_auth(lead_token),
    )
    assert pinned.status_code == 201, pinned.text
    ids = {
        "釘選": pinned.json()["id"],
        "最近": recent.json()["id"],
        "較早": older.json()["id"],
        "沒用": unused.json()["id"],
    }
    now = datetime.now(timezone.utc)
    db.add_all([
        UserSkillUsage(
            lineage_id=ids["最近"],
            user_id=member.id,
            used_at=now,
            mode="manual",
        ),
        UserSkillUsage(
            lineage_id=ids["最近"],
            user_id=member.id,
            used_at=now - timedelta(days=1),
            mode="auto",
        ),
        UserSkillUsage(
            lineage_id=ids["最近"],
            user_id=member.id,
            used_at=now - timedelta(days=40),
            mode="manual",
        ),
        UserSkillUsage(
            lineage_id=ids["較早"],
            user_id=member.id,
            used_at=now - timedelta(days=2),
            mode="manual",
        ),
        UserSkillUsage(
            lineage_id=ids["釘選"],
            user_id=other.id,
            used_at=now,
            mode="manual",
        ),
    ])
    db.commit()
    starred = client.post(f"/api/skills/{ids['釘選']}/favorite", headers=_auth(member_token))
    assert starred.status_code == 204, starred.text
    again = client.post(f"/api/skills/{ids['釘選']}/favorite", headers=_auth(member_token))
    assert again.status_code == 204, again.text
    assert (
        db.query(UserSkillFavorite)
        .filter(UserSkillFavorite.user_id == member.id)
        .count()
        == 1
    )
    mine = client.get("/api/skills?view=usable", headers=_auth(member_token))
    assert mine.status_code == 200, mine.text
    ordered = [item["name"] for item in mine.json()["skills"]]
    assert ordered[:4] == ["釘選", "最近", "較早", "沒用"]
    by_name = {item["name"]: item for item in mine.json()["skills"]}
    assert by_name["釘選"]["favorite"] is True
    assert by_name["最近"]["favorite"] is False
    assert by_name["最近"]["use_count_30d"] == 2
    assert by_name["較早"]["use_count_30d"] == 1
    assert by_name["沒用"]["use_count_30d"] == 0
    assert by_name["沒用"]["last_used_at"] is None
    assert by_name["最近"]["last_used_at"] is not None
    theirs = client.get("/api/skills?view=usable", headers=_auth(other_token))
    other_pinned = next(item for item in theirs.json()["skills"] if item["name"] == "釘選")
    assert other_pinned["favorite"] is False
    assert other_pinned["use_count_30d"] == 1
    removed = client.delete(
        f"/api/skills/{ids['釘選']}/favorite",
        headers=_auth(member_token),
    )
    assert removed.status_code == 204, removed.text
    cleared = client.delete(
        f"/api/skills/{ids['釘選']}/favorite",
        headers=_auth(member_token),
    )
    assert cleared.status_code == 204
    after = client.get("/api/skills?view=usable", headers=_auth(member_token))
    first = next(item for item in after.json()["skills"] if item["name"] == "釘選")
    assert first["favorite"] is False


def test_unit_skill_stats_are_limited_to_unit_admins_and_governance_admins(client, db):
    east = _dept(db, "統計東")
    west = _dept(db, "統計西")
    grantor = make_user(db, username="stats-grantor", role="admin")
    east_lead = make_user(db, username="stats-east", department_id=east.id)
    west_lead = make_user(db, username="stats-west", department_id=west.id)
    member = make_user(db, username="stats-member", department_id=east.id)
    _unit_admin(db, east_lead, east, grantor)
    _unit_admin(db, west_lead, west, grantor)
    east_token = login(client, "stats-east")
    west_token = login(client, "stats-west")
    member_token = login(client, "stats-member")
    admin_token = login(client, "stats-grantor")
    created = client.post(
        "/api/skills",
        json=_payload(name="東區熱門", scope="unit", department_id=east.id, body="統計"),
        headers=_auth(east_token),
    )
    assert created.status_code == 201, created.text
    lineage_id = created.json()["id"]
    now = datetime.now(timezone.utc)
    db.add_all([
        UserSkillUsage(
            lineage_id=lineage_id,
            user_id=member.id,
            used_at=now - timedelta(days=1),
            mode="manual",
        ),
        UserSkillUsage(
            lineage_id=lineage_id,
            user_id=west_lead.id,
            used_at=now - timedelta(hours=2),
            mode="auto",
        ),
        UserSkillUsage(
            lineage_id=lineage_id,
            user_id=member.id,
            used_at=now - timedelta(days=40),
            mode="manual",
        ),
    ])
    db.commit()
    assert client.get(
        f"/api/skills/stats?department_id={east.id}",
        headers=_auth(member_token),
    ).status_code == 403
    assert client.get(
        f"/api/skills/stats?department_id={east.id}",
        headers=_auth(west_token),
    ).status_code == 403
    for token in (east_token, admin_token):
        allowed = client.get(
            f"/api/skills/stats?department_id={east.id}",
            headers=_auth(token),
        )
        assert allowed.status_code == 200, allowed.text
        body = allowed.json()
        assert body["department_id"] == east.id
        assert body["days"] == 30
        assert body["skills"] == [
            {
                "lineage_id": lineage_id,
                "name": "東區熱門",
                "use_count_30d": 2,
                "last_used_at": body["skills"][0]["last_used_at"],
            }
        ]
        assert body["skills"][0]["last_used_at"] is not None
    missing = client.get(
        "/api/skills/stats?department_id=999999",
        headers=_auth(admin_token),
    )
    assert missing.status_code == 404


def test_campus_publish_can_be_turned_off_without_hiding_published_skills(client, db):
    admin = make_user(db, username="campus-switch", role="admin")
    member = make_user(db, username="campus-reader")
    admin_token = login(client, "campus-switch")
    member_token = login(client, "campus-reader")
    created = client.post(
        "/api/skills",
        json=_payload(name="全院既有", scope="campus", body="仍然可用"),
        headers=_auth(admin_token),
    )
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "published"
    lineage_id = created.json()["id"]
    set_setting(db, "skills.campus_publish_enabled", False)
    db.commit()
    targets = client.get("/api/skills/publish-targets", headers=_auth(admin_token))
    assert targets.status_code == 200, targets.text
    assert targets.json()["campus"] is False
    blocked = client.post(
        "/api/skills",
        json=_payload(name="全院新的", scope="campus", body="不該建立"),
        headers=_auth(admin_token),
    )
    assert blocked.status_code == 403
    usable = client.get("/api/skills?view=usable", headers=_auth(member_token))
    assert any(item["id"] == lineage_id for item in usable.json()["skills"])


@pytest.mark.asyncio
async def test_sse_event_keeps_the_first_skill_and_lists_the_stack():
    applied = skills.AppliedSkillResult([
        skills.AppliedSkill(2, 8, 1, "第一", "甲", "manual"),
        skills.AppliedSkill(3, 9, 1, "第二", "乙", "manual"),
    ])

    async def upstream():
        yield "data: [DONE]\n\n"

    chunks = []
    async for chunk in proxy_api._sse_with_applied_skill(upstream(), applied):
        chunks.append(chunk)
    assert chunks[0].startswith("event: anila.skill\n")
    data = json.loads(chunks[0].split("data: ", 1)[1])
    assert data["name"] == "第一"
    assert data["applied_skill"]["body"] == "甲"
    assert [item["name"] for item in data["applied_skills"]] == ["第一", "第二"]
    assert chunks[1] == "data: [DONE]\n\n"


def test_skill_list_exposes_stack_limits_and_clamps_max_stack(client, db):
    from app.services.settings_registry import REGISTRY

    user = make_user(db, username="stack-limits", role="admin")
    token = login(client, "stack-limits")
    spec = REGISTRY["skills.max_stack"]
    assert spec.domain_fn(1) and spec.domain_fn(20)
    assert not spec.domain_fn(0) and not spec.domain_fn(21)
    with pytest.raises(ValueError):
        set_setting(db, "skills.max_stack", 21)
    for view in ("usable", "mine"):
        listed = client.get(f"/api/skills?view={view}", headers=_auth(token))
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["max_stack"] == 3
        assert body["max_total_chars"] == 4000
        assert "skills" in body
    set_setting(db, "skills.max_stack", 20)
    set_setting(db, "skills.max_total_chars", 1200)
    db.commit()
    raised = client.get("/api/skills?view=mine", headers=_auth(token))
    assert raised.json()["max_stack"] == 20
    assert raised.json()["max_total_chars"] == 1200
    stored = db.get(PlatformSetting, "skills.max_stack")
    stored.value = "50"
    db.commit()
    clamped = client.get("/api/skills?view=usable", headers=_auth(token))
    assert clamped.json()["max_stack"] == 20
    stored.value = "0"
    db.commit()
    assert client.get("/api/skills", headers=_auth(token)).json()["max_stack"] == 1
