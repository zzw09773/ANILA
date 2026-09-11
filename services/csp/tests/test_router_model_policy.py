"""Router model grants: campus / department / group / user, no primary bypass."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.model_access_group import ModelAccessGroup, ModelAccessGroupMember
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.services.api_key_service import check_model_permission
from app.services.router_model_policy import (
    RouterModelPolicyError,
    list_router_models_for_user,
    resolve_router_model,
    user_can_use_router_model,
)
from tests.conftest import make_model, make_user


def _enable_router(model: ModelRegistry, db: Session, *, primary: bool = False) -> ModelRegistry:
    model.router_enabled = True
    model.model_type = "llm"
    model.is_active = True
    if primary:
        model.is_router_primary = True
    db.commit()
    db.refresh(model)
    return model


def _grant(db: Session, model: ModelRegistry, **kwargs) -> RouterModelGrant:
    row = RouterModelGrant(model_id=model.id, **kwargs)
    db.add(row)
    db.commit()
    return row


def test_campus_grant_allows_active_user(db: Session):
    user = make_user(db, username="campus-user")
    model = _enable_router(make_model(db, name="glm-campus"), db)
    _grant(db, model, scope_type="all")
    assert user_can_use_router_model(db, user, model)
    names = {m.name for m in list_router_models_for_user(db, user)}
    assert "glm-campus" in names


def test_department_grant_does_not_include_children_by_default(db: Session):
    parent = Department(name="院部-父")
    db.add(parent)
    db.flush()
    child = Department(name="所-子", parent_id=parent.id)
    db.add(child)
    db.commit()
    parent_user = make_user(db, username="dept-parent", department_id=parent.id)
    child_user = make_user(db, username="dept-child", department_id=child.id)
    model = _enable_router(make_model(db, name="qwen-dept"), db)
    _grant(db, model, scope_type="department", department_id=parent.id, include_descendants=False)
    assert user_can_use_router_model(db, parent_user, model)
    assert not user_can_use_router_model(db, child_user, model)


def test_department_grant_with_descendants(db: Session):
    parent = Department(name="院部-含下")
    db.add(parent)
    db.flush()
    child = Department(name="所-含下", parent_id=parent.id)
    db.add(child)
    db.commit()
    child_user = make_user(db, username="dept-desc", department_id=child.id)
    model = _enable_router(make_model(db, name="qwen-desc"), db)
    _grant(db, model, scope_type="department", department_id=parent.id, include_descendants=True)
    assert user_can_use_router_model(db, child_user, model)


def test_group_grant(db: Session):
    user = make_user(db, username="group-user")
    outsider = make_user(db, username="group-out")
    model = _enable_router(make_model(db, name="glm-group"), db)
    group = ModelAccessGroup(name="研發群組", is_active=True)
    db.add(group)
    db.flush()
    db.add(ModelAccessGroupMember(group_id=group.id, user_id=user.id))
    db.commit()
    _grant(db, model, scope_type="group", group_id=group.id)
    assert user_can_use_router_model(db, user, model)
    assert not user_can_use_router_model(db, outsider, model)


def test_user_grant_expiry(db: Session):
    user = make_user(db, username="exp-user")
    model = _enable_router(make_model(db, name="glm-exp"), db)
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    grant = _grant(db, model, scope_type="user", user_id=user.id, expires_at=past)
    assert not user_can_use_router_model(db, user, model)
    grant.expires_at = future
    db.commit()
    assert user_can_use_router_model(db, user, model)


def test_disabled_model_loses_access_despite_campus_grant(db: Session):
    user = make_user(db, username="off-user")
    model = _enable_router(make_model(db, name="glm-off"), db)
    _grant(db, model, scope_type="all")
    model.is_active = False
    db.commit()
    assert not user_can_use_router_model(db, user, model)


def test_router_primary_no_longer_bypasses_regular_user(db: Session):
    user = make_user(db, username="bypass-user")
    model = _enable_router(make_model(db, name="glm-primary"), db, primary=True)
    assert not user_can_use_router_model(db, user, model)
    assert not check_model_permission(db, user=user, api_key_id=None, model_id=model.id)


def test_resolve_requires_default_when_unspecified(db: Session):
    user = make_user(db, username="no-default")
    model = _enable_router(make_model(db, name="glm-nd"), db)
    _grant(db, model, scope_type="all")
    with pytest.raises(RouterModelPolicyError) as exc:
        resolve_router_model(db, user)
    assert exc.value.status_code == 409


def test_platform_entry_cannot_be_requested_as_base(db: Session):
    user = make_user(db, username="entry-user")
    with pytest.raises(RouterModelPolicyError) as exc:
        resolve_router_model(db, user, requested_name="anila-router")
    assert exc.value.status_code in (403, 409)
