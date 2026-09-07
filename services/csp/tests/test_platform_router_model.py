"""anila-router is a platform primitive, not an admin-registered LLM.

The shell default target is ``anila-router``. When that request lands on
CSP ``/v1/chat/completions`` (cookie same-origin ``/v1``, not ``/router``)
``_resolve_model`` 404s unless a registry row exists. Operators have been
creating that row by hand after every enable. Seed it on boot; keep the
admin from being able to delete the name out of existence.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.models.model_registry import ModelRegistry
from app.services.api_key_service import check_model_permission
from app.services.auto_seed import (
    PLATFORM_ROUTER_ENDPOINT,
    PLATFORM_ROUTER_NAME,
    ensure_platform_router_model,
)
from tests.conftest import make_user


def test_ensure_creates_anila_router_when_absent(db: Session):
    assert (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == PLATFORM_ROUTER_NAME)
        .first()
        is None
    )
    row = ensure_platform_router_model(db)
    db.commit()
    assert row.name == "anila-router"
    assert row.endpoint_url == PLATFORM_ROUTER_ENDPOINT
    assert row.is_active is True
    assert row.is_internal is True
    assert row.is_router_primary is False


def test_ensure_reactivates_disabled_row_without_moving_endpoint(db: Session):
    existing = ModelRegistry(
        name="anila-router",
        display_name="old label",
        model_type="llm",
        endpoint_url="http://router-override:9000",
        is_active=False,
        is_internal=False,
    )
    db.add(existing)
    db.commit()

    row = ensure_platform_router_model(db)
    db.commit()
    assert row.id == existing.id
    assert row.is_active is True
    assert row.is_internal is True
    assert row.endpoint_url == "http://router-override:9000"


def test_regular_user_may_use_anila_router_without_a_grant(db: Session):
    user = make_user(db, username="regular")
    row = ensure_platform_router_model(db)
    db.commit()
    assert check_model_permission(
        db, user=user, api_key_id=None, model_id=row.id
    )


def test_compose_keeps_router_on_the_ssrf_allow_list():
    platform = Path(__file__).resolve().parents[3] / "infra" / "compose" / "platform.yml"
    text = platform.read_text()
    assert ",docling,router" in text or ",router,docling" in text or ",router" in text
    # Must sit outside ${ANILA_TRUSTED_HOSTS:-...} so an operator override
    # cannot drop the compose-internal service name (same shape as docling).
    assert "},docling,router" in text or "},router" in text
