"""r1_0073：差評結案欄位可以加上、也可以拿掉。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "r1_0073_feedback_resolution.py"
    )
    spec = importlib.util.spec_from_file_location("r1_0073_feedback_resolution", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(engine, fn) -> None:
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            fn()


def test_revision_follows_the_current_head_and_names_the_three_fields():
    module = _migration()
    assert module.revision == "r1_0073"
    assert module.down_revision == "r1_0072"
    source = Path(module.__file__).read_text(encoding="utf-8")
    for column in ("feedback_status", "feedback_resolution", "feedback_resolved_at"):
        assert column in source
    assert "ck_messages_feedback_status" in source
    assert "ck_messages_feedback_wont_fix_has_note" in source
    assert "notifications" in source


def test_upgrade_and_downgrade_on_sqlite(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback.db'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE messages (id INTEGER PRIMARY KEY)"))
    module = _migration()
    _apply(engine, module.upgrade)
    columns = {col["name"] for col in inspect(engine).get_columns("messages")}
    assert {
        "feedback_status",
        "feedback_resolution",
        "feedback_resolution_note",
        "feedback_resolved_at",
        "feedback_submitter_id",
    } <= columns
    indexes = {idx["name"] for idx in inspect(engine).get_indexes("messages")}
    assert "ix_messages_feedback_status" in indexes
    fks = inspect(engine).get_foreign_keys("messages")
    assert any(fk.get("referred_table") == "users" for fk in fks)

    _apply(engine, module.downgrade)
    after = {col["name"] for col in inspect(engine).get_columns("messages")}
    assert "feedback_status" not in after
    assert "feedback_resolution" not in after
    assert "feedback_resolved_at" not in after
