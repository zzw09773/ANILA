"""ingestion-worker 金鑰核發的鎖在 PostgreSQL 上要能執行。

測試環境是 SQLite，它會直接丟掉 FOR UPDATE，所以 2026-09-26 上線時才撞到
「FOR UPDATE cannot be applied to the nullable side of an outer join」。
這裡用 PostgreSQL 方言編譯同一條查詢，確認鎖只落在 users。
"""
from sqlalchemy.dialects import postgresql

from app.models.user import User


def test_worker_user_lock_targets_users_only(db):
    query = (
        db.query(User)
        .filter(User.username == "ingestion-worker")
        .with_for_update(of=User)
    )
    sql = str(query.statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF users" in sql


def test_provisioner_uses_scoped_lock():
    import inspect

    from app.services import internal_service_clients

    source = inspect.getsource(internal_service_clients._ensure_api_key)
    assert ".with_for_update(of=User)" in source
    assert ".with_for_update()\n" not in source
