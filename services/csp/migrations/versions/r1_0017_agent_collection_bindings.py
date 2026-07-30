# -*- coding: utf-8 -*-
"""P4.7 — agent_collection_bindings（agent 多知識庫綁定）.

SYSTEM-MAP §4：「一個 agent 可以綁多個知識庫」。既有
``agents.bound_collection_id`` 單數 FK 無法表達「通用庫 + 專案庫」；
OE-2 缺口 G6 / PLAN 4.7。

本 migration 建 junction 表（複合 PK，同 ``user_agent_permissions`` 形狀），
並把既有單數綁定搬進表內。``agents.bound_collection_id`` 保留為衍生相容
鏡像（寫入路徑同步為 min(ids) 或 NULL），不在此刪欄。

Mirror invariant
================

``agents.bound_collection_id`` must always name a member of the agent's
junction set (or be NULL when the set is empty). App writers keep this
via ``set_bound_collection_ids``. Collection deletion cascades the
junction row away while the scalar FK is ``ON DELETE SET NULL``, which
would otherwise leave set={surviving} and mirror=NULL. A trigger on
``agent_collection_bindings`` recomputes the mirror after every INSERT
or DELETE so the FK cascade path cannot drift.

Downgrade
=========

Before dropping the junction table, collapse each agent's set into the
scalar column (``MIN(collection_id)``). Set→scalar is lossy and that is
accepted; silently unbinding an agent that still had bindings is not.
A subsequent upgrade then restores a non-empty singleton from the
collapsed scalar.

Idempotency
===========

Upgrade 用 raw ``CREATE TABLE IF NOT EXISTS``，並以
``WHERE NOT EXISTS`` 回填，容忍 ``create_all`` 已建表／已回填的 DB。
Downgrade 同樣 ``IF EXISTS``。Trigger／function 用 ``CREATE OR REPLACE``
／``DROP IF EXISTS``。

Revision ID: r1_0017
Revises: r1_0016
"""

from typing import Sequence, Union

from alembic import op

revision: str = "r1_0017"
down_revision: Union[str, None] = "r1_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Recompute agents.bound_collection_id = MIN(junction) or NULL whenever
# the set changes at the DB level (collection CASCADE delete included).
_MIRROR_SYNC_FN = """
CREATE OR REPLACE FUNCTION sync_agent_bound_collection_mirror()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
DECLARE
  aid INTEGER := COALESCE(NEW.agent_id, OLD.agent_id);
BEGIN
  UPDATE agents
     SET bound_collection_id = (
       SELECT MIN(collection_id)
         FROM agent_collection_bindings
        WHERE agent_id = aid
     )
   WHERE id = aid;
  RETURN COALESCE(NEW, OLD);
END;
$fn$
"""

_MIRROR_SYNC_TRG = """
DROP TRIGGER IF EXISTS trg_acb_sync_bound_collection_mirror
  ON agent_collection_bindings;
CREATE TRIGGER trg_acb_sync_bound_collection_mirror
  AFTER INSERT OR DELETE ON agent_collection_bindings
  FOR EACH ROW
  EXECUTE FUNCTION sync_agent_bound_collection_mirror()
"""


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_collection_bindings (
            agent_id INTEGER NOT NULL,
            collection_id INTEGER NOT NULL,
            PRIMARY KEY (agent_id, collection_id),
            CONSTRAINT fk_agent_collection_bindings_agent
              FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
            CONSTRAINT fk_agent_collection_bindings_collection
              FOREIGN KEY (collection_id) REFERENCES ingestion_collections(id)
              ON DELETE CASCADE
        )
        """
    )
    # Carry existing single bindings across — skip rows already present
    # (idempotent re-run / create_all + partial backfill).
    op.execute(
        """
        INSERT INTO agent_collection_bindings (agent_id, collection_id)
        SELECT a.id, a.bound_collection_id
          FROM agents a
         WHERE a.bound_collection_id IS NOT NULL
           AND NOT EXISTS (
             SELECT 1 FROM agent_collection_bindings b
              WHERE b.agent_id = a.id
                AND b.collection_id = a.bound_collection_id
           )
        """
    )
    op.execute(_MIRROR_SYNC_FN)
    op.execute(_MIRROR_SYNC_TRG)


def downgrade() -> None:
    # Collapse set → scalar BEFORE drop so a later upgrade restores a
    # non-empty binding for every agent that still had junction rows.
    # Lossy (keeps MIN only); must not leave previously-bound agents NULL.
    op.execute(
        """
        UPDATE agents AS a
           SET bound_collection_id = sub.m
          FROM (
            SELECT agent_id, MIN(collection_id) AS m
              FROM agent_collection_bindings
             GROUP BY agent_id
          ) AS sub
         WHERE a.id = sub.agent_id
           AND a.bound_collection_id IS DISTINCT FROM sub.m
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_acb_sync_bound_collection_mirror "
        "ON agent_collection_bindings"
    )
    op.execute("DROP TABLE IF EXISTS agent_collection_bindings")
    op.execute("DROP FUNCTION IF EXISTS sync_agent_bound_collection_mirror()")
