"""True-PostgreSQL G15/G18 multi-instance consume/rotation evidence.

Set ``ANILA_TEST_PG_DSN`` to an isolated PostgreSQL superuser DSN. Each test
creates and destroys only its own randomly named schema.
"""
from __future__ import annotations

import os
import importlib.util
import threading
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.auth_session import AuthSession
from app.models.user import User
from app.services.auth_service import (
    RefreshTokenReuseDetected,
    create_tokens,
    rotate_refresh_token,
)
from app.services.card_auth_service import (
    CardLoginRejected,
    _consume_card_challenge,
    _decode_card_challenge_claims,
    issue_card_challenge,
)
from app.utils.security import decode_token, hash_password


_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN not set — true PostgreSQL race test",
)


def _schema_engine(schema: str):
    return create_engine(
        _DSN,
        pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )


def _create_schema(*, auth: bool) -> tuple[str, object]:
    schema = f"gate2_auth_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_DSN, isolation_level="AUTOCOMMIT")
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET search_path TO {schema}"))
        connection.execute(
            text(
                """
                CREATE TABLE card_login_challenges (
                    jti varchar(64) PRIMARY KEY,
                    nonce_digest varchar(64) NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at timestamptz NOT NULL
                );
                CREATE INDEX ix_card_login_challenges_expires_at
                    ON card_login_challenges(expires_at);
                """
            )
        )
        if auth:
            connection.execute(
                text(
                    """
                    CREATE TABLE departments (
                        id serial PRIMARY KEY,
                        name varchar(100) UNIQUE NOT NULL,
                        description varchar(255),
                        is_active boolean DEFAULT true,
                        created_at timestamp,
                        updated_at timestamp
                    );
                    CREATE TABLE users (
                        id serial PRIMARY KEY,
                        username varchar(100) UNIQUE NOT NULL,
                        email varchar(255),
                        hashed_password varchar(255) NOT NULL,
                        role varchar(20) NOT NULL DEFAULT 'user',
                        department_id integer REFERENCES departments(id) ON DELETE SET NULL,
                        is_active boolean DEFAULT true,
                        is_approved boolean NOT NULL DEFAULT true,
                        token_version integer NOT NULL DEFAULT 0,
                        local_password_disabled boolean NOT NULL DEFAULT false,
                        last_login_at timestamp,
                        ui_settings jsonb NOT NULL DEFAULT '{}'::jsonb,
                        created_at timestamp,
                        updated_at timestamp
                    );
                    CREATE TABLE auth_sessions (
                        sid varchar(64) PRIMARY KEY,
                        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        refresh_family_id varchar(64) UNIQUE NOT NULL,
                        amr_json text NOT NULL,
                        acr varchar(128) NOT NULL,
                        auth_time timestamptz NOT NULL,
                        break_glass boolean NOT NULL DEFAULT false,
                        break_glass_ticket varchar(128),
                        break_glass_expires_at timestamptz,
                        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        revoked_at timestamptz,
                        revoke_reason varchar(128),
                        CONSTRAINT ck_auth_sessions_break_glass_binding CHECK (
                            (break_glass = false
                                AND break_glass_ticket IS NULL
                                AND break_glass_expires_at IS NULL)
                            OR
                            (break_glass = true
                                AND break_glass_ticket IS NOT NULL
                                AND break_glass_expires_at IS NOT NULL)
                        )
                    );
                    CREATE TABLE auth_refresh_tokens (
                        jti_hash varchar(64) PRIMARY KEY,
                        sid varchar(64) NOT NULL REFERENCES auth_sessions(sid) ON DELETE CASCADE,
                        generation integer NOT NULL,
                        parent_jti_hash varchar(64),
                        issued_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        expires_at timestamptz NOT NULL,
                        consumed_at timestamptz,
                        revoked_at timestamptz,
                        CONSTRAINT uq_auth_refresh_sid_generation UNIQUE(sid, generation)
                    );
                    CREATE TABLE token_revocations (
                        id bigserial PRIMARY KEY,
                        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        revoked_at_version integer NOT NULL,
                        scope varchar(16) NOT NULL DEFAULT 'user_version',
                        token_jti_hash varchar(64),
                        session_id_hash varchar(64),
                        token_type varchar(16),
                        reason varchar(128),
                        revoked_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE audit_logs (
                        id bigserial PRIMARY KEY,
                        actor_user_id integer REFERENCES users(id) ON DELETE SET NULL,
                        actor_username varchar(100),
                        action varchar(100) NOT NULL,
                        resource_type varchar(50) NOT NULL,
                        resource_id varchar(255),
                        status varchar(20) NOT NULL DEFAULT 'success',
                        detail text,
                        ip_address varchar(64),
                        metadata_json text,
                        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
            )
    return schema, admin


def _drop_schema(schema: str, admin) -> None:
    assert schema.startswith("gate2_auth_")
    with admin.begin() as connection:
        connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()


def _load_auth_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0014_auth_session_rotation.py"
    )
    spec = importlib.util.spec_from_file_location("gate2_auth_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_auth_migration_upgrade_constraints_and_downgrade():
    schema = f"gate2_auth_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_DSN)
    try:
        with admin.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text(
                    """
                    CREATE TABLE users (id serial PRIMARY KEY);
                    CREATE TABLE token_revocations (
                        id bigserial PRIMARY KEY,
                        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        revoked_at_version integer NOT NULL,
                        revoked_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
            )
            migration = _load_auth_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            table_names = set(inspect(connection).get_table_names(schema=schema))
            assert {"auth_sessions", "auth_refresh_tokens"} <= table_names
            constraints = {
                item["name"]
                for item in inspect(connection).get_check_constraints(
                    "token_revocations", schema=schema
                )
            }
            assert {
                "ck_token_revocations_scope",
                "ck_token_revocations_scope_shape",
                "ck_token_revocations_jti_hash_length",
                "ck_token_revocations_sid_hash_length",
            } <= constraints

            connection.execute(text("INSERT INTO users DEFAULT VALUES"))
            savepoint = connection.begin_nested()
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO token_revocations "
                        "(user_id, revoked_at_version, scope) "
                        "VALUES (1, 0, 'jti')"
                    )
                )
            savepoint.rollback()

            migration.downgrade()
            table_names = set(inspect(connection).get_table_names(schema=schema))
            assert "auth_sessions" not in table_names
            assert "auth_refresh_tokens" not in table_names
    finally:
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin.dispose()


def test_card_challenge_exactly_one_after_restart_and_two_instance_race():
    schema, admin = _create_schema(auth=False)
    engine_a = _schema_engine(schema)
    engine_b = _schema_engine(schema)
    SessionA = sessionmaker(bind=engine_a)
    SessionB = sessionmaker(bind=engine_b)
    try:
        # Sequential restart: issue in one process/session, consume in another,
        # then prove a third fresh session cannot replay it.
        with SessionA() as issuer:
            token, nonce, _ = issue_card_challenge(issuer)
        _, jti = _decode_card_challenge_claims(token)
        with SessionB() as restarted:
            _consume_card_challenge(restarted, jti=jti, nonce=nonce)
        with SessionA() as after_second_restart:
            with pytest.raises(CardLoginRejected):
                _consume_card_challenge(after_second_restart, jti=jti, nonce=nonce)

        # Concurrent instances: PostgreSQL's conditional DELETE has one winner.
        with SessionA() as issuer:
            race_token, race_nonce, _ = issue_card_challenge(issuer)
        _, race_jti = _decode_card_challenge_claims(race_token)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def consume(SessionFactory):
            with SessionFactory() as session:
                barrier.wait()
                try:
                    _consume_card_challenge(
                        session, jti=race_jti, nonce=race_nonce
                    )
                    outcomes.append("won")
                except CardLoginRejected:
                    outcomes.append("rejected")

        threads = [
            threading.Thread(target=consume, args=(SessionA,)),
            threading.Thread(target=consume, args=(SessionB,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(outcomes) == ["rejected", "won"]
    finally:
        engine_a.dispose()
        engine_b.dispose()
        _drop_schema(schema, admin)


def test_refresh_rotation_restart_and_two_instance_race_max_one_success():
    schema, admin = _create_schema(auth=True)
    engine_a = _schema_engine(schema)
    engine_b = _schema_engine(schema)
    SessionA = sessionmaker(bind=engine_a)
    SessionB = sessionmaker(bind=engine_b)
    try:
        with SessionA() as issuer:
            user = User(
                username=f"pg-auth-{uuid.uuid4().hex}",
                hashed_password=hash_password("irrelevant"),
                role="user",
                is_active=True,
                is_approved=True,
            )
            issuer.add(user)
            issuer.commit()
            issuer.refresh(user)
            user_id = user.id
            first_pair = create_tokens(user, db=issuer, amr=("pwd",))
            issuer.commit()

        # New engine/session proves durable state survives process restart.
        first_payload = decode_token(first_pair["refresh_token"])
        assert first_payload is not None
        with SessionB() as restarted:
            user = restarted.get(User, user_id)
            second_pair = rotate_refresh_token(restarted, user, first_payload)
        second_payload = decode_token(second_pair["refresh_token"])
        assert second_payload is not None

        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def rotate(SessionFactory):
            with SessionFactory() as session:
                user = session.get(User, user_id)
                barrier.wait()
                try:
                    rotate_refresh_token(session, user, second_payload)
                    outcomes.append("won")
                except (RefreshTokenReuseDetected, Exception) as exc:
                    # HTTPException and the explicit reuse signal are both
                    # fail-closed loser outcomes; unexpected errors are exposed
                    # by their class name in the assertion below.
                    outcomes.append(
                        "rejected"
                        if isinstance(exc, RefreshTokenReuseDetected)
                        else f"error:{type(exc).__name__}"
                    )

        threads = [
            threading.Thread(target=rotate, args=(SessionA,)),
            threading.Thread(target=rotate, args=(SessionB,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert sorted(outcomes) == ["rejected", "won"]

        with SessionA() as readback:
            session = readback.get(AuthSession, second_payload["sid"])
            assert session.revoked_at is not None
            assert session.revoke_reason == "refresh_token_reuse"
            audit = readback.execute(
                text(
                    "SELECT metadata_json FROM audit_logs "
                    "WHERE action = 'auth.refresh_reuse'"
                )
            ).scalar_one()
            assert second_payload["jti"] not in audit
            assert "token_jti_hash" in audit
            assert "session_id_hash" in audit
    finally:
        engine_a.dispose()
        engine_b.dispose()
        _drop_schema(schema, admin)
