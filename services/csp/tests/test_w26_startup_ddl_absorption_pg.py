"""真 PostgreSQL 證據:startup DDL 折回 alembic(W2-6)。

為什麼一定要真 PG
-----------------
本包要修的三件事在 SQLite 上**結構上測不到**:

1. ``r1_0036`` 的冪等性只有在「被舊 startup DDL 補過欄位的既有庫」上才有意義,
   而那個庫的形狀是 PG 專屬的(``ADD COLUMN IF NOT EXISTS``、
   ``information_schema``、UNIQUE 約束 vs unique 索引的差別)。
2. ``alerts.fingerprint`` 的併發去重靠的是 DB 唯一索引把兩路交易序列化。
   單元測試的 SQLite 是 ``StaticPool`` 單連線,跨交易競態不存在。
3. ``verify_schema`` 拒啟動的對象是生產庫。

執行方式(需要一個**獨立、可丟棄**的 PostgreSQL,superuser DSN)::

    docker run -d --rm --name throwaway-pg -e POSTGRES_PASSWORD=x \\
        -e POSTGRES_DB=csp -p 55432:5432 pgvector/pgvector:pg16
    ANILA_TEST_PG_DSN=postgresql://postgres:x@127.0.0.1:55432/csp \\
        python -m pytest tests/test_w26_startup_ddl_absorption_pg.py

⚠ 每個測試自建/自刪**自己的資料庫**(名字帶 uuid),不碰 DSN 指到的那個庫的
內容。仍然請只對可丟棄的實例跑 —— 它會 ``CREATE DATABASE`` / ``DROP DATABASE``。
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.services.alert_service import upsert_alert
from app.services.startup_migrations import (
    SchemaOutOfDateError,
    collect_schema_gaps,
    verify_schema,
)

_DSN = os.environ.get("ANILA_TEST_PG_DSN") or os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN / TEST_POSTGRES_URL not set —— 需要可丟棄的真 PostgreSQL",
)

CSP_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = CSP_ROOT / "migrations"

_PRE_R1_0036_HEAD = "r1_0035"


# ── `.15` 既有庫的形狀:折回前 startup_migrations 會跑的每一條 DDL ──────────
#
# 刻意逐字內嵌而不 import —— 那些函式已隨 W2-6 刪除,而這裡要重建的是**歷史
# 事實**(內網 `.15` 與各 dev 庫被補成什麼樣子),不是現行程式碼的行為。
# 對照來源:`startup_migrations.py` @ 38e0ece 的 `_ensure_schema_backfills`。
_LEGACY_STARTUP_DDL: tuple[str, ...] = (
    # users
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS department_id INTEGER"
    " REFERENCES departments(id) ON DELETE SET NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
    # model_registry
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS health_status VARCHAR(20) DEFAULT 'offline'",
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS health_checked_at TIMESTAMP NULL",
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS context_window INTEGER NULL",
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS base_model_id INTEGER"
    " REFERENCES model_registry(id)",
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS is_internal BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE model_registry ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
    # token_usage
    "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS department_id INTEGER"
    " REFERENCES departments(id)",
    "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS request_timestamp"
    " TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS request_duration_ms INTEGER NULL",
    "CREATE INDEX IF NOT EXISTS idx_usage_user_time ON token_usage (user_id, request_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_usage_department_time ON token_usage (department_id, request_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_usage_model_time ON token_usage (model_id, request_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON token_usage (request_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_usage_apikey_time ON token_usage (api_key_id, request_timestamp)",
    # departments
    "ALTER TABLE departments ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NULL",
    # api_keys
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP NULL",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_suffix VARCHAR(4) NOT NULL DEFAULT ''",
    # alerts —— 12 欄,無 index / 無 UNIQUE / 無 FK(這就是病灶)
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS category VARCHAR(50) NOT NULL DEFAULT 'general'",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS severity VARCHAR(20) NOT NULL DEFAULT 'info'",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'open'",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(200) NOT NULL DEFAULT ''",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS source_id VARCHAR(100) NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMP NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS acknowledged_by_user_id INTEGER NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMP NULL",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS metadata_json TEXT NULL",
    # audit_logs
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'ok'",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_user_id INTEGER NULL",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS actor_username VARCHAR(100) NULL",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS ip_address VARCHAR(64) NULL",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS metadata_json TEXT NULL",
    # audit_logs.resource_id 的啟動時 ALTER TYPE(startup_migrations.py:245-247)
    "ALTER TABLE audit_logs ALTER COLUMN resource_id TYPE VARCHAR(100)"
    " USING resource_id::varchar",
    # platform_links
    "ALTER TABLE platform_links ADD COLUMN IF NOT EXISTS icon VARCHAR(50) NULL",
    "ALTER TABLE platform_links ADD COLUMN IF NOT EXISTS sort_order INTEGER NULL DEFAULT 0",
)


def _with_database(name: str) -> str:
    # ⚠ ``str(URL)`` 會把密碼遮成 ``***``(SQLAlchemy 2.0 的預設),直接拿去連線
    # 一定得到 "password authentication failed"。必須明示 hide_password=False。
    return make_url(_DSN).set(database=name).render_as_string(hide_password=False)


def _maintenance_url() -> str:
    return _with_database("postgres")


def _db_url(name: str) -> str:
    return _with_database(name)


def _alembic_config(url: str) -> Config:
    # 刻意用「沒有 config file」的 Config:``env.py`` 只有在
    # ``config_file_name`` 非 None 時才呼叫 ``fileConfig()``,而 alembic.ini 的
    # [loggers] 會把 pytest 的 logging handler 全部 disable(CLAUDE.md 記過這個
    # footgun)。
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _run_alembic(url: str, verb: str, target: str) -> None:
    previous = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = url
    try:
        getattr(command, verb)(_alembic_config(url), target)
    finally:
        if previous is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = previous


@pytest.fixture()
def throwaway_db():
    """建立一個獨立資料庫,結束時刪掉。回傳它的 URL。"""
    name = f"w26_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_maintenance_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield _db_url(name)
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _seed_legacy_alerts(engine) -> dict[str, int]:
    """種下 `.15` 那種「fingerprint 全是空字串 + 真重複」的歷史資料。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active)"
                " VALUES ('ops', 'x', 'admin', TRUE)"
            )
        )
        actor_id = connection.execute(
            text("SELECT id FROM users WHERE username = 'ops'")
        ).scalar_one()

        # (a) fingerprint 欄位加上之前就存在的三筆 → DEFAULT '' 全撞在一起
        for title in ("磁碟將滿", "憑證即將到期", "備份逾期"):
            connection.execute(
                text(
                    "INSERT INTO alerts (title, message, alert_type, is_active)"
                    " VALUES (:t, :t, 'warning', TRUE)"
                ),
                {"t": title},
            )

        # (b) 同一個非空 fingerprint 真的有兩列 —— read-then-write 競態的殘留
        connection.execute(
            text(
                "INSERT INTO alerts (title, message, alert_type, is_active,"
                " fingerprint, last_seen_at)"
                " VALUES ('模型離線(舊)', 'old', 'error', TRUE,"
                " 'health:model:7', TIMESTAMP '2026-01-01 00:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO alerts (title, message, alert_type, is_active,"
                " fingerprint, last_seen_at)"
                " VALUES ('模型離線(新)', 'new', 'error', TRUE,"
                " 'health:model:7', TIMESTAMP '2026-06-30 00:00:00')"
            )
        )

        # (c) acknowledged_by_user_id 指向已刪除的使用者 → 建 FK 前必須清掉
        connection.execute(
            text(
                "INSERT INTO alerts (title, message, alert_type, is_active,"
                " fingerprint, acknowledged_by_user_id)"
                " VALUES ('孤兒簽收', 'orphan', 'error', TRUE,"
                " 'health:agent:9', 999999)"
            )
        )
        # (d) 合法簽收 → 必須被保留
        connection.execute(
            text(
                "INSERT INTO alerts (title, message, alert_type, is_active,"
                " fingerprint, acknowledged_by_user_id)"
                " VALUES ('合法簽收', 'kept', 'error', TRUE,"
                " 'health:agent:10', :a)"
            ),
            {"a": actor_id},
        )
    return {"actor_id": actor_id}


def _unique_constraint_columns(engine, table: str) -> set[tuple[str, ...]]:
    insp = inspect(engine)
    return {tuple(uc["column_names"]) for uc in insp.get_unique_constraints(table)}


def _index_names(engine, table: str) -> set[str]:
    insp = inspect(engine)
    return {ix["name"] for ix in insp.get_indexes(table)}


def test_r1_0036_is_idempotent_on_a_startup_ddl_healed_database(throwaway_db):
    """⚠ 最大的風險:`.15` 的欄位已被 startup DDL 補過,migration 不能重建。

    這個測試把 `.15` 的形狀重建出來(r1_0035 + 舊 startup DDL + 歷史髒資料),
    然後跑 ``alembic upgrade head``。它同時證明四件事:

    * 欄位/索引已存在 → 不炸(``IF NOT EXISTS`` 姿態成立)
    * ``fingerprint = ''`` 的哨兵列**不被刪除**,只換成唯一的合成指紋
    * 真重複只保留最新一列
    * 孤兒 ``acknowledged_by_user_id`` 被清成 NULL,FK 才建得起來;合法簽收保留
    """
    _run_alembic(throwaway_db, "upgrade", _PRE_R1_0036_HEAD)

    engine = create_engine(throwaway_db)
    with engine.begin() as connection:
        for ddl in _LEGACY_STARTUP_DDL:
            connection.execute(text(ddl))
    seeded = _seed_legacy_alerts(engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM alerts")).scalar() == 7
        # 前提確認:此時既沒有 UNIQUE 也沒有 FK —— 這就是生產庫的現況
        assert ("fingerprint",) not in _unique_constraint_columns(engine, "alerts")

    # ── 這一步在沒有去重的 migration 下會炸 duplicate key ────────────────
    _run_alembic(throwaway_db, "upgrade", "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, title, fingerprint, acknowledged_by_user_id FROM alerts"
                 " ORDER BY id")
        ).all()

    titles = {r.title for r in rows}
    fingerprints = [r.fingerprint for r in rows]

    # (a) 三筆空指紋的歷史告警全部保留(刪掉就是無紀錄的資料銷毀)
    assert {"磁碟將滿", "憑證即將到期", "備份逾期"} <= titles
    legacy = [f for f in fingerprints if f.startswith("legacy:")]
    assert len(legacy) == 3
    assert len(set(legacy)) == 3

    # (b) 真重複只留最新那筆
    assert "模型離線(新)" in titles
    assert "模型離線(舊)" not in titles
    assert fingerprints.count("health:model:7") == 1

    # (c)/(d) 孤兒清成 NULL、合法簽收保留
    by_title = {r.title: r for r in rows}
    assert by_title["孤兒簽收"].acknowledged_by_user_id is None
    assert by_title["合法簽收"].acknowledged_by_user_id == seeded["actor_id"]

    assert len(rows) == 6  # 7 - 1(重複)
    assert len(fingerprints) == len(set(fingerprints))

    # 結構:UNIQUE 約束、索引、FK 都補齊了
    assert ("fingerprint",) in _unique_constraint_columns(engine, "alerts")
    assert {
        "ix_alerts_category",
        "ix_alerts_severity",
        "ix_alerts_source_type",
        "ix_alerts_status",
        "ix_alerts_last_seen_at",
    } <= _index_names(engine, "alerts")
    assert "ix_users_department_id" in _index_names(engine, "users")
    assert "ix_attachments_message_id" in _index_names(engine, "attachments")
    fk_columns = {
        tuple(fk["constrained_columns"])
        for fk in inspect(engine).get_foreign_keys("alerts")
    }
    assert ("acknowledged_by_user_id",) in fk_columns

    engine.dispose()


def test_r1_0036_can_be_replayed_on_an_already_migrated_database(throwaway_db):
    """真正的冪等性:對已經套用過本 revision 的庫再跑一次,不得失敗。

    做法是 ``alembic stamp r1_0035`` 把版本指針退回去(schema 不動),再
    ``upgrade head`` —— 於是 ``r1_0036.upgrade()`` 在「所有欄位/索引/約束都已
    存在」的庫上重跑一次。這正是 `.15` 上任何一次意外重跑會遇到的情況。
    """
    _run_alembic(throwaway_db, "upgrade", "head")

    engine = create_engine(throwaway_db)
    before = inspect(engine).get_columns("alerts")

    _run_alembic(throwaway_db, "stamp", _PRE_R1_0036_HEAD)
    _run_alembic(throwaway_db, "upgrade", "head")

    after = inspect(engine).get_columns("alerts")
    assert [c["name"] for c in before] == [c["name"] for c in after]
    assert ("fingerprint",) in _unique_constraint_columns(engine, "alerts")
    engine.dispose()


def test_clean_chain_leaves_no_startup_schema_gaps(throwaway_db):
    """``alembic upgrade head`` 到乾淨庫之後,啟動檢查必須直接通過。

    這就是 DR 還原路徑:還原 + upgrade head,不需要「先啟動一次 app 讓它自癒」。
    """
    _run_alembic(throwaway_db, "upgrade", "head")
    engine = create_engine(throwaway_db)
    assert collect_schema_gaps(engine) == []
    engine.dispose()


def test_verify_schema_refuses_a_database_that_is_behind(throwaway_db):
    """⑤ schema 落後 → 拒啟動,且訊息指向 alembic。"""
    _run_alembic(throwaway_db, "upgrade", "head")
    engine = create_engine(throwaway_db)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE alerts DROP CONSTRAINT uq_alerts_fingerprint")
        )
        connection.execute(text("ALTER TABLE alerts DROP COLUMN metadata_json"))

    with pytest.raises(SchemaOutOfDateError) as excinfo:
        verify_schema(engine)

    message = str(excinfo.value)
    assert "alembic upgrade head" in message
    assert "alerts.metadata_json" in message
    assert "不再" in message  # 明說不再自癒
    engine.dispose()


def test_concurrent_same_fingerprint_upserts_leave_exactly_one_row(throwaway_db):
    """③ 併發兩路同 fingerprint 告警 → 庫內 1 列。

    兩個獨立 session 各自開交易、都在對方 commit 前送出 upsert:第二路會被
    ``uq_alerts_fingerprint`` 擋在鎖上,等第一路 commit 後轉成 UPDATE。
    舊的 read-then-write 實作在這裡會兩路都 INSERT → 第二路 IntegrityError
    (或在沒有 UNIQUE 的舊庫上留下兩列)。
    """
    _run_alembic(throwaway_db, "upgrade", "head")
    engine = create_engine(throwaway_db)
    Session = sessionmaker(bind=engine)

    first_statement_sent = threading.Event()
    second_thread_started = threading.Event()
    errors: list[BaseException] = []

    def second_writer():
        session = Session()
        second_thread_started.set()
        try:
            upsert_alert(
                session,
                fingerprint="health:model:42",
                category="health",
                severity="critical",
                title="第二路",
                message="second",
            )
            session.commit()
        except BaseException as exc:  # noqa: BLE001 —— 要把它帶回主執行緒斷言
            session.rollback()
            errors.append(exc)
        finally:
            session.close()

    session_a = Session()
    try:
        upsert_alert(
            session_a,
            fingerprint="health:model:42",
            category="health",
            severity="high",
            title="第一路",
            message="first",
        )
        first_statement_sent.set()

        thread = threading.Thread(target=second_writer, daemon=True)
        thread.start()
        second_thread_started.wait(timeout=10)
        # 讓第二路確實撞上第一路未提交的列(它會在唯一索引上等鎖)
        thread.join(timeout=1.0)
        assert thread.is_alive(), (
            "第二路沒有被唯一約束序列化 —— 表示 alerts.fingerprint 沒有 UNIQUE"
        )

        session_a.commit()
        thread.join(timeout=20)
        assert not thread.is_alive()
    finally:
        session_a.close()

    assert errors == [], f"第二路不該失敗,而是應該轉成 UPDATE: {errors!r}"

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT title, severity FROM alerts"
                " WHERE fingerprint = 'health:model:42'"
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].title == "第二路"  # 後到的那路以 UPDATE 覆蓋
    engine.dispose()
