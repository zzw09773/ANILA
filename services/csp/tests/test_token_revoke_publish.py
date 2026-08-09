"""Tests for the Redis pub/sub bridge that broadcasts token-revocation
events to downstream services (anila-studio cold-start sync + live
push channel).

The csp/backend is the *publisher* side of the broadcast. The DB bump
+ `token_revocations` insert are the durable source-of-truth (cold
start replays them via ``GET /api/auth/revocations?since=...``);
Redis pub/sub is best-effort low-latency notify.

Design choices pinned by these tests:

* Publish failure (Redis down, network blip, etc.) MUST NOT fail the
  HTTP response. The user's logout / password-change / admin-revoke
  request still returns 2xx because the DB write already happened —
  anila-studio will catch up on its next cold-start poll. This is the
  "csp publisher = weak guarantee" half of the R14 trade-off; the
  studio side will be fail-closed (503 if Redis unreachable).

* Publisher MUST be called AFTER the DB commit, never before. If we
  flipped the order, Redis could broadcast a revocation that ends up
  rolled back, and subscribers would over-invalidate.

* Connection is lazy-initialised — importing the publisher MUST NOT
  open a socket. Required so pytest collection on a machine with no
  Redis still works, and so we don't pin a Redis client to the wrong
  event loop.

Test strategy: monkeypatch the publisher's internal client factory so
each test gets a recording fake instead of a real Redis connection.
``fakeredis`` is not in the requirements file (avoiding a new dep);
the recording stub is enough for these contracts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any

# Same opt-in dance other client-based tests use; the startup security
# gate refuses to boot under the dev SECRET_KEY otherwise.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_user


# `test_startup_security.py` reloads `app.config` mid-suite to test the
# production-mode raises. Its monkeypatched env is restored AFTER the
# reload, so the cached `settings` global picks up *production* values
# (non-dev SECRET_KEY etc.) and subsequent tests that boot the TestClient
# lifespan would fail the dev-default gate.
#
# This autouse fixture re-applies the dev opt-in env var AND reloads
# the affected modules so our lifespan boot sees the dev-secret allow
# flag again. ``app.main.lifespan`` does a fresh ``from
# app.services.startup_security import assert_no_dev_defaults`` on
# every call so we don't need to re-patch main.
#
# 2026-07-31:teardown 補上 ``app.config.settings`` 還原。reload 會讓
# ``app.config.settings`` 變成一顆新物件,但 import 期就綁定 settings 的模組
# (``app.utils.security`` 等) 手上還是舊那顆 —— 兩顆並存,誰 monkeypatch 誰
# 就變成執行順序的函數。本 fixture 原本只補償別人的污染,自己也在製造污染。
@pytest.fixture(autouse=True)
def _ensure_dev_secret_gate(monkeypatch):
    import importlib
    import app.config as config_module
    import app.services.startup_security as ss_module

    original_settings = config_module.settings
    monkeypatch.setenv("ANILA_ALLOW_DEV_SECRET", "1")
    importlib.reload(config_module)
    importlib.reload(ss_module)
    yield
    config_module.settings = original_settings
    importlib.reload(ss_module)


# ---------------------------------------------------------------------------
# Recording fake — captures every publish(channel, message) call.
# ---------------------------------------------------------------------------


class _RecordingRedis:
    """Async stand-in for ``redis.asyncio.Redis``.

    Records every publish() call. ``subscriber_count`` mimics the real
    return value (number of subscribers that received the message). We
    return 0 by default — the publisher must NOT treat 0 as an error.
    """

    def __init__(self, subscriber_count: int = 0, raise_on_publish: BaseException | None = None):
        self.published: list[tuple[str, str]] = []
        self.subscriber_count = subscriber_count
        self.raise_on_publish = raise_on_publish
        self.closed = False

    async def publish(self, channel: str, message: str) -> int:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append((channel, message))
        return self.subscriber_count

    async def aclose(self) -> None:  # redis>=5 API
        self.closed = True

    # Older API alias some libraries also accept; harmless to expose.
    async def close(self) -> None:
        self.closed = True


class _RecordingSyncRedis:
    """Sync stand-in used by synchronous FastAPI endpoints."""

    def __init__(
        self,
        subscriber_count: int = 0,
        raise_on_publish: BaseException | None = None,
    ):
        self.published: list[tuple[str, str]] = []
        self.subscriber_count = subscriber_count
        self.raise_on_publish = raise_on_publish
        self.closed = False

    def publish(self, channel: str, message: str) -> int:
        if self.raise_on_publish is not None:
            raise self.raise_on_publish
        self.published.append((channel, message))
        return self.subscriber_count

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def recording_redis(monkeypatch) -> _RecordingRedis:
    """Install a recording fake in place of the real Redis client.

    The publisher module exposes ``_get_redis_client`` (an async
    factory) for exactly this seam. Monkeypatching the public symbol
    means the fixture works regardless of whether the publisher is
    imported through routes or directly.
    """
    fake = _RecordingRedis()

    async def _factory(redis_url: str | None = None):
        return fake

    from app.services import token_revocation_publisher

    # Wipe any cached singleton from previous tests in the same session.
    monkeypatch.setattr(token_revocation_publisher, "_client_singleton", None, raising=False)
    monkeypatch.setattr(token_revocation_publisher, "_get_redis_client", _factory)
    return fake


@pytest.fixture
def recording_sync_redis(monkeypatch) -> _RecordingSyncRedis:
    fake = _RecordingSyncRedis()

    from app.services import token_revocation_publisher

    # ⚠ ``timeout`` 是必填關鍵字（2026-08-09 起）：值由呼叫端走登錄表解析後傳進來。
    monkeypatch.setattr(
        token_revocation_publisher,
        "_make_sync_redis_client",
        lambda redis_url=None, *, timeout=None: fake,
    )
    return fake


# ---------------------------------------------------------------------------
# Direct publisher unit tests.
# ---------------------------------------------------------------------------


def test_publish_revocation_emits_expected_envelope(recording_redis):
    """Single call must produce one publish with the documented schema."""
    from app.services.token_revocation_publisher import publish_revocation

    asyncio.run(publish_revocation(user_id=42, revoked_at_version=3))

    assert len(recording_redis.published) == 1
    channel, message = recording_redis.published[0]
    assert channel == "anila:auth:token-revoke"

    payload = json.loads(message)
    assert payload["user_id"] == 42
    assert payload["revoked_at_version"] == 3
    assert payload["schema_version"] == 1
    # ts is ISO-8601 UTC, parseable.
    ts = datetime.fromisoformat(payload["ts"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None


def test_publish_revocation_swallows_redis_failure(monkeypatch, caplog):
    """Redis blowing up MUST NOT raise — that's the whole point of
    best-effort publish. The DB row is the durable record."""
    from app.services import token_revocation_publisher

    fake = _RecordingRedis(raise_on_publish=ConnectionError("redis down"))

    async def _factory(redis_url: str | None = None):
        return fake

    monkeypatch.setattr(token_revocation_publisher, "_client_singleton", None, raising=False)
    monkeypatch.setattr(token_revocation_publisher, "_get_redis_client", _factory)

    # Capture error logs but expect NO exception.
    import logging
    caplog.set_level(logging.ERROR, logger="app.services.token_revocation_publisher")
    asyncio.run(token_revocation_publisher.publish_revocation(user_id=7, revoked_at_version=1))

    # Connection error was logged but did NOT propagate.
    assert any("revoke" in rec.message.lower() or "redis" in rec.message.lower()
               for rec in caplog.records), caplog.text


def test_publish_revocation_handles_factory_failure(monkeypatch, caplog):
    """If even building the client raises (URL malformed, missing
    library, etc.), publish_revocation still returns cleanly."""
    from app.services import token_revocation_publisher

    async def _factory(redis_url: str | None = None):
        raise RuntimeError("cannot build redis client")

    monkeypatch.setattr(token_revocation_publisher, "_client_singleton", None, raising=False)
    monkeypatch.setattr(token_revocation_publisher, "_get_redis_client", _factory)

    import logging
    caplog.set_level(logging.ERROR, logger="app.services.token_revocation_publisher")
    asyncio.run(token_revocation_publisher.publish_revocation(user_id=11, revoked_at_version=2))
    assert caplog.records, "expected an error log entry on factory failure"


def test_publish_revocation_sync_uses_fresh_client_per_call(monkeypatch):
    """Sync endpoints must not reuse a redis.asyncio client bound to a closed
    asyncio.run() loop. Each sync publish gets and closes a sync client.
    """
    from app.services import token_revocation_publisher

    clients: list[_RecordingSyncRedis] = []

    def factory(redis_url: str | None = None, *, timeout: float | None = None):
        client = _RecordingSyncRedis()
        clients.append(client)
        return client

    monkeypatch.setattr(
        token_revocation_publisher,
        "_make_sync_redis_client",
        factory,
    )

    token_revocation_publisher.publish_revocation_sync(
        user_id=1, revoked_at_version=2, timeout=7.5
    )
    token_revocation_publisher.publish_revocation_sync(
        user_id=1, revoked_at_version=3, timeout=7.5
    )

    assert len(clients) == 2
    assert [len(client.published) for client in clients] == [1, 1]
    assert all(client.closed for client in clients)


def test_make_sync_redis_client_sets_timeouts(monkeypatch):
    """A stuck Redis connection must not hang a sync worker thread."""
    from app.services import token_revocation_publisher

    captured: dict[str, Any] = {}

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return _RecordingSyncRedis()

    monkeypatch.setitem(sys.modules, "redis", FakeRedisModule)

    # ⚠ 2026-08-09：逾時不再是這個模組的常數（那份 import 期常數直讀 os.environ、
    # 不過登錄表值域，於是畫面顯示 2.0 而連線用 999）。現在是**必填關鍵字**，由手上有
    # session 的呼叫端解析後傳進來。這裡刻意用 7.5 而不是登錄表預設 2.0 —— 用預設值問，
    # 「真的把參數傳下去了」與「又掉回某個預設」會一起變綠。
    client = token_revocation_publisher._make_sync_redis_client(
        "redis://example.test:6379/0", timeout=7.5
    )

    assert isinstance(client, _RecordingSyncRedis)
    assert captured["url"] == "redis://example.test:6379/0"
    assert captured["socket_connect_timeout"] == 7.5
    assert captured["socket_timeout"] == 7.5


# ---------------------------------------------------------------------------
# Integration: HTTP endpoints trigger publish + DB write.
# ---------------------------------------------------------------------------


def _bump_call(recording_redis: _RecordingRedis | _RecordingSyncRedis) -> dict[str, Any]:
    assert len(recording_redis.published) == 1, (
        f"expected exactly one publish, got {len(recording_redis.published)}"
    )
    _, message = recording_redis.published[0]
    return json.loads(message)


def test_logout_triggers_publish_after_bump(
    client: TestClient, db, recording_sync_redis
):
    """The existing logout path bumps token_version; the new publish
    call must fire after that bump, with the post-bump version."""
    user = make_user(db, username="logout-user", role="user")
    initial_version = user.token_version

    resp = client.post(
        "/api/auth/login",
        json={"username": "logout-user", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200, resp.text

    # publish_revocation should have been called once with the
    # *post-bump* token_version.
    payload = _bump_call(recording_sync_redis)
    assert payload["user_id"] == user.id
    assert payload["revoked_at_version"] == initial_version + 1


def test_password_change_triggers_publish(
    client: TestClient, db, recording_sync_redis
):
    """Changing the password also bumps token_version → must publish."""
    user = make_user(db, username="pw-user", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "pw-user", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    access = resp.json()["access_token"]

    resp = client.put(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {access}"},
        json={"current_password": "password", "new_password": "Newpassw0rd!"},
    )
    assert resp.status_code == 200, resp.text

    payload = _bump_call(recording_sync_redis)
    assert payload["user_id"] == user.id
    # Original was 0; pw change bumps once.
    assert payload["revoked_at_version"] == 1


def test_admin_revoke_triggers_publish_and_bump(
    client: TestClient, db, recording_sync_redis
):
    """POST /api/auth/revoke is admin-only and force-bumps the target's
    token_version. Verifies bump, DB row insert, and publish all fire."""
    make_user(db, username="admin-rev", role="admin")
    target = make_user(db, username="target-1", role="user")
    initial_version = target.token_version

    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-rev", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    admin_access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {admin_access}"},
        json={"user_id": target.id},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == target.id
    assert body["revoked_at_version"] == initial_version + 1

    # DB bump
    db.expire_all()
    from app.models.user import User
    refreshed = db.query(User).filter(User.id == target.id).first()
    assert refreshed.token_version == initial_version + 1

    payload = _bump_call(recording_sync_redis)
    assert payload["user_id"] == target.id
    assert payload["revoked_at_version"] == initial_version + 1


def test_admin_revoke_forbidden_for_normal_user(
    client: TestClient, db, recording_sync_redis
):
    """A regular user must not be able to call admin revoke. The
    request must short-circuit BEFORE bump / publish happens."""
    make_user(db, username="rando", role="user")
    target = make_user(db, username="target-2", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "rando", "password": "password"},
    )
    assert resp.status_code == 200
    access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {access}"},
        json={"user_id": target.id},
    )
    assert resp.status_code == 403, resp.text

    # No publish, no bump.
    assert recording_sync_redis.published == []
    db.expire_all()
    from app.models.user import User
    refreshed = db.query(User).filter(User.id == target.id).first()
    assert refreshed.token_version == 0


def test_admin_revoke_404_unknown_user(
    client: TestClient, db, recording_sync_redis
):
    make_user(db, username="admin-404", role="admin")
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-404", "password": "password"},
    )
    access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {access}"},
        json={"user_id": 999_999},
    )
    assert resp.status_code == 404, resp.text
    assert recording_sync_redis.published == []


# ---------------------------------------------------------------------------
# The producer-side contract, pinned.
#
# 2026-07-31:consumers (anila-studio / asr-gateway) 曾經把 `revoked_at_version`
# 當成「最後一個該死的版本」而用 `>=` 比較,結果任何一次撤銷都會把該帳號
# **永久**鎖在那兩個服務外面。修正落在 consumer 端(改成 `<`),前提是這裡發
# 出去的值永遠是「撞完之後的 token_version」= 下一張權杖的 `tv`。
#
# 下面兩條就是那個前提的看門狗:誰把這裡改成 `token_version - 1`,誰就會看到
# 它們變紅,而不是等到使用者改完密碼才發現語音壞了。
# ---------------------------------------------------------------------------


def _tv_of(token: str) -> int:
    from app.utils.security import decode_token

    payload = decode_token(token)
    assert payload is not None
    return int(payload["tv"])


def test_published_version_equals_the_tv_of_the_next_token_issued(
    client: TestClient, db, recording_sync_redis
):
    """改密碼後,發布的 `revoked_at_version` 必須等於 csp 當場簽出來的那張
    新權杖的 `tv`。

    這條等式就是 consumer 端 `tv < revoked_at_version` 的立足點:相等 ⇒
    新權杖活得下來。若這裡改成發 `version - 1`,新權杖的 tv 會比它大 1,
    consumer 端就會開始放行「撤銷前最後一代」的權杖 —— 那是安全破口,
    而且不會有任何人抱怨,所以更該被測試擋住。
    """
    make_user(db, username="contract-user", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "contract-user", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    old_access = resp.json()["access_token"]

    resp = client.put(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {old_access}"},
        json={"current_password": "password", "new_password": "Newpassw0rd!"},
    )
    assert resp.status_code == 200, resp.text
    new_access = resp.json()["access_token"]

    published_version = _bump_call(recording_sync_redis)["revoked_at_version"]

    assert _tv_of(new_access) == published_version, (
        "發布的 revoked_at_version 必須等於下一張權杖的 tv —— "
        "consumer 端的 `tv < revoked_at_version` 規則靠這條等式成立"
    )
    assert _tv_of(old_access) < published_version, (
        "撤銷前簽出的權杖必須嚴格小於發布值,否則 consumer 端殺不掉它"
    )

    # 把 consumer 端那條規則原地套一次,兩半一起驗。
    def consumer_rejects(tv: int) -> bool:
        return tv < published_version

    assert consumer_rejects(_tv_of(old_access)) is True
    assert consumer_rejects(_tv_of(new_access)) is False


def test_admin_revoke_does_not_lock_the_user_out_of_relogin(
    client: TestClient, db, recording_sync_redis
):
    """管理員強制登出之後,該使用者重新登入拿到的權杖,不能落在自己被撤銷
    的範圍裡。

    這是 `.15` 上線最怕的那個情境:管理員按了「撤銷權杖」,使用者重新登入
    看似成功,結果半個平台不認他。
    """
    make_user(db, username="admin-relogin", role="admin")
    target = make_user(db, username="target-relogin", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "admin-relogin", "password": "password"},
    )
    admin_access = resp.json()["access_token"]

    resp = client.post(
        "/api/auth/revoke",
        headers={"Authorization": f"Bearer {admin_access}"},
        json={"user_id": target.id},
    )
    assert resp.status_code == 200, resp.text
    revoked_at_version = resp.json()["revoked_at_version"]
    assert _bump_call(recording_sync_redis)["revoked_at_version"] == revoked_at_version

    resp = client.post(
        "/api/auth/login",
        json={"username": "target-relogin", "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    relogin_tv = _tv_of(resp.json()["access_token"])

    assert relogin_tv == revoked_at_version
    assert (relogin_tv < revoked_at_version) is False, (
        "重新登入拿到的權杖若落在撤銷範圍內,帳號就被永久鎖死了"
    )


def test_publish_failure_does_not_break_logout(client: TestClient, db, monkeypatch):
    """End-to-end resilience: if Redis is unreachable, logout still
    returns 200 because the DB bump already happened."""
    user = make_user(db, username="resilient", role="user")

    resp = client.post(
        "/api/auth/login",
        json={"username": "resilient", "password": "password"},
    )
    access = resp.json()["access_token"]

    from app.services import token_revocation_publisher

    fake = _RecordingSyncRedis(raise_on_publish=ConnectionError("redis down"))

    def _factory(redis_url: str | None = None, *, timeout: float | None = None):
        return fake

    monkeypatch.setattr(
        token_revocation_publisher, "_make_sync_redis_client", _factory
    )

    resp = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
    )
    assert resp.status_code == 200, resp.text

    # DB bump still happened even though publish blew up.
    db.expire_all()
    from app.models.user import User
    refreshed = db.query(User).filter(User.id == user.id).first()
    assert refreshed.token_version == 1
