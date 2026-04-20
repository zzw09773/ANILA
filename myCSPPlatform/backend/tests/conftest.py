"""Shared test fixtures for myCSPPlatform backend tests.

Uses SQLite in-memory so tests have no external dependency on Postgres.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DEBUG"] = "false"
os.environ["DATABASE_URL"] = "sqlite:///./.pytest-csp.db"
os.environ["HEALTH_CHECK_INTERVAL"] = "3600"
os.environ.setdefault("AUTO_REGISTER_MODELS", "")
os.environ.setdefault("AUTO_REGISTER_AGENTS", "")
os.environ.setdefault("AUTO_SEED_API_KEYS", "")
os.environ.setdefault("AUTO_REGISTER_LINKS", "")

from app.database import Base, get_db
from app.main import app
from app.models.user import User
from app.models.model_registry import ModelRegistry
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.agent import Agent
from app.utils.security import hash_password


TEST_DB_URL = "sqlite://"


@pytest.fixture(scope="function")
def db_engine():
    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def db(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function")
def client(db_engine):
    """TestClient with overridden DB dependency."""
    Session = sessionmaker(bind=db_engine)

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


# ── Fixture helpers ────────────────────────────────────────────────────────────

def make_user(db, username="alice", role="user", is_approved=True) -> User:
    u = User(
        username=username,
        hashed_password=hash_password("password"),
        role=role,
        is_active=True,
        is_approved=is_approved,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def make_model(db, name="gpt-4o-mini") -> ModelRegistry:
    m = ModelRegistry(
        name=name,
        display_name=name,
        model_type="llm",
        endpoint_url="http://mock-llm:8080",
        is_active=True,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def make_agent(db, owner: User, name="test-agent",
               approval_status="pending") -> Agent:
    a = Agent(
        name=name,
        owner_user_id=owner.id,
        endpoint_url="http://agent:9100",
        description_for_router="A test agent",
        approval_status=approval_status,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def make_api_key(db, user: User, raw_key: str = "sk-test-key") -> ApiKey:
    from hashlib import sha256
    key_obj = ApiKey(
        user_id=user.id,
        key_hash=sha256(raw_key.encode()).hexdigest(),
        key_prefix=raw_key[:8],
        key_suffix=raw_key[-4:],
        name="test-key",
        is_active=True,
    )
    db.add(key_obj)
    db.commit()
    db.refresh(key_obj)
    return key_obj


def login(client, username="alice", password="password") -> str:
    resp = client.post("/api/auth/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]
