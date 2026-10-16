"""試跑索引與執行權欄位的隔離遷移及資料保全。"""

import hashlib
import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


def migration():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/r1_0077_quick_test_lifecycle.py"
    spec = importlib.util.spec_from_file_location("quick_test_lifecycle_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply(engine, fn):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            fn()


def old_db(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    meta = sa.MetaData()
    sa.Table("users", meta, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table("quick_versions", meta, sa.Column("id", sa.Integer, primary_key=True))
    runs = sa.Table("quick_runs", meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("version_id", sa.Integer, sa.ForeignKey("quick_versions.id"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
    )
    meta.create_all(engine)
    return engine, runs


def marker(nonce):
    return {"testing": True, "request_id": nonce,
            "request_hash": hashlib.sha256(b'synthetic').hexdigest(), "actor_department_id": 5}


def insert(engine, runs, values):
    with engine.begin() as conn:
        conn.execute(runs.insert(), values)


def column_snapshot(engine):
    return [{**column, "type": str(column["type"])}
            for column in sa.inspect(engine).get_columns("quick_runs")]


def test_upgrade_backfills_exact_markers_and_downgrade_preserves_rows(tmp_path):
    engine, runs = old_db(tmp_path)
    nonce = str(uuid4())
    originals = [
        dict(id=1, user_id=1, version_id=3, status="completed", result={"steps": [], "test_tracking": marker(nonce)}),
        dict(id=2, user_id=1, version_id=3, status="running", result={"test_tracking": marker(None)}),
        dict(id=3, user_id=2, version_id=3, status="completed", result={"test_tracking": marker(nonce)}),
        dict(id=4, user_id=1, version_id=3, status="completed", result={"steps": [], "output": "runtime"}),
        dict(id=5, user_id=1, version_id=3, status="running", result={"test_tracking": {**marker(nonce), "unexpected": True}}),
    ]
    insert(engine, runs, originals)
    module = migration()
    assert (module.revision, module.down_revision) == ("r1_0077", "r1_0076")
    apply(engine, module.upgrade)
    table = sa.Table("quick_runs", sa.MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        rows = conn.execute(sa.select(table).order_by(table.c.id)).mappings().all()
    assert [r["is_test"] for r in rows] == [True, True, True, False, False]
    assert rows[0]["test_request_id"] == nonce and rows[1]["test_request_id"] is None
    assert all(r["execution_owner_token"] is None and r["lease_expires_at"] is None for r in rows)
    assert [r["result"] for r in rows] == [r["result"] for r in originals]
    indexes = {i["name"] for i in sa.inspect(engine).get_indexes("quick_runs")}
    assert {"ix_quick_run_test_lease", "ix_quick_run_test_lookup"} <= indexes
    assert {c["name"] for c in sa.inspect(engine).get_unique_constraints("quick_runs")} == {"uq_quick_run_user_nonce"}
    assert len(sa.inspect(engine).get_foreign_keys("quick_runs")) == 2
    apply(engine, module.downgrade)
    assert not (set(module.COLUMNS) & {c["name"] for c in sa.inspect(engine).get_columns("quick_runs")})
    with engine.connect() as conn:
        rows = conn.execute(sa.select(runs).order_by(runs.c.id)).mappings().all()
    assert [r["result"] for r in rows] == [r["result"] for r in originals]
    assert [r["status"] for r in rows] == [r["status"] for r in originals]


def test_duplicate_legacy_nonce_stops_before_any_ddl(tmp_path):
    engine, runs = old_db(tmp_path)
    nonce = str(uuid4())
    original = [dict(id=i, user_id=1, version_id=3, status="completed", result={"test_tracking": marker(nonce)}) for i in (1, 2)]
    insert(engine, runs, original)
    before = column_snapshot(engine)
    with pytest.raises(RuntimeError, match="DUPLICATE_TEST_REQUEST") as raised:
        apply(engine, migration().upgrade)
    assert nonce not in str(raised.value)
    assert column_snapshot(engine) == before
    with engine.connect() as conn:
        assert conn.scalar(sa.select(sa.func.count()).select_from(runs)) == 2


def test_unique_nonce_blocks_same_owner_but_allows_legacy_nulls(tmp_path):
    engine, _ = old_db(tmp_path)
    apply(engine, migration().upgrade)
    runs = sa.Table("quick_runs", sa.MetaData(), autoload_with=engine)
    nonce = str(uuid4())
    base = dict(user_id=1, version_id=3, status="running", result={}, is_test=True)
    insert(engine, runs, [{**base, "test_request_id": nonce}])
    with pytest.raises(IntegrityError):
        insert(engine, runs, [{**base, "test_request_id": nonce}])
    insert(engine, runs, [{**base, "test_request_id": None}, {**base, "test_request_id": None}, {**base, "user_id": 2, "test_request_id": nonce}])


def test_downgrade_refuses_live_execution_authority_before_ddl(tmp_path):
    engine, _ = old_db(tmp_path)
    module = migration()
    apply(engine, module.upgrade)
    runs = sa.Table("quick_runs", sa.MetaData(), autoload_with=engine)
    insert(engine, runs, [dict(user_id=1, version_id=3, status="running", result={}, is_test=True,
                             execution_owner_token=str(uuid4()))])
    before = column_snapshot(engine)
    with pytest.raises(RuntimeError, match="TEST_EXECUTION_ACTIVE"):
        apply(engine, module.downgrade)
    assert column_snapshot(engine) == before


@pytest.mark.parametrize("change", [
    {"request_id": "A" * 36}, {"request_id": str(uuid4()).upper()},
    {"request_hash": "BAD"}, {"actor_department_id": True},
    {"actor_department_id": 0}, {"testing": 1},
])
def test_invalid_marker_is_not_adopted(change):
    module = migration()
    assert module._marker({"test_tracking": {**marker(str(uuid4())), **change}}) is None
