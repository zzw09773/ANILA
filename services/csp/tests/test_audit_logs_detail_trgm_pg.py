"""True-PostgreSQL acceptance for r1_0035 (audit_logs.detail trgm index).

SQLite 單元測試看不見這件事(schema 走 ``Base.metadata.create_all``,而
``gin_trgm_ops`` 只有 PostgreSQL 有),所以判準只能在真 PG 上跑:CI 的
``postgres-rls`` job 會提供 ``ANILA_GATE2_TEST_PG_ADMIN_URL``;沒有該環境變數時
本檔整批 skip(照 ``test_ingestion_generations_pg.py`` 的既有寫法)。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url


ROOT = Path(__file__).resolve().parents[3]
CSP_DIR = ROOT / "services/csp"
INDEX_NAME = "ix_audit_logs_detail_trgm"


def _admin_url() -> URL:
    raw = (
        os.environ.get("ANILA_AUDIT_TRGM_PG_URL")
        or os.environ.get("ANILA_GATE2_TEST_PG_ADMIN_URL")
        or os.environ.get("ANILA_GATE3_TEST_PG_ADMIN_URL")
    )
    if not raw:
        pytest.skip("ANILA_GATE2_TEST_PG_ADMIN_URL is not set")
    return make_url(raw)


def _database_url(url: URL, database: str) -> str:
    return url.set(database=database).render_as_string(hide_password=False)


def _alembic(database_url: str, *arguments: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "MIGRATION_DATABASE_URL": database_url,
            "DATABASE_URL": database_url,
            "CSP_APP_DB_PASSWORD": "audit-trgm-app-db",
            # Prepend rather than replace: overwriting PYTHONPATH with "." threw
            # away whatever the caller had set, so the subprocess could only
            # find anila_security when the packages happened to be installed
            # editable into site-packages. CI installs them that way, so this
            # never surfaced — and the file was skipping silently there anyway.
            "PYTHONPATH": os.pathsep.join(
                p for p in (".", os.environ.get("PYTHONPATH", "")) if p
            ),
            "PYTHONIOENCODING": "utf-8",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=CSP_DIR,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic {' '.join(arguments)} failed:\n{result.stdout}\n{result.stderr}"
        )


@pytest.fixture
def migrated_database() -> Iterator[str]:
    admin = _admin_url()
    database = f"audit_trgm_{uuid.uuid4().hex[:10]}"
    maintenance = create_engine(
        _database_url(admin, "postgres"), isolation_level="AUTOCOMMIT"
    )
    with maintenance.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = _database_url(admin, database)
    try:
        _alembic(url, "upgrade", "head")
        yield url
    finally:
        with maintenance.connect() as connection:
            connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
        maintenance.dispose()


def _index_rows(database_url: str) -> list[tuple[str, str]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return [
                (row[0], row[1])
                for row in connection.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'audit_logs'"
                    )
                )
            ]
    finally:
        engine.dispose()


def test_upgrade_head_creates_valid_gin_trgm_index(migrated_database: str):
    rows = dict(_index_rows(migrated_database))
    assert INDEX_NAME in rows, sorted(rows)
    definition = rows[INDEX_NAME].lower()
    assert "using gin" in definition
    assert "gin_trgm_ops" in definition
    assert "detail" in definition

    engine = create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            # CONCURRENTLY 失敗會留下 indisvalid=false 的索引 —— 存在但查詢用
            # 不到。只斷言「有這個名字」會漏掉這種壞法。
            valid = connection.execute(
                text(
                    "SELECT i.indisvalid FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE c.relname = :name"
                ),
                {"name": INDEX_NAME},
            ).scalar_one()
            assert valid is True
            assert connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).first() is not None
    finally:
        engine.dispose()


def test_downgrade_removes_the_index_and_keeps_pg_trgm(migrated_database: str):
    _alembic(migrated_database, "downgrade", "r1_0034")
    rows = dict(_index_rows(migrated_database))
    assert INDEX_NAME not in rows
    engine = create_engine(migrated_database)
    try:
        with engine.connect() as connection:
            # extension 不能連坐拔掉:0044 的 messages.content /
            # conversations.title trgm index 也靠它。
            assert connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).first() is not None
    finally:
        engine.dispose()
    # 重跑 upgrade 必須可行(IF NOT EXISTS + INVALID 殘留清理)。
    _alembic(migrated_database, "upgrade", "head")
    assert INDEX_NAME in dict(_index_rows(migrated_database))


def test_ilike_detail_search_can_use_the_trgm_index(migrated_database: str):
    """索引存在還不夠 —— planner 得真的能用它。

    小表上 PG 一定選 seq scan,所以先關掉 seq scan 再看 plan 走不走 bitmap
    index scan(這是「索引對 ILIKE '%x%' 有效」的機械判準,不是效能測試)。
    """
    engine = create_engine(migrated_database)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO audit_logs"
                    "(action,resource_type,status,detail,created_at) "
                    "SELECT 'model.update','model','success',"
                    "'改了模型設定 ' || g, now() "
                    "FROM generate_series(1,200) AS g"
                )
            )
            connection.execute(text("ANALYZE audit_logs"))
        with engine.connect() as connection:
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(
                row[0]
                for row in connection.execute(
                    text(
                        "EXPLAIN SELECT id FROM audit_logs "
                        "WHERE detail ILIKE '%模型設定%'"
                    )
                )
            )
            assert INDEX_NAME in plan, plan
    finally:
        engine.dispose()
