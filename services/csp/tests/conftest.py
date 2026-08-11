"""Shared test fixtures for myCSPPlatform backend tests.

Uses SQLite in-memory so tests have no external dependency on Postgres.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Per-session throwaway file DB for ``SessionLocal`` (headers.py 等直接開
# session、繞過 fixture override 的路徑)。以前釘 ``./.pytest-csp.db``(相對
# cwd)—— 全套跑過留下 schema 的目錄會綠、乾淨目錄 solo 跑就
# ``no such table``。路徑與生命週期改成 session 專用臨時檔;pin 機制保留。
_TEST_DB_DIR = tempfile.mkdtemp(prefix="pytest-csp-")
_TEST_DB_PATH = Path(_TEST_DB_DIR) / "pytest-csp.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
os.environ.setdefault("AUTO_REGISTER_MODELS", "")
os.environ.setdefault("AUTO_REGISTER_AGENTS", "")
os.environ.setdefault("AUTO_SEED_API_KEYS", "")
# anila-core 的 credential_crypto 直接讀 ``os.environ["SECRET_KEY"]``,沒設就
# RuntimeError。殼裡 export 過的人看到 test_agent_credentials 全綠、沒 export 的
# 人看到 12 紅 —— 同一份碼兩種答案。在這裡釘死,讓「跑之前有沒有先設環境變數」
# 不再是測試結果的變因。值必須**不在** ``startup_security._KNOWN_DEFAULTS`` 內,
# 否則 dev-default 閘門會擋下 lifespan。這不是祕密,只是固定的測試用字串。
os.environ.setdefault(
    "SECRET_KEY", "pytest-fixed-not-a-real-secret-0123456789abcdef"
)
# ``app.main.lifespan`` 會呼叫 ``assert_no_dev_defaults()``,ADMIN_PASSWORD 之類
# 還是 dev 預設值,沒開這個 flag 就 RuntimeError → 每一支用 ``client`` fixture 的
# 測試都在 setup 炸掉。以前它是靠 ``test_token_revoke_publish.py`` import 期的
# ``os.environ.setdefault`` 副作用「順便」被設起來的 —— 也就是說跑全套會綠、
# 只挑幾個檔跑就整批 error。答案取決於你選了哪些檔,那不是基準線。搬到這裡。
# ``test_startup_security`` 要測 production 行為時會自己 ``monkeypatch.delenv``。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")
# ``app.config.Settings`` 的 ``env_file=".env"`` 是相對 cwd 解析的；硬設
# 單一 auth mode 讓測試不受工作目錄或本機 .env 影響。
os.environ["ANILA_AUTH_MODE"] = "password"

from app.database import Base, engine as _session_local_engine, get_db
from app.main import app
from app.middleware import cookies as cookie_module
from app.services import ingestion_pool
from app.models.user import User
from app.models.model_registry import ModelRegistry
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.agent import Agent
from app.utils.security import hash_password

# ``SessionLocal`` 綁的是上面的臨時檔 DB;fixture 的 in-memory engine 是另一條。
# 直接 ``SessionLocal()`` 的 production 路徑(如 proxy/headers.py)需要 schema
# 已在,不能等「某個用過 TestClient 的測試碰巧跑過 lifespan create_all」。
# 必須在 model import(含 ``app.main`` 帶進來的)之後才 create_all。
Base.metadata.create_all(bind=_session_local_engine)

def _cleanup_test_db_dir() -> None:
    shutil.rmtree(_TEST_DB_DIR, ignore_errors=True)


atexit.register(_cleanup_test_db_dir)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    _cleanup_test_db_dir()


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
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function")
def client(db_engine, monkeypatch):
    """TestClient with overridden DB dependency."""
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)

    async def _skip_network_ingestion_pool():
        """Keep unit TestClient startup from dialing the real Postgres host."""
        return None

    monkeypatch.setattr(ingestion_pool, "open_pool", _skip_network_ingestion_pool)
    # TestClient uses http://testserver. Production cookies remain Secure;
    # this scoped harness seam keeps only the HTTP fixture able to send them
    # back without replacing the production helper for every test.
    monkeypatch.setattr(cookie_module, "_cookie_secure", lambda: False)

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

def make_user(db, username="alice", role="user", is_approved=True, department_id=None) -> User:
    u = User(
        username=username,
        hashed_password=hash_password("password"),
        role=role,
        is_active=True,
        is_approved=is_approved,
        department_id=department_id,
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
               approval_status="registered") -> Agent:
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
