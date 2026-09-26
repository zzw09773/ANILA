"""既有 image-generator agent 必須在遷移裡退役，不能只靠清空種子名單。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from app.models.agent import ApiKeyAgentPermission, UserAgentPermission
from tests.conftest import make_agent, make_api_key, make_user

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/r1_0051_retire_image_generator.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("r1_0051_retire", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retire_image_generator_disables_row_and_revokes_permissions(db):
    owner = make_user(db, username="flux-owner", role="admin")
    retired = make_agent(
        db, owner, name="image-generator", approval_status="approved"
    )
    kept = make_agent(db, owner, name="kept-agent", approval_status="approved")
    user = make_user(db, username="flux-user")
    db.add(UserAgentPermission(user_id=user.id, agent_id=retired.id))
    db.add(UserAgentPermission(user_id=user.id, agent_id=kept.id))
    key = make_api_key(db, user, raw_key="sk-image-generator-key")
    db.add(ApiKeyAgentPermission(api_key_id=key.id, agent_id=retired.id))
    db.commit()

    module = _migration()
    module.retire_image_generator(db.connection())
    module.retire_image_generator(db.connection())
    db.commit()
    db.expire_all()

    db.refresh(retired)
    db.refresh(kept)
    assert retired.approval_status == "disabled"
    assert retired.unavailable_reason == "retired"
    assert kept.approval_status == "approved"
    assert kept.unavailable_reason is None
    assert (
        db.query(UserAgentPermission)
        .filter(UserAgentPermission.agent_id == retired.id)
        .count()
        == 0
    )
    assert (
        db.query(ApiKeyAgentPermission)
        .filter(ApiKeyAgentPermission.agent_id == retired.id)
        .count()
        == 0
    )
    assert (
        db.query(UserAgentPermission)
        .filter(UserAgentPermission.agent_id == kept.id)
        .count()
        == 1
    )
