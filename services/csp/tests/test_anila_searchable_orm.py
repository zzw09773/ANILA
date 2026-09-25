# -*- coding: utf-8 -*-
"""密等閘門在 ``create_all()`` 建出來的 schema 裡也要生效。

``tests/test_anila_searchable_pg.py`` 釘的是 **migration** 那一份 schema。
但本專案還有第二條建表路徑，而且其中一條是**正式環境**的：

* ``app/main.py:115`` —— alembic 升級失敗時的退路 ``Base.metadata.create_all()``；
* ``tests/conftest.py:76`` 與 ``:100``（整套 SQLite 測試的 schema）。

這幾條走的是 ORM 的 ``__table_args__``，不是 migration。少了那份宣告，它們建出來
的表就是「有 ``anila_searchable`` 欄位、沒有密等閘門」—— 正式環境在 alembic 出事
的那一天會拿到一份可以把機密庫標成全院可檢索的 schema，而且不會有任何錯誤訊息。

⚠ SQLite **會**執行 CHECK 約束（實測），所以這份宣告在這裡是真的在擋，不是裝飾。

⚠ 同名雙宣告（ORM ＋ migration r1_0033）是本 repo 的既有做法：
``ck_messages_parent_not_self``(ORM ＋ r1_0012)、
``ck_messages_rating_score_matches_thumb``(ORM ＋ r1_0030) 都是。代價是兩份
會漂開；因此兩層各自有行為測試，而且**兩層都參數化跑遍所有帶密等的級別**，
任一層被改壞或刪掉都會有東西變紅。
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.ingestion import IngestionCollection
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel

_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.value
_CLASSIFIED = [
    level.value
    for level in ClassificationLevel
    if level is not ClassificationLevel.UNCLASSIFIED
]


def _owner(db) -> User:
    user = User(
        username=f"anila_searchable_owner_{id(db)}",
        hashed_password="x",
        role="developer",
        is_approved=True,
    )
    db.add(user)
    db.flush()
    return user


def _collection(db, level: str, *, searchable: bool) -> IngestionCollection:
    return IngestionCollection(
        name="院內規章",
        chunking_config={"strategy": "fixed"},
        embedding_model="nv-embed-v2",
        embedding_dim=4000,
        created_by=_owner(db).id,
        origin="csp",
        status="active",
        classification_level=level,
        anila_searchable=searchable,
    )


def test_the_orm_declares_the_column(db):
    """刪掉 ORM 的 Column 時，要有東西直接變紅。

    這一支存在的理由很窄：其餘測試都是行為測試，而行為測試在欄位整個消失時
    的失敗訊息會指向奇怪的地方。
    """
    assert "anila_searchable" in IngestionCollection.__table__.columns


@pytest.mark.parametrize("level", _CLASSIFIED)
def test_create_all_schema_blocks_marking_a_classified_collection(db, level):
    db.add(_collection(db, level, searchable=True))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


@pytest.mark.parametrize("level", _CLASSIFIED)
def test_create_all_schema_blocks_raising_classification_on_a_marked_collection(
    db, level
):
    coll = _collection(db, _UNCLASSIFIED, searchable=True)
    db.add(coll)
    db.flush()
    coll.classification_level = level
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_create_all_schema_allows_marking_an_unclassified_collection(db):
    """閘門，不是禁令 —— 恆假的約束在這裡也要被殺掉。"""
    coll = _collection(db, _UNCLASSIFIED, searchable=True)
    db.add(coll)
    db.flush()
    assert coll.anila_searchable is True


def test_create_all_schema_defaults_to_not_searchable(db):
    """無機密是必要條件不是觸發條件：不指定就是沒標記。"""
    coll = IngestionCollection(
        name="院內規章",
        chunking_config={"strategy": "fixed"},
        embedding_model="nv-embed-v2",
        embedding_dim=4000,
        created_by=_owner(db).id,
        origin="csp",
        status="active",
        classification_level=_UNCLASSIFIED,
    )
    db.add(coll)
    db.flush()
    assert coll.anila_searchable is False
