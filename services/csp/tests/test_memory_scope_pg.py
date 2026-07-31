# -*- coding: utf-8 -*-
"""P4.5 —— ``conversation_memory_chunks`` 這個儲存體的範圍限制(PostgreSQL-only)。

姊妹檔 ``test_memory_anilalm_scope.py`` 在 SQLite 上驗 ``user_facts``;
向量召回這半用 pgvector 的 ``halfvec`` 與 ``<=>`` 距離運算子,SQLite 兩者
皆無,所以只能在真的 PostgreSQL 上驗。這裡跑的是**真的 ANN 查詢**,
不是把 SQL 攔下來看參數 —— 攔參數只證明字串長對,證明不了資料庫真的
把對話框外的列擋掉了。

擁有者裁定(PLAN.md §4.4/4.5):ANILALM 的「同一 session」= 同一個對話框。

沒設 ``ANILA_TEST_PG_DSN`` 就跳過。跑法:

.. code-block:: bash

    cd services/csp
    ANILA_TEST_PG_DSN=postgresql://USER:PASS@127.0.0.1:PORT/postgres \\
      PYTHONPATH=../../packages/anila-core/src:. \\
      pytest tests/test_memory_scope_pg.py -v

DSN 請指向維護用資料庫;fixture 會自己 ``CREATE DATABASE
memscope_pg_<hex>`` 再 drop,絕不碰平台的 ``csp`` 資料庫。
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse, urlunparse

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason=(
        "ANILA_TEST_PG_DSN not set — halfvec / <=> 是 pgvector 專有,"
        "SQLite 跑不了向量召回"
    ),
)

_DIM = 4000
_MODEL = "nvidia/nv-embed-v2"


def _scratch_url(admin_dsn: str, dbname: str) -> str:
    parts = urlparse(admin_dsn)
    return urlunparse(parts._replace(path=f"/{dbname}"))


def _dbname_of(dsn: str) -> str:
    return (urlparse(dsn).path or "").lstrip("/") or "postgres"


def _axis(i: int) -> list[float]:
    """單位向量,第 ``i`` 軸為 1。同軸的餘弦相似度 = 1.0,
    所以相似度門檻不會參與判定,唯一在過濾的就是範圍條件。"""
    v = [0.0] * _DIM
    v[i] = 1.0
    return v


@pytest.fixture(scope="module")
def pg_engine():
    assert _DSN
    from sqlalchemy import create_engine, text

    import app.models  # noqa: F401  —— 讓 Base.metadata 齊全
    from app.database import Base

    admin_db = _dbname_of(_DSN)
    scratch = f"memscope_pg_{uuid.uuid4().hex[:10]}"
    assert scratch != admin_db, "refusing to use the DSN's own database"
    assert scratch != "csp", "refusing to touch the live platform database"

    admin = psycopg2.connect(_DSN)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = admin.cursor()
    try:
        cur.execute(f'CREATE DATABASE "{scratch}"')
    finally:
        cur.close()
        admin.close()

    engine = create_engine(_scratch_url(_DSN, scratch))
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        # ORM 把 embedding 宣告成 Text(halfvec 在 SQLAlchemy 沒有一級型別),
        # 真正的 migration 建的是 halfvec(4000)。這裡對齊正式 schema,
        # 否則 <=> 無從施展。
        conn.execute(
            text(
                "ALTER TABLE conversation_memory_chunks "
                f"ALTER COLUMN embedding TYPE halfvec({_DIM}) "
                f"USING embedding::halfvec({_DIM})"
            )
        )
        # 同樣對齊 migration 0030:正式 schema 的 created_at 帶
        # ``DEFAULT CURRENT_TIMESTAMP``,但 ORM 只宣告 Python 端 default,
        # 所以 create_all 建出來的表沒有預設值,而 ``_insert_chunk`` 走的是
        # 不含 created_at 的原生 SQL。不補這一行,測的就不是正式 schema。
        conn.execute(
            text(
                "ALTER TABLE conversation_memory_chunks "
                "ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()
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


@pytest.fixture()
def world(pg_engine, monkeypatch):
    """一個使用者、三個對話框,每個對話框各一則已嵌入的記憶。"""
    from sqlalchemy.orm import sessionmaker

    from app.models.conversation import Conversation
    from app.models.user import User
    from app.services import memory_service
    from app.utils.security import hash_password

    Session = sessionmaker(bind=pg_engine, expire_on_commit=False)
    db = Session()

    user = User(
        username=f"u{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.invalid",
        hashed_password=hash_password("password"),
        role="user",
        is_approved=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    convs = {}
    for name, origin in (
        ("lm", "anilalm"),
        ("lm_other", "anilalm"),
        ("ui", "anila-ui"),
    ):
        c = Conversation(user_id=user.id, title=name, origin=origin)
        db.add(c)
        db.commit()
        db.refresh(c)
        convs[name] = c

    for name, c in convs.items():
        memory_service._insert_chunk(
            db,
            user_id=user.id,
            conversation_id=c.id,
            message_id=None,
            role="user",
            content=f"content-{name}",
            is_encrypted=False,
            embedding=_axis(0),
            source_model=_MODEL,
            native_dim=4096,
        )
    db.commit()

    async def _fake_embed(_db, _text):
        return _axis(0), _MODEL, 4096

    monkeypatch.setattr(memory_service, "_embed", _fake_embed)

    try:
        yield db, user, convs
    finally:
        db.close()


def _contents(hits):
    return sorted(h.content for h in hits)


# ── 規則的兩邊,在真的向量查詢上 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_conversation_id_returns_that_box_and_nothing_else(world):
    """ANILALM:對話框內的召回還在,對話框外的召回不見了。"""
    from app.services import memory_service

    db, user, convs = world

    hits = await memory_service.retrieve_relevant_chunks(
        db, user.id, "問題", only_conversation_id=convs["lm"].id
    )

    assert _contents(hits) == ["content-lm"], (
        "ANILALM 只該召回同一個對話框的記憶,實得:%s" % _contents(hits)
    )


@pytest.mark.asyncio
async def test_unscoped_retrieval_still_crosses_conversations(world):
    """ANILA 側不變 —— 沒下範圍就是跨對話召回,規格給的功能不能被收掉。"""
    from app.services import memory_service

    db, user, convs = world

    hits = await memory_service.retrieve_relevant_chunks(
        db, user.id, "問題", exclude_conversation_id=convs["ui"].id
    )

    assert _contents(hits) == ["content-lm", "content-lm_other"]


@pytest.mark.asyncio
async def test_only_conversation_id_beats_exclude_of_the_same_id(world):
    """呼叫端會把同一個 id 同時當 exclude 傳進來(它就是當前對話)。
    若兩個條件都生效,結果會是空的 —— 那是靜默關掉召回,不是限制範圍。"""
    from app.services import memory_service

    db, user, convs = world

    hits = await memory_service.retrieve_relevant_chunks(
        db,
        user.id,
        "問題",
        exclude_conversation_id=convs["lm"].id,
        only_conversation_id=convs["lm"].id,
    )

    assert _contents(hits) == ["content-lm"]


@pytest.mark.asyncio
async def test_build_memory_block_scopes_chunks_too(world):
    """走 ``build_memory_block`` 的正式入口,確認 chunk 這半也被關住。"""
    from app.services import memory_service

    db, user, convs = world

    result = await memory_service.build_memory_block(
        db,
        user.id,
        "問題",
        exclude_conversation_id=convs["lm"].id,
        only_conversation_id=convs["lm"].id,
    )

    assert _contents(result.chunks) == ["content-lm"]
    assert "content-lm_other" not in (result.block or "")
    assert "content-ui" not in (result.block or "")
