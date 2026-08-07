# -*- coding: utf-8 -*-
"""r1_0033 的 CHECK 約束：已標記的庫不可能帶密等。

為什麼要 PG：這支測的是 **migration 建出來的那一份 schema** —— 既有部署跑完
``alembic upgrade`` 之後真正上線的 DDL。那條路只有 PostgreSQL 走得完
（``ingestion_collections`` 前面串著 pgvector 與 RLS 的一整條 migration 鏈）。
``create_all()`` 那條路（ORM ``__table_args__``）另由
``tests/test_anila_searchable_orm.py`` 在 SQLite 上釘住 —— **兩層各自宣告、
各自被行為測試釘住**，任何一層漏掉都會有東西變紅。

⚠ 更正：SQLite **會**執行 CHECK 約束（本檔初版寫「SQLite 不執行」是錯的）。
選 PG 的理由是上面那條：要驗的是 migration 產出的真實 DDL，不是 ORM 渲染出來
的近似品。
⚠ 這支刻意用 superuser 跑就好 —— CHECK 對 superuser 一樣會擋（不像 RLS 會被 bypass）。

⚠ ``upgraded_conn`` 是 **function-scoped**：每支測試各拿一列全新的 collection。
共用同一列會讓第一支測完留下 ``anila_searchable = true``，第二支的**前置**
UPDATE（升密）就當場撞上同一條 CHECK —— 而那次違反發生在
``pytest.raises`` 外面，測試會以 error 收場而不是紅燈。scratch DB 與 alembic
升級仍是 module-scoped，一次就好。

⚠ 兩支「擋得住」的測試單獨看是**不夠**的，有兩個方向都會漏：

* 恆假的約束（例如 ``NOT anila_searchable``，誰都不准標記）照樣全綠而功能已死
  → ``test_marking_an_unclassified_collection_succeeds`` 殺它；
* 只擋最高階的約束（例如 ``classification_level <> '機密'``）也照樣全綠，而
  ``密`` / ``營業秘密`` 的庫就被全院檢索得到 → 兩支「擋得住」的測試**參數化跑遍
  除了無機密以外的每一級**。清單由 ``ClassificationLevel`` 取補集推導，不寫死：
  日後新增等級會自動納入，而且這樣釘住的是「必須等於無機密」而非「不等於機密」。
"""
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest

from app.schemas.contracts.classification import ClassificationLevel

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_TEST_PG_DSN not set — needs PostgreSQL"
)

_CSP_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _CSP_ROOT / "alembic.ini"

# 唯一能開標記的等級，以及它的補集 —— 兩者都從契約 enum 推導，避免與
# ``app/schemas/contracts/classification.py`` 漂開。
_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.value
_CLASSIFIED = [
    level.value
    for level in ClassificationLevel
    if level is not ClassificationLevel.UNCLASSIFIED
]


def _scratch_url(admin_dsn: str, dbname: str) -> str:
    parts = urlparse(admin_dsn)
    return urlunparse(parts._replace(path=f"/{dbname}"))


def _dbname_of(dsn: str) -> str:
    return (urlparse(dsn).path or "").lstrip("/") or "postgres"


def _alembic(target: str, dsn: str) -> None:
    """Run ``alembic upgrade <target>`` against ``dsn``.

    ``migrations/env.py`` calls ``logging.config.fileConfig``, which tears
    down handlers attached by the rest of the suite; neutralise it for the
    duration so this fixture cannot reshape logging for other test files.
    """
    import logging.config

    from alembic import command
    from alembic.config import Config

    prev_file_config = logging.config.fileConfig
    logging.config.fileConfig = lambda *a, **k: None

    prev_mig = os.environ.get("MIGRATION_DATABASE_URL")
    prev_db = os.environ.get("DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = dsn
    os.environ["DATABASE_URL"] = dsn
    old_cwd = os.getcwd()
    os.chdir(_CSP_ROOT)
    try:
        command.upgrade(Config(str(_ALEMBIC_INI)), target)
    finally:
        os.chdir(old_cwd)
        logging.config.fileConfig = prev_file_config
        for key, prev in (
            ("MIGRATION_DATABASE_URL", prev_mig),
            ("DATABASE_URL", prev_db),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


@pytest.fixture(scope="module")
def scratch_dsn():
    """A uuid-suffixed throwaway database. Never the live platform DB."""
    assert _DSN
    admin_db = _dbname_of(_DSN)
    scratch = f"r1_0033_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to migrate the DSN's own database"
    assert scratch != "csp", "refusing to touch the live platform database"

    admin = psycopg2.connect(_DSN)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = admin.cursor()
    try:
        cur.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        cur.close()
        admin.close()

    try:
        yield _scratch_url(_DSN, scratch)
    finally:
        admin = psycopg2.connect(_DSN)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = admin.cursor()
        try:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (scratch,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        finally:
            cur.close()
            admin.close()


@pytest.fixture(scope="module")
def upgraded(scratch_dsn):
    """Migrate the scratch DB to head and seed the collection's owner."""
    _alembic("head", scratch_dsn)

    conn = psycopg2.connect(scratch_dsn)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, hashed_password, role, is_approved) "
        "VALUES (%s, 'x', 'developer', true) RETURNING id",
        (f"r1_0033_owner_{uuid.uuid4().hex[:8]}",),
    )
    owner = cur.fetchone()[0]
    conn.commit()

    yield conn, cur, owner

    cur.close()
    conn.close()


@pytest.fixture
def upgraded_conn(upgraded):
    """One freshly seeded 無機密 collection per test — see the module docstring."""
    conn, cur, owner = upgraded
    cur.execute(
        "INSERT INTO ingestion_collections "
        "  (name, chunking_config, embedding_model, embedding_dim, "
        "   created_by, origin, status, classification_level) "
        "VALUES (%s, %s::jsonb, 'nv-embed-v2', 4000, %s, 'csp', 'active', %s) "
        "RETURNING id",
        (
            f"kb-{uuid.uuid4().hex[:8]}",
            json.dumps({"strategy": "fixed"}),
            owner,
            _UNCLASSIFIED,
        ),
    )
    coll_id = cur.fetchone()[0]
    conn.commit()

    yield conn, cur, coll_id

    # A CheckViolation leaves the transaction aborted — roll back before
    # the DELETE or the cleanup itself errors.
    conn.rollback()
    cur.execute("DELETE FROM ingestion_collections WHERE id = %s", (coll_id,))
    conn.commit()


@pytest.mark.parametrize("level", _CLASSIFIED)
def test_raising_classification_on_a_marked_collection_fails(upgraded_conn, level):
    conn, cur, coll_id = upgraded_conn
    cur.execute(
        "UPDATE ingestion_collections SET anila_searchable = true WHERE id = %s",
        (coll_id,),
    )
    conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "UPDATE ingestion_collections SET classification_level = %s WHERE id = %s",
            (level, coll_id),
        )
    conn.rollback()


@pytest.mark.parametrize("level", _CLASSIFIED)
def test_marking_a_classified_collection_fails(upgraded_conn, level):
    conn, cur, coll_id = upgraded_conn
    cur.execute(
        "UPDATE ingestion_collections SET classification_level = %s WHERE id = %s",
        (level, coll_id),
    )
    conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "UPDATE ingestion_collections SET anila_searchable = true WHERE id = %s",
            (coll_id,),
        )
    conn.rollback()


def test_marking_an_unclassified_collection_succeeds(upgraded_conn):
    """約束是閘門，不是禁令。

    Without this, a constraint written as ``NOT anila_searchable`` — nobody
    may ever be marked — passes both tests above while the feature is dead.
    """
    conn, cur, coll_id = upgraded_conn
    cur.execute(
        "UPDATE ingestion_collections SET anila_searchable = true WHERE id = %s",
        (coll_id,),
    )
    conn.commit()
    cur.execute(
        "SELECT anila_searchable FROM ingestion_collections WHERE id = %s", (coll_id,)
    )
    assert cur.fetchone()[0] is True


def test_a_new_unclassified_collection_is_not_searchable_by_default(upgraded_conn):
    """無機密 is a necessary condition, not a trigger.

    Nothing may become ANILA-searchable because its classification happens
    to be low — an admin has to say so. Kills a ``server_default`` of
    ``true``, which every other test in this file would tolerate.
    """
    conn, cur, coll_id = upgraded_conn
    cur.execute(
        "SELECT anila_searchable FROM ingestion_collections WHERE id = %s", (coll_id,)
    )
    assert cur.fetchone()[0] is False
