"""試跑唯一請求索引與執行權租約。

Revision ID: r1_0077
Revises: r1_0076
"""

import re
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "r1_0077"
down_revision = "r1_0076"
branch_labels = None
depends_on = None

J = sa.JSON().with_variant(JSONB, "postgresql")
HASH = re.compile(r"[0-9a-f]{64}\Z")
COLUMNS = (
    "is_test", "test_request_id", "test_request_hash",
    "test_actor_department_id", "execution_owner_token",
    "lease_expires_at", "lease_heartbeat_at",
)


def _marker(result):
    value = result.get("test_tracking") if isinstance(result, dict) else None
    if not isinstance(value, dict) or set(value) != {
        "testing", "request_id", "request_hash", "actor_department_id"
    } or value["testing"] is not True:
        return None
    nonce = value["request_id"]
    if nonce is not None:
        if not isinstance(nonce, str):
            return None
        try:
            if str(UUID(nonce)) != nonce:
                return None
        except ValueError:
            return None
    digest, department = value["request_hash"], value["actor_department_id"]
    if not isinstance(digest, str) or not HASH.fullmatch(digest):
        return None
    if department is not None and (type(department) is not int or department <= 0):
        return None
    return {
        "is_test": True,
        "test_request_id": nonce,
        "test_request_hash": digest,
        "test_actor_department_id": department,
    }


def _old_runs():
    return sa.table("quick_runs", sa.column("id", sa.Integer),
                    sa.column("user_id", sa.Integer), sa.column("result", J))


def _rows(bind, table):
    result = bind.execute(sa.select(table).execution_options(stream_results=True))
    try:
        while batch := result.fetchmany(500):
            yield from batch
    finally:
        result.close()


def upgrade():
    bind = op.get_bind()
    old = _old_runs()
    seen = set()
    for row in _rows(bind, old):
        value = _marker(row.result)
        if value and value["test_request_id"] is not None:
            pair = (row.user_id, value["test_request_id"])
            if pair in seen:
                raise RuntimeError(
                    "試跑請求識別碼重複，已停止遷移並保留原紀錄；"
                    "請先確認重複紀錄，不得重新執行（DUPLICATE_TEST_REQUEST）"
                )
            seen.add(pair)

    op.add_column("quick_runs", sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.false()))
    for name, kind in (
        ("test_request_id", sa.String(36)), ("test_request_hash", sa.String(64)),
        ("test_actor_department_id", sa.Integer()),
        ("execution_owner_token", sa.String(36)),
        ("lease_expires_at", sa.DateTime(timezone=True)),
        ("lease_heartbeat_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("quick_runs", sa.Column(name, kind, nullable=True))
    runs = _old_runs()
    for name in COLUMNS:
        runs.append_column(sa.column(name))
    # 舊試跑只建立查詢指標，不簽發執行權或重啟模型呼叫。
    for row in _rows(bind, old):
        value = _marker(row.result)
        if value:
            bind.execute(runs.update().where(runs.c.id == row.id).values(**value))
    with op.batch_alter_table("quick_runs") as batch:
        batch.create_unique_constraint("uq_quick_run_user_nonce", ["user_id", "test_request_id"])
    op.create_index("ix_quick_run_test_lease", "quick_runs", ["is_test", "status", "lease_expires_at"])
    op.create_index("ix_quick_run_test_lookup", "quick_runs", ["user_id", "is_test", "version_id", "id"])


def downgrade():
    bind = op.get_bind()
    runs = sa.table("quick_runs", sa.column("status"), sa.column("is_test", sa.Boolean),
                    sa.column("execution_owner_token"))
    active = bind.scalar(sa.select(sa.func.count()).select_from(runs).where(
        runs.c.is_test.is_(True), runs.c.status == "running",
        runs.c.execution_owner_token.is_not(None),
    ))
    if active:
        raise RuntimeError(
            "仍有試跑持有執行權，已停止降版；請先停止接收工作並結束執行"
            "（TEST_EXECUTION_ACTIVE）"
        )
    op.drop_index("ix_quick_run_test_lookup", table_name="quick_runs")
    op.drop_index("ix_quick_run_test_lease", table_name="quick_runs")
    with op.batch_alter_table("quick_runs") as batch:
        batch.drop_constraint("uq_quick_run_user_nonce", type_="unique")
        for name in reversed(COLUMNS):
            batch.drop_column(name)
