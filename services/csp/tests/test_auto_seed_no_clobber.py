"""OE-2 B3 — env seeds a model once; after that the row belongs to the admin.

auto_seed used to reassign endpoint_url on every startup for any model whose
name appears in AUTO_REGISTER_MODELS. An admin who changed an endpoint in the
console saw it save, and saw it silently revert on the next deploy — the same
silent-revert family as the rest of docs/FAKE-CONTROLS.md, and harder to catch
because the revert happens hours later during an unrelated restart.
"""
from __future__ import annotations

import json

from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services import auto_seed
from app.utils.security import hash_password


class _KeepOpen:
    """auto_seed() closes its session; the pytest fixture still needs it."""

    def __init__(self, session):
        self._session = session

    def close(self):
        return None

    def __getattr__(self, name):
        return getattr(self._session, name)


def _run_seed(db, monkeypatch, models: list[dict]) -> None:
    monkeypatch.setattr(auto_seed, "SessionLocal", lambda: _KeepOpen(db))
    monkeypatch.setattr(auto_seed, "_parse_model_env_vars", lambda: [])
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_MODELS",
        json.dumps(models),
    )
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_AGENTS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    auto_seed.auto_seed()


def test_seed_does_not_reassign_endpoint_url_for_existing_models(db, monkeypatch):
    existing = ModelRegistry(
        name="nv-embed",
        display_name="nv-embed",
        model_type="embedding",
        endpoint_url="http://admin-chosen:8000/v1",
        is_active=True,
    )
    db.add(existing)
    db.commit()

    _run_seed(
        db,
        monkeypatch,
        [
            {
                "name": "nv-embed",
                "display_name": "env label",
                "model_type": "embedding",
                "endpoint_url": "http://env-seed:8000/v1",
            }
        ],
    )

    row = db.query(ModelRegistry).filter(ModelRegistry.name == "nv-embed").one()
    assert row.endpoint_url == "http://admin-chosen:8000/v1", (
        "auto_seed must not overwrite an existing model's endpoint_url — "
        "env creates the row, the admin owns it afterwards"
    )
    assert row.display_name == "nv-embed"


def _run_agent_seed(db, monkeypatch, agents: list[dict]) -> None:
    monkeypatch.setattr(auto_seed, "SessionLocal", lambda: _KeepOpen(db))
    monkeypatch.setattr(auto_seed, "_parse_model_env_vars", lambda: [])
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_MODELS", "")
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_AGENTS",
        json.dumps(agents),
    )
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    auto_seed.auto_seed()


def test_seed_does_not_clobber_existing_agents(db, monkeypatch):
    """AUTO_REGISTER_AGENTS creates missing rows only.

    An existing agent's description_for_router, approval_status,
    health_status and approved_by belong to the admin after the first
    insert. A later boot must not write the env values back.
    """
    owner = User(
        username="agent-owner",
        hashed_password=hash_password("password"),
        role="developer",
        is_active=True,
    )
    approver = User(
        username="approver",
        hashed_password=hash_password("password"),
        role="admin",
        is_active=True,
    )
    db.add_all([owner, approver])
    db.commit()
    db.add(
        Agent(
            name="kept-agent",
            owner_user_id=owner.id,
            endpoint_url="http://admin-chosen:9100",
            description_for_router="管理員寫的描述",
            approval_status="registered",
            health_status="healthy",
            approved_by=approver.id,
        )
    )
    db.commit()

    _run_agent_seed(
        db,
        monkeypatch,
        [
            {
                "name": "kept-agent",
                "endpoint_url": "http://env-seed:9100",
                "description_for_router": "from env",
                "approval_status": "approved",
                "health_status": "unknown",
                "owner_username": "agent-owner",
            }
        ],
    )

    row = db.query(Agent).filter(Agent.name == "kept-agent").one()
    assert row.description_for_router == "管理員寫的描述"
    assert row.approval_status == "registered"
    assert row.health_status == "healthy"
    assert row.approved_by == approver.id
    assert row.endpoint_url == "http://admin-chosen:9100"


def test_seed_still_creates_missing_agents(db, monkeypatch):
    """The other half: env must still insert an agent that is absent."""
    _run_agent_seed(
        db,
        monkeypatch,
        [
            {
                "name": "brand-new-agent",
                "endpoint_url": "http://env-seed:9100",
                "description_for_router": "from seed",
                "approval_status": "approved",
                "health_status": "unknown",
                "api_version": "v1",
            }
        ],
    )

    row = db.query(Agent).filter(Agent.name == "brand-new-agent").one()
    assert row.endpoint_url == "http://env-seed:9100"
    assert row.description_for_router == "from seed"
    assert row.approval_status == "approved"
    assert row.health_status == "unknown"
    assert row.approved_by is not None


def test_seed_still_creates_missing_models(db, monkeypatch):
    """The other half: env must still be able to seed a model that is absent."""
    _run_seed(
        db,
        monkeypatch,
        [
            {
                "name": "brand-new-llm",
                "display_name": "Brand New",
                "model_type": "llm",
                "endpoint_url": "http://env-seed:8000/v1",
                "api_version": "v1",
                "description": "from seed",
                "context_window": 8192,
            }
        ],
    )

    row = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == "brand-new-llm")
        .one()
    )
    assert row.endpoint_url == "http://env-seed:8000/v1"
    assert row.display_name == "Brand New"
    assert row.model_type == "llm"
    assert row.context_window == 8192


def test_skip_reason_tells_deactivated_apart_from_unregistered():
    """A deactivated model must not be reported as unregistered.

    Before seeding filtered on ``is_active`` a deactivated model resolved
    normally and never reached the "cannot grant" branch. The filter is what
    made it reachable, so the message has to say which of the two happened —
    otherwise the reader hunts a registration problem that does not exist.
    """
    inactive = {"switched-off-llm"}

    assert auto_seed.seed_model_skip_reason("switched-off-llm", inactive) == "已停用"
    assert auto_seed.seed_model_skip_reason("never-registered", inactive) == "未註冊"
    # Two distinct rows, distinct identifiers: a single-row fixture would pass
    # even if the function ignored its argument and returned a constant.
    assert auto_seed.seed_model_skip_reason("another-absent", inactive) == "未註冊"
