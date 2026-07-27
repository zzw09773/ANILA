# ruff: noqa: E402
"""Shared test fixtures for myCSPPlatform backend tests.

Uses SQLite in-memory so tests have no external dependency on Postgres.
"""

from __future__ import annotations

import os
import atexit
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_TEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="anila-csp-tests-"))
atexit.register(shutil.rmtree, _TEST_RUNTIME_DIR, ignore_errors=True)
_TEST_DB_PATH = _TEST_RUNTIME_DIR / "csp.db"

os.environ["DEBUG"] = "false"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH.as_posix()}"
os.environ["HEALTH_CHECK_INTERVAL"] = "3600"
# The SQLite unit fixture owns schema setup through Base.metadata.create_all.
# Formal Alembic startup is exercised by focused startup tests, not every
# TestClient instance. The dev opt-in is required so production cannot use the
# skip flag as a migration bypass.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")
os.environ.setdefault("ANILA_DEPLOYMENT_PROFILE", "test")
os.environ.setdefault("SKIP_STARTUP_MIGRATIONS", "true")
# TestClient uses http://testserver — Secure __Host- cookies would be dropped.
# This selects the distinct anila_dev_* names; production never accepts the
# old unprefixed names as a fallback.
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("AUTO_REGISTER_MODELS", "")
os.environ.setdefault("AUTO_REGISTER_AGENTS", "")
os.environ.setdefault("AUTO_SEED_API_KEYS", "")
os.environ.setdefault("AUTO_REGISTER_LINKS", "")
# JWT keys: auto-generate dev RSA pair if missing, so CI / fresh clones
# don't have to manually run scripts/generate-jwt-keypair.py first. Never put
# these keys under the repository: ignored files still leak into Docker build
# contexts and local multi-user ACLs.
_TEST_JWT_DIR = _TEST_RUNTIME_DIR / "jwt"
_TEST_JWT_DIR.mkdir()
os.environ.setdefault("JWT_PRIVATE_KEY_PATH", str(_TEST_JWT_DIR / "jwt-private.pem"))
os.environ.setdefault("JWT_PUBLIC_KEY_PATH", str(_TEST_JWT_DIR / "jwt-public.pem"))
os.environ.setdefault("ALLOW_AUTO_KEYGEN", "true")

from app.database import Base, engine as app_engine, get_db
from app.main import app
from app.models.user import User
from app.models.model_registry import ModelRegistry
from app.models.api_key import ApiKey
from app.models.agent import Agent
from app.utils.security import hash_password


TEST_DB_URL = "sqlite://"


@pytest.fixture(scope="session", autouse=True)
def global_test_database_schema():
    """Initialize out-of-request SessionLocal users on an isolated test DB.

    Request handlers normally use the function-scoped ``get_db`` override,
    but startup hooks and proxy helpers intentionally own independent
    ``SessionLocal`` sessions. With migrations skipped in unit tests, their
    global engine still needs the ORM schema or a clean CI checkout fails with
    ``no such table`` before the security behavior can be asserted.
    """
    Base.metadata.create_all(bind=app_engine)
    yield
    app_engine.dispose()


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


# Request-scoped test sessions must mirror ``app.database.SessionLocal``.
# It sets ``expire_on_commit=False`` as an event-loop deadlock guard (see the
# comment there); a fixture that kept the SQLAlchemy default would hide both
# the bug and the fix, since ``get_db`` is overridden below.
_TEST_SESSION_KWARGS = {"expire_on_commit": False}


@pytest.fixture(scope="function")
def db(db_engine):
    Session = sessionmaker(bind=db_engine, **_TEST_SESSION_KWARGS)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function")
def client(db_engine):
    """TestClient with overridden DB dependency."""
    Session = sessionmaker(bind=db_engine, **_TEST_SESSION_KWARGS)

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


@pytest.fixture
def synthetic_card_trust(monkeypatch):
    """Trust only the ephemeral test CA and keep nonce binding enabled."""
    from app.services import card_auth
    from tests.synthetic_card_pki import SYNTHETIC_CARD, SYNTHETIC_POLICY_OID

    monkeypatch.setattr(card_auth, "_ca_anchor_cache", SYNTHETIC_CARD.anchor_cache())
    monkeypatch.setattr(card_auth, "_crl_cache", SYNTHETIC_CARD.crl_cache())
    monkeypatch.setattr(card_auth, "_crl_cache_source", None)
    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", False)
    monkeypatch.setattr(card_auth.settings, "CARD_CRL_REQUIRED", True)
    monkeypatch.setattr(
        card_auth.settings,
        "CARD_REQUIRED_CERT_POLICY_OIDS",
        SYNTHETIC_POLICY_OID,
    )
    return SYNTHETIC_CARD


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
