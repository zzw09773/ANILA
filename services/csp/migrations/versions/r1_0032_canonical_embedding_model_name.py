# -*- coding: utf-8 -*-
"""#56 — embedding 模型名稱的大小寫碰撞：把資料改成註冊表的拼法。

背景
====
``ingestion_collections.embedding_model`` 的 DB DEFAULT 是
``'nvidia/NV-embed-V2'``(migration 0014),而同一顆模型在
``model_registry`` 註冊的名字是 ``nvidia/nv-embed-v2``。
migration ``r1_0018`` 又把那個欄位原封不動抄到
``document_chunks`` / ``ingestion_images`` 的 ``embedding_source_model``。
檢索是**用模型名字過濾 chunk 來源**的,所以一個索引完全正常的語料庫
會被判成「索引在別的模型下」——**200 筆空結果、零日誌**。

2026-08-05 把讀取端的比較改成大小寫不敏感,止住了症狀。
**這一支處理的是資料本身**:

1. 拿掉 ``ingestion_collections.embedding_model`` 的欄位預設值。
   ORM(``app/models/ingestion.py``)本來就沒有宣告 server_default,
   而樹裡每一條寫入路徑都明確給值(唯一一處是
   ``app/api/ingestion/collections.py`` 的 ``create_collection``),
   所以這個預設值只服務「忘了給值」的寫入——而它能給的只是一個
   對某次部署的猜測,猜錯就是上面那個缺陷。拿掉之後 NOT NULL 會讓
   那種寫入**當場失敗**,而不是靜靜地寫進一個錯的來源模型。
2. 把四個欄位裡「只差大小寫」的值改寫成 ``model_registry`` 的拼法。

候選只取 ``model_type='embedding'`` 的註冊列——這幾欄記的是「哪顆模型產生了這個向量」,
聊天模型不可能產生,而一列剛好同名的 llm 會讓 distinct 數變 2、把真正的 embedder 整批跳過。
**故意不濾 `is_active`**:已停用的 embedder 仍然是它產出的那些列的正確名字
(#56 第 9 條與 409 訊息都是叫操作者重新指定回那顆、必要時重新啟用)。
只在**恰好一種**註冊拼法能對上時才改寫。``model_registry.name`` 沒有
大小寫不敏感的唯一性(#56 第 8 條),兩個只差大小寫的列建得出來;
真的碰到就不猜,原值留著。

**沒改的也要說**:每張表跑完會再數兩個數字並記一行——「拼法有歧義而不敢猜」的列數、
以及「根本沒有對應的 embedding 註冊列」的列數。只記改了幾列,等於教操作者
把沉默當成「都乾淨了」,而那正是這一條要消滅的形狀。

RLS
===
``document_chunks`` / ``ingestion_images`` 是 ENABLE + FORCE RLS,
policy 鍵在 ``anila.collection_id`` GUC(0019 / 0037)。FORCE 表示
連 table owner 都吃 policy,所以**這支不假設 migration 角色是
superuser**:逐一集合設 GUC 再改寫,非 superuser 角色跑起來結果相同。
``conversation_memory_chunks`` 沒有 RLS(見 FAKE-CONTROLS #52),直接改。

可逆性
======
**資料部分不可逆**——原本的大小寫在改寫後不存在於任何地方,
無從還原。``downgrade()`` 只還原欄位預設值,並在此明說這件事。
重跑 ``upgrade()`` 是安全的:改寫條件含 ``<> canonical``,
第二次跑不會動到任何列。

Revision ID: r1_0032
Revises: r1_0031
Create Date: 2026-08-07
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0032"
down_revision: Union[str, None] = "r1_0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# The 0014 default this migration removes. Restored by downgrade().
_OLD_COLUMN_DEFAULT = "nvidia/NV-embed-V2"

# Only an **embedding** registration can be the canonical spelling of a
# value in these columns: they record which model produced a vector, and
# a chat model cannot have produced one. Without this filter an unrelated
# ``model_type='llm'`` row named ``NVIDIA/NV-Embed-V2`` pushes the
# distinct-name count for that case-folded key to 2 and the real
# embedder's rows are silently skipped.
#
# ``is_active`` is deliberately NOT filtered. A deactivated embedder is
# still the correct name for the vectors it already produced — #56 item 9
# and the 409 message both tell the operator to re-designate (and if
# necessary reactivate) exactly that model. Excluding it would strand the
# corpus it built. ``is_active`` governs which model may be *chosen*, not
# how an existing row is *spelled*.
#
# ``count(DISTINCT name) = 1`` then drops what remains ambiguous
# (#56 item 8) so nothing is guessed.
_CANDIDATES = "SELECT name FROM model_registry WHERE model_type = 'embedding'"

_CANONICAL = f"""
    SELECT lower(name) AS lname, min(name) AS canonical
      FROM ({_CANDIDATES}) AS c
     GROUP BY lower(name)
    HAVING count(DISTINCT name) = 1
"""

# The complement: case-folded keys the registry spells more than one way.
# Rows on these are left alone and reported, never guessed at.
_AMBIGUOUS = f"""
    SELECT lower(name) AS lname
      FROM ({_CANDIDATES}) AS c
     GROUP BY lower(name)
    HAVING count(DISTINCT name) > 1
"""

# (table, column) pairs whose rows are reachable without the RLS GUC.
_UNSCOPED = (
    ("ingestion_collections", "embedding_model"),
    ("conversation_memory_chunks", "embedding_source_model"),
)

# Same, but behind the ``anila.collection_id`` policy.
_COLLECTION_SCOPED = (
    ("document_chunks", "embedding_source_model"),
    ("ingestion_images", "embedding_source_model"),
)


def _rewrite_sql(table: str, column: str, *, scoped: bool) -> str:
    scope = "   AND t.collection_id = :cid\n" if scoped else ""
    return (
        f"UPDATE {table} AS t\n"
        f"   SET {column} = m.canonical\n"
        f"  FROM ({_CANONICAL}) AS m\n"
        f" WHERE lower(t.{column}) = m.lname\n"
        f"   AND t.{column} <> m.canonical\n" + scope
    )


def _left_sql(table: str, column: str, *, scoped: bool) -> str:
    """Count the rows this migration decided NOT to touch.

    A data fix that only ever logs what it changed teaches the operator
    that silence means "all clean" — which is the exact habit this whole
    entry (#56) exists to break. Two categories, because they mean
    different things:

    * ``ambiguous`` — the registry spells this case-folded name more than
      one way, so there is no single right answer and we refuse to pick.
      The runtime path warns on the same condition
      (``platform_embedding.py``), so the two agree.
    * ``unregistered`` — no embedding registration matches at all. These
      rows are not a casing problem; they are a corpus indexed under a
      model this platform no longer knows, and retrieval will find
      nothing in them whatever we do to their capitalisation.
    """
    scope = "   AND t.collection_id = :cid\n" if scoped else ""
    return (
        f"SELECT\n"
        f"  count(*) FILTER (\n"
        f"    WHERE lower(t.{column}) IN (SELECT lname FROM ({_AMBIGUOUS}) AS a)\n"
        f"  ) AS ambiguous,\n"
        f"  count(*) FILTER (\n"
        f"    WHERE NOT EXISTS (\n"
        f"      SELECT 1 FROM ({_CANDIDATES}) AS r\n"
        f"       WHERE lower(r.name) = lower(t.{column})\n"
        f"    )\n"
        f"  ) AS unregistered\n"
        f"  FROM {table} AS t\n"
        f" WHERE t.{column} IS NOT NULL\n" + scope
    )


def _report_left(table: str, ambiguous: int, unregistered: int) -> None:
    if not ambiguous and not unregistered:
        logger.info("r1_0032: %s — nothing left unfixed", table)
        return
    logger.warning(
        "r1_0032: %s — LEFT UNFIXED: %d row(s) whose model name the "
        "registry spells more than one way (ambiguous, not guessed at), "
        "%d row(s) naming no registered embedding model at all. These "
        "rows keep the spelling they had; retrieval still relies on the "
        "case-insensitive comparison for them.",
        table,
        ambiguous,
        unregistered,
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── 1. Drop the column default that seeded the collision ────────────────
    if "ingestion_collections" in tables:
        op.execute(
            "ALTER TABLE ingestion_collections "
            "ALTER COLUMN embedding_model DROP DEFAULT"
        )

    if "model_registry" not in tables:
        # Nothing to canonicalise against; the default drop above is the
        # whole migration on such a database.
        return

    # ── 2. Canonicalise the rows RLS does not hide ──────────────────────────
    for table, column in _UNSCOPED:
        if table not in tables:
            continue
        result = bind.execute(sa.text(_rewrite_sql(table, column, scoped=False)))
        logger.info(
            "r1_0032: %s.%s — %d row(s) recased to the model_registry spelling",
            table,
            column,
            result.rowcount,
        )
        left = bind.execute(sa.text(_left_sql(table, column, scoped=False))).one()
        _report_left(table, int(left[0]), int(left[1]))

    # ── 3. Canonicalise chunk provenance, one collection at a time ──────────
    # FORCE RLS applies to the table owner too, so a non-superuser
    # migration role would silently update zero rows without the GUC.
    # Setting it per collection makes this correct under both roles.
    scoped_tables = [t for t, _ in _COLLECTION_SCOPED if t in tables]
    if not scoped_tables or "ingestion_collections" not in tables:
        return

    collection_ids = [
        int(row[0])
        for row in bind.execute(
            sa.text("SELECT id FROM ingestion_collections ORDER BY id")
        ).fetchall()
    ]
    touched = {table: 0 for table in scoped_tables}
    left = {table: [0, 0] for table in scoped_tables}
    try:
        for cid in collection_ids:
            bind.execute(
                sa.text("SELECT set_config('anila.collection_id', :cid, false)"),
                {"cid": str(cid)},
            )
            for table, column in _COLLECTION_SCOPED:
                if table not in tables:
                    continue
                result = bind.execute(
                    sa.text(_rewrite_sql(table, column, scoped=True)), {"cid": cid}
                )
                touched[table] += result.rowcount or 0
                row = bind.execute(
                    sa.text(_left_sql(table, column, scoped=True)), {"cid": cid}
                ).one()
                left[table][0] += int(row[0])
                left[table][1] += int(row[1])
    finally:
        bind.execute(sa.text("SELECT set_config('anila.collection_id', '', false)"))

    for table, count in touched.items():
        logger.info(
            "r1_0032: %s.embedding_source_model — %d row(s) recased across %d "
            "collection(s)",
            table,
            count,
            len(collection_ids),
        )
        _report_left(table, left[table][0], left[table][1])


def downgrade() -> None:
    """Restore the 0014 column default only.

    The recasing is **not** reversed and cannot be: the original
    spellings were overwritten in place and are not recorded anywhere.
    Reversing it would mean re-introducing the exact collision this
    migration exists to remove, which is not a state worth being able
    to return to.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ingestion_collections" in set(inspector.get_table_names()):
        op.execute(
            "ALTER TABLE ingestion_collections "
            "ALTER COLUMN embedding_model SET DEFAULT "
            f"'{_OLD_COLUMN_DEFAULT}'"
        )
