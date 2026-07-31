"""P2.7 稽核帳 — 需要真 PostgreSQL 的證據。

這裡驗的是「資料庫真的拒絕」與「竄改真的被查出來」,不是「程式碼看起來有做」。
SQLite 沒有 role、沒有 owner、沒有 trigger 語意,那邊驗出來的綠燈是假的。

跑法:``ANILA_TEST_PG_DSN=postgresql://<superuser>:<pw>@host:port/postgres``
(或把 DSN 寫進 ``/tmp/anila-test-pg-dsn``)。沒設就整檔 skip,與其他 *_pg.py 一致。

``SET ROLE csp_app`` 是拿掉 superuser 特權的標準做法 —— 切到非 superuser 角色
之後,權限檢查與 ownership 檢查都會真的生效,所以不需要知道 csp_app 的密碼。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

_DSN = os.environ.get("ANILA_TEST_PG_DSN")
if not _DSN and Path("/tmp/anila-test-pg-dsn").exists():
    _DSN = Path("/tmp/anila-test-pg-dsn").read_text().strip()

pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN not set — needs PostgreSQL + CREATE DATABASE",
)

_CSP_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_INI = _CSP_ROOT / "alembic.ini"

_PROTECTED = (
    "audit_logs", "policy_decisions", "classification_events",
    "audit_checkpoints",
)


def _scratch_url(admin_dsn: str, dbname: str) -> str:
    parts = urlparse(admin_dsn)
    return urlunparse(parts._replace(path=f"/{dbname}"))


def _dbname_of(dsn: str) -> str:
    return (urlparse(dsn).path or "").lstrip("/") or "postgres"


@pytest.fixture(scope="module")
def migrated_pg():
    assert _DSN
    scratch = f"p27_ledger_{uuid.uuid4().hex[:10]}"
    assert scratch not in (_dbname_of(_DSN), "csp"), "refusing to touch a live DB"

    admin = psycopg2.connect(_DSN)
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    admin.cursor().execute(f'CREATE DATABASE "{scratch}"')
    admin.close()

    scratch_dsn = _scratch_url(_DSN, scratch)
    saved = {
        k: os.environ.get(k) for k in ("MIGRATION_DATABASE_URL", "DATABASE_URL")
    }
    os.environ["MIGRATION_DATABASE_URL"] = scratch_dsn
    os.environ["DATABASE_URL"] = scratch_dsn
    old_cwd = os.getcwd()
    try:
        from alembic import command
        from alembic.config import Config

        os.chdir(_CSP_ROOT)
        command.upgrade(Config(str(_ALEMBIC_INI)), "head")
        os.chdir(old_cwd)
        yield scratch_dsn
    finally:
        os.chdir(old_cwd)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        admin = psycopg2.connect(_DSN)
        admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        cur = admin.cursor()
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (scratch,)
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.close()


@pytest.fixture
def conn(migrated_pg):
    """migration-role(owner / superuser)連線。"""
    c = psycopg2.connect(migrated_pg)
    c.autocommit = True
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def clean_ledger(conn):
    """把帳本清空 —— 只有拿到主機的人做得到,所以測試自己也得先關觸發器。"""
    cur = conn.cursor()
    for table in _PROTECTED:
        cur.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        cur.execute(f"DELETE FROM {table}")
        cur.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")
    yield conn


def _as_app(conn):
    """把連線身分降為 runtime role ``csp_app``(superuser 特權失效)。

    ⚠ ``SET ROLE`` 是 **session** 級的,不是 cursor 級 —— 同一條連線之後的
    所有操作都會是 csp_app。要切回去請用 ``_as_owner``。
    """
    cur = conn.cursor()
    cur.execute("SET ROLE csp_app")
    return cur


def _as_owner(conn):
    """把連線身分切回 migration role(owner / superuser)。"""
    cur = conn.cursor()
    cur.execute("RESET ROLE")
    return cur


def _role_engine(dsn: str, role: str):
    """一支身分是 ``role`` 的 engine —— 模擬 runtime 連線。

    ⚠ 不能用 ``connect`` event 發 ``SET ROLE``:那道 SET 跑在 psycopg2 的
    隱含交易裡,連線還池時 SQLAlchemy 的 ROLLBACK 會把它一起回捲,第二次
    借出來就變回 superuser —— 測試會安靜地變成「用 superuser 驗權限」的假綠燈
    (本包實作時真的踩到)。走連線參數設 ``role`` GUC 才是 session 預設值,
    ROLLBACK 只會回到它。
    """
    return create_engine(dsn, connect_args={"options": f"-c role={role}"})


def _insert_audit(cur, *, detail: str, days_ago: int = 0, actor: int = 7) -> int:
    cur.execute(
        "INSERT INTO audit_logs (actor_user_id, actor_username, action, "
        " resource_type, resource_id, status, detail, created_at) "
        "VALUES (%s, 'kung', 'read', 'document', 'doc-1', 'success', %s, "
        "        now() - make_interval(days => %s)) RETURNING id",
        (actor, detail, days_ago),
    )
    return cur.fetchone()[0]


def _session(dsn):
    return sessionmaker(bind=create_engine(dsn))()


# ── 第一層:權限分離 ────────────────────────────────────────────────────────

def test_runtime_role_is_not_the_owner(conn):
    """owner 隱含全部權限,而且拆得掉觸發器 —— 所以 owner 不能是 runtime role。"""
    cur = conn.cursor()
    for table in _PROTECTED:
        cur.execute(
            "SELECT pg_get_userbyid(c.relowner) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=%s", (table,)
        )
        assert cur.fetchone()[0] != "csp_app", f"{table} 的 owner 還是 runtime role"


def test_runtime_role_has_only_the_privileges_it_needs(conn):
    """事件表:SELECT+INSERT。檢查點表:**只有 SELECT**。

    檢查點表連 INSERT 都不給,理由見
    ``test_runtime_role_cannot_poison_the_chain_with_a_future_checkpoint``。
    """
    cur = conn.cursor()
    for table in _PROTECTED:
        expected_insert = table != "audit_checkpoints"
        for priv, expected in (
            ("SELECT", True), ("INSERT", expected_insert),
            ("UPDATE", False), ("DELETE", False), ("TRUNCATE", False),
        ):
            cur.execute(
                "SELECT has_table_privilege('csp_app', %s, %s)", (table, priv)
            )
            assert cur.fetchone()[0] is expected, f"{table}.{priv}"


def test_runtime_role_can_still_write_and_read_audit_rows(conn):
    """平台必須照常寫稽核 —— 收權不能把正常路徑一起關掉。"""
    cur = _as_app(conn)
    row_id = _insert_audit(cur, detail="normal write")
    cur.execute("SELECT detail FROM audit_logs WHERE id = %s", (row_id,))
    assert cur.fetchone()[0] == "normal write"


@pytest.mark.parametrize("table", _PROTECTED)
@pytest.mark.parametrize("verb", ["UPDATE {t} SET created_at = now()",
                                  "DELETE FROM {t}", "TRUNCATE {t}"])
def test_runtime_role_cannot_update_or_delete(conn, table, verb):
    cur = _as_app(conn)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute(verb.format(t=table))


def test_runtime_role_cannot_drop_the_append_only_trigger(conn):
    """光有觸發器不夠 —— 能拆掉它的人等於沒有觸發器。"""
    cur = _as_app(conn)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc:
        cur.execute("DROP TRIGGER audit_logs_no_update ON audit_logs")
    assert "must be owner" in str(exc.value)


def test_trigger_blocks_even_the_table_owner(conn):
    """皮帶加吊帶:就算有人把 UPDATE 權限發回去,觸發器仍然擋。"""
    cur = conn.cursor()  # migration role = owner
    _insert_audit(cur, detail="owner probe")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc:
        cur.execute("UPDATE audit_logs SET detail = 'rewritten'")
    assert "append-only" in str(exc.value)


def test_delete_inside_retention_is_refused_expired_is_allowed(conn):
    """保留期(半年)內不准刪;過期的購毀是唯一合法刪除。"""
    cur = conn.cursor()
    fresh = _insert_audit(cur, detail="fresh", days_ago=1)
    old = _insert_audit(cur, detail="expired", days_ago=400)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        cur.execute("DELETE FROM audit_logs WHERE id = %s", (fresh,))
    cur.execute("DELETE FROM audit_logs WHERE id = %s", (old,))
    cur.execute("SELECT COUNT(*) FROM audit_logs WHERE id = %s", (old,))
    assert cur.fetchone()[0] == 0


def test_no_foreign_keys_left_on_audit_tables(conn):
    """FK 的 ON DELETE SET NULL 由 DB 自己發 UPDATE,會撞上 append-only 觸發器
    (刪使用者/task 就整個炸掉),而且等於「刪帳號洗掉稽核歸屬」。"""
    cur = conn.cursor()
    cur.execute(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint "
        "WHERE contype='f' AND conrelid = ANY(%s::regclass[])",
        (list(_PROTECTED),),
    )
    assert cur.fetchall() == []


def test_protected_set_matches_the_database(conn):
    """runtime 常數與資料庫實況不得漂移。"""
    from app.services.audit_ledger import AUDIT_LEDGER_TABLES

    cur = conn.cursor()
    cur.execute(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_trigger t ON t.tgrelid = c.oid "
        "WHERE n.nspname='public' AND NOT t.tgisinternal "
        "  AND t.tgname LIKE '%%\\_no\\_update' GROUP BY 1"
    )
    assert {r[0] for r in cur.fetchall()} == set(AUDIT_LEDGER_TABLES)


# ── 第二層:日級雜湊鏈 ──────────────────────────────────────────────────────

def test_chain_detects_a_tampered_row_and_names_the_day(migrated_pg, clean_ledger):
    """核心驗收:改一列 → 驗證器抓到,而且說得出是哪一天。"""
    from app.services import audit_ledger

    cur = _as_app(clean_ledger)
    for days_ago in (3, 2, 1):
        for i in range(3):
            _insert_audit(cur, detail=f"day-{days_ago} row-{i}", days_ago=days_ago)

    db = _session(migrated_pg)
    try:
        assert len(audit_ledger.seal_due_checkpoints(db)) == 3
        db.rollback()
        clean = audit_ledger.verify_chain(db)
        assert clean.ok, audit_ledger.format_verification(clean)
        exported_head = clean.chain_head

        # 被害日 = 前天。拿主機的人會先關掉觸發器 —— 觸發器擋得住 csp_app,
        # 擋不住 superuser,這裡模擬的就是後者。
        victim_day = datetime.now(timezone.utc).date() - timedelta(days=2)
        owner = _as_owner(clean_ledger)
        owner.execute("ALTER TABLE audit_logs DISABLE TRIGGER USER")
        owner.execute(
            "UPDATE audit_logs SET detail='(nothing to see here)' "
            "WHERE detail LIKE 'day-2 %'"
        )
        owner.execute("ALTER TABLE audit_logs ENABLE TRIGGER USER")

        db.rollback()
        after = audit_ledger.verify_chain(db, expected_head=exported_head)
        report = audit_ledger.format_verification(after)
        assert not after.ok, report
        assert after.tampered_days == [victim_day], report
        assert str(victim_day) in report
    finally:
        db.close()


def test_recomputing_the_chain_still_fails_against_an_exported_head(
    migrated_pg, clean_ledger,
):
    """會重算鏈的內部人 —— 這是「錨定在資料庫之外」唯一存在的理由。

    改了列之後把檢查點一起重寫,本機看起來完全自洽;但已經交出去的那份
    匯出檔上印的鏈頭對不上,所以還是露餡。
    """
    from app.services import audit_ledger

    cur = _as_app(clean_ledger)
    for i in range(2):
        _insert_audit(cur, detail=f"anchored-{i}", days_ago=5)

    db = _session(migrated_pg)
    try:
        audit_ledger.seal_due_checkpoints(db)
        db.rollback()
        exported_head = audit_ledger.verify_chain(db).chain_head

        owner = _as_owner(clean_ledger)
        for table in ("audit_logs", "audit_checkpoints"):
            owner.execute(f"ALTER TABLE {table} DISABLE TRIGGER USER")
        owner.execute(
            "UPDATE audit_logs SET detail='(scrubbed)' WHERE detail LIKE 'anchored-%'"
        )
        db.rollback()
        rows = db.execute(
            text("SELECT id, day FROM audit_checkpoints ORDER BY day")
        ).all()
        prev = audit_ledger.GENESIS_HASH
        for row in rows:
            digest, _counts = audit_ledger.compute_day_digest(db, row.day)
            head = audit_ledger.link(prev, row.day, digest)
            owner.execute(
                "UPDATE audit_checkpoints SET day_digest=%s, prev_hash=%s, "
                "chain_head=%s WHERE id=%s", (digest, prev, head, row.id),
            )
            prev = head
        for table in ("audit_logs", "audit_checkpoints"):
            owner.execute(f"ALTER TABLE {table} ENABLE TRIGGER USER")

        db.rollback()
        local_only = audit_ledger.verify_chain(db)
        assert local_only.ok, (
            "重算過的鏈本機自洽 —— 這正是為什麼證據必須離開這台機器:"
            + audit_ledger.format_verification(local_only)
        )
        against_report = audit_ledger.verify_chain(db, expected_head=exported_head)
        assert not against_report.ok
        assert against_report.head_matches is False
    finally:
        db.close()


def test_deleting_a_checkpoint_shows_up_as_a_gap(migrated_pg, clean_ledger):
    from app.services import audit_ledger

    cur = _as_app(clean_ledger)
    for days_ago in (9, 8, 7):
        _insert_audit(cur, detail=f"gap-{days_ago}", days_ago=days_ago)

    db = _session(migrated_pg)
    try:
        audit_ledger.seal_due_checkpoints(db)
        owner = _as_owner(clean_ledger)
        owner.execute("ALTER TABLE audit_checkpoints DISABLE TRIGGER USER")
        owner.execute(
            "DELETE FROM audit_checkpoints WHERE day = %s",
            (datetime.now(timezone.utc).date() - timedelta(days=8),),
        )
        owner.execute("ALTER TABLE audit_checkpoints ENABLE TRIGGER USER")
        db.rollback()
        result = audit_ledger.verify_chain(db)
        assert not result.ok
        assert any("不連續" in p for p in result.problems), result.problems
    finally:
        db.close()


def test_backdated_insert_into_a_sealed_day_is_caught(migrated_pg, clean_ledger):
    """事後補一筆假紀錄進已封存的日子,也要被抓到。"""
    from app.services import audit_ledger

    cur = _as_app(clean_ledger)
    _insert_audit(cur, detail="genuine", days_ago=2)
    db = _session(migrated_pg)
    try:
        audit_ledger.seal_due_checkpoints(db)
        db.rollback()
        assert audit_ledger.verify_chain(db).ok
        # 補插只需要 INSERT 權限 —— csp_app 就做得到（見設計文件對「捏造」的誠實承認）
        _insert_audit(cur, detail="fabricated alibi", days_ago=2)
        db.rollback()
        result = audit_ledger.verify_chain(db)
        assert not result.ok
        assert result.tampered_days == [
            datetime.now(timezone.utc).date() - timedelta(days=2)
        ]
    finally:
        db.close()


def test_sealing_is_idempotent_and_never_seals_today(migrated_pg, clean_ledger):
    from app.services import audit_ledger

    cur = _as_app(clean_ledger)
    _insert_audit(cur, detail="yesterday", days_ago=1)
    _insert_audit(cur, detail="today", days_ago=0)
    db = _session(migrated_pg)
    try:
        audit_ledger.seal_due_checkpoints(db)
        db.rollback()
        assert audit_ledger.seal_due_checkpoints(db) == [], "重複封存製造重複檢查點"
        db.rollback()
        result = audit_ledger.verify_chain(db)
        assert result.ok, audit_ledger.format_verification(result)
        assert result.last_day < datetime.now(timezone.utc).date(), (
            "把今天也封起來,當日後續的正常寫入會立刻被誤判成竄改"
        )
        assert result.unanchored_rows.get("audit_logs") == 1
    finally:
        db.close()


# ── 檢查點下毒:讓「抓弊的東西自己安靜地死掉」 ─────────────────────────────

def test_runtime_role_cannot_poison_the_chain_with_a_future_checkpoint(
    migrated_pg, clean_ledger,
):
    """拿到 runtime 憑證的人不得往 ``audit_checkpoints`` 塞任何東西。

    為什麼這件事致命:摘要裡沒有祕密,有 SELECT 就算得出自洽的鏈頭。若
    ``csp_app`` 有 INSERT,他可以塞一列**日期在未來**的檢查點;封存游標會跳到
    今天之後,``while cursor < today`` 永遠為假 —— 每日封存從此停擺,而且
    ``if sealed:`` 不成立所以**連一行日誌都不會寫**,匯出檔還是照印那個再也
    不動的鏈頭。負責抓弊的機制自己死了,沒有人會知道。

    所以這裡不是「偵測」而是「拿掉能力」:r1_0027 對檢查點表只給 SELECT。
    """
    app_cur = _as_app(clean_ledger)
    future = datetime.now(timezone.utc).date() + timedelta(days=3650)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege) as exc:
        app_cur.execute(
            "INSERT INTO audit_checkpoints (day, digest_version, day_digest, "
            " prev_hash, chain_head, row_counts, created_at) "
            "VALUES (%s, 1, %s, %s, %s, '{}', now())",
            (future, "0" * 64, "0" * 64, "0" * 64),
        )
    assert "audit_checkpoints" in str(exc.value)


def test_future_dated_checkpoint_does_not_stall_sealing(migrated_pg, clean_ledger):
    """就算未來日期的檢查點真的出現(只有 superuser 寫得進去),封存也不能停。

    深度防禦:權限已經擋掉一般路徑,但「游標無條件相信 MAX(day)」這個寫法
    本身是脆的。這條測試釘住的是**行為**:帳本裡有一列 2036 年的檢查點時,
    昨天照樣封得起來,而且驗證會把那列指名出來。
    """
    from app.services import audit_ledger

    app_cur = _as_app(clean_ledger)
    _insert_audit(app_cur, detail="genuine yesterday", days_ago=1)

    owner = _as_owner(clean_ledger)
    future = datetime.now(timezone.utc).date() + timedelta(days=3650)
    owner.execute(
        "INSERT INTO audit_checkpoints (day, digest_version, day_digest, "
        " prev_hash, chain_head, row_counts, created_at) "
        "VALUES (%s, 1, %s, %s, %s, '{}', now())",
        (future, "0" * 64, "0" * 64, "0" * 64),
    )

    db = _session(migrated_pg)
    try:
        sealed = audit_ledger.seal_due_checkpoints(db)
        assert sealed, "未來日期的檢查點把每日封存卡死了 —— 抓弊機制安靜地死了"
        db.rollback()
        result = audit_ledger.verify_chain(db)
        assert future in result.future_days
        assert not result.ok
        assert "未來" in audit_ledger.format_verification(result)
    finally:
        db.close()


# ── 還原之後的擁有權(P3.1 × P2.7) ─────────────────────────────────────────

_OWNERSHIP_SQL = (
    Path(__file__).resolve().parents[3]
    / "infra" / "deployment" / "scripts" / "assert-db-ownership.sql"
)


def test_restore_ownership_sql_is_a_noop_on_a_migrated_database(migrated_pg, conn):
    """還原腳本的擁有權校正,在正常資料庫上必須什麼都不改。

    這條把兩份「該長什麼樣」綁在一起:``r1_0027`` 產生的狀態,與
    ``assert-db-ownership.sql`` 還原後扳回的狀態。任何一邊改了、另一邊沒改,
    這裡就會紅 —— 否則下一次真的還原時才會發現,而那是凌晨兩點。
    """
    assert _OWNERSHIP_SQL.is_file(), _OWNERSHIP_SQL

    def fingerprint():
        cur = conn.cursor()
        cur.execute(
            "SELECT c.relname, pg_get_userbyid(c.relowner), "
            "       has_table_privilege('csp_app', c.oid, 'SELECT'), "
            "       has_table_privilege('csp_app', c.oid, 'INSERT'), "
            "       has_table_privilege('csp_app', c.oid, 'UPDATE'), "
            "       has_table_privilege('csp_app', c.oid, 'DELETE') "
            "  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname='public' AND c.relkind='r' ORDER BY 1"
        )
        return cur.fetchall()

    before = fingerprint()
    assert before, "沒有任何表 — fixture 壞了"
    _as_owner(conn).execute(_OWNERSHIP_SQL.read_text())
    assert fingerprint() == before, "還原用的擁有權校正在正常資料庫上改了東西"


def test_restore_ownership_sql_repairs_a_flattened_ownership_map(
    migrated_pg, conn,
):
    """模擬 ``pg_restore --no-owner``:擁有權被壓平成同一個 role。

    兩件事都要修回來 ——
    (1) 開機會發 DDL 的表要回到 csp_app,否則平台開機噴 must be owner、
        例外被吞掉、``/health`` 還是 200(=容器全綠但使用者進不來);
    (2) 稽核表**不可以**回到 csp_app,否則 P2.7 在第一次還原就蒸發。
    """
    owner = _as_owner(conn)
    # 兩個方向都要模擬:
    #   * users / token_usage → csp   ：`pg_restore --no-owner` 真正造成的傷害,
    #     開機 DDL 會 `must be owner`,而例外被吞掉 → 容器全綠但平台是壞的。
    #   * audit_logs / audit_checkpoints → csp_app ：對 P2.7 最危險的方向。
    owner.execute(
        """
        DO $$ DECLARE r RECORD; BEGIN
          FOR r IN SELECT c.relname FROM pg_class c
                     JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relkind='r'
                      AND c.relname IN ('users','token_usage')
          LOOP
            EXECUTE format('ALTER TABLE public.%I OWNER TO csp', r.relname);
          END LOOP;
          FOR r IN SELECT c.relname FROM pg_class c
                     JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relkind='r'
                      AND c.relname IN ('audit_logs','audit_checkpoints')
          LOOP
            EXECUTE format('ALTER TABLE public.%I OWNER TO csp_app', r.relname);
            EXECUTE format('GRANT ALL ON TABLE public.%I TO csp_app', r.relname);
          END LOOP;
        END $$
        """
    )
    owner.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
        "ON n.oid=c.relnamespace WHERE n.nspname='public' "
        "AND ((c.relname IN ('audit_logs','audit_checkpoints') "
        "      AND pg_get_userbyid(c.relowner)='csp_app') "
        "  OR (c.relname IN ('users','token_usage') "
        "      AND pg_get_userbyid(c.relowner)<>'csp_app'))"
    )
    assert owner.fetchone()[0] == 4, "前提沒成立:壓平沒生效"

    _as_owner(conn).execute(_OWNERSHIP_SQL.read_text())

    cur = conn.cursor()
    cur.execute(
        "SELECT c.relname, pg_get_userbyid(c.relowner), "
        "       has_table_privilege('csp_app', c.oid, 'UPDATE'), "
        "       has_table_privilege('csp_app', c.oid, 'INSERT') "
        "  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        " WHERE n.nspname='public' AND c.relname IN "
        "       ('users','token_usage','audit_logs','audit_checkpoints') ORDER BY 1"
    )
    got = {r[0]: r[1:] for r in cur.fetchall()}
    assert got["users"][0] == "csp_app", "開機 DDL 用的表沒有被扳回 csp_app"
    assert got["token_usage"][0] == "csp_app"
    for table in ("audit_logs", "audit_checkpoints"):
        assert got[table][0] != "csp_app", f"{table} 還歸 runtime role — P2.7 蒸發"
        assert got[table][1] is False, f"{table} 的 csp_app 仍可 UPDATE"
    assert got["audit_logs"][2] is True, "稽核事件表要保留 INSERT,平台得寫得進去"
    assert got["audit_checkpoints"][2] is False, "檢查點表不可以有 INSERT"


# ── 開機路徑:第二支 engine ─────────────────────────────────────────────────

def test_boot_backfill_survives_the_audit_tables_changing_owner(migrated_pg, conn):
    """整段開機回填必須在收權之後照樣跑得完。

    這是「平台還開得起來」那條驗收:``_ensure_schema_backfills`` 走的是
    runtime engine(``csp_app``),而 audit_logs 已經不歸它所有 —— 只有
    ``_migration_bind`` 那一小段換成 migration 身分,才不會在
    ``ALTER TABLE audit_logs`` 上拿到 ``must be owner of table audit_logs``。
    把那個 ``with _migration_bind(bind) as audit_bind`` 改回 ``bind``,
    這條就會紅。
    """
    from app.services.startup_migrations import _ensure_schema_backfills

    owner = conn.cursor()
    owner.execute("ALTER TABLE audit_logs DROP COLUMN IF EXISTS metadata_json")

    runtime = _role_engine(migrated_pg, "csp_app")
    saved = os.environ.get("MIGRATION_DATABASE_URL")
    os.environ["MIGRATION_DATABASE_URL"] = migrated_pg
    try:
        with runtime.connect() as c:
            assert c.execute(text("SELECT current_user")).scalar() == "csp_app"
        # 例外不被吞掉:run_startup_migrations 會 log-and-continue,
        # 那正是「開機沒炸但 schema 沒補上」的假綠燈,所以這裡直接呼叫內層。
        _ensure_schema_backfills(runtime)
    finally:
        runtime.dispose()
        if saved is None:
            os.environ.pop("MIGRATION_DATABASE_URL", None)
        else:
            os.environ["MIGRATION_DATABASE_URL"] = saved

    owner.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='audit_logs' AND column_name='metadata_json'"
    )
    assert owner.fetchone() is not None, "開機補欄位沒生效"


def test_startup_guard_refuses_a_loosened_audit_table(migrated_pg, conn, monkeypatch):
    """未來哪支 migration 忘了收權,csp **當天**就起不來,而不是半年後在稽核現場發現。"""
    import app.services.startup_security as ss

    engine = _role_engine(migrated_pg, "csp_app")
    monkeypatch.setattr("app.database.engine", engine, raising=False)
    monkeypatch.setattr(ss, "_is_dev_mode", lambda: False)
    owner = conn.cursor()
    try:
        ss.assert_audit_ledger_locked_down()  # 正常姿態:過

        owner.execute("GRANT UPDATE ON audit_logs TO csp_app")
        with pytest.raises(RuntimeError) as exc:
            ss.assert_audit_ledger_locked_down()
        assert "audit_logs" in str(exc.value)
        assert "UPDATE" in str(exc.value)
    finally:
        owner.execute("REVOKE UPDATE ON audit_logs FROM csp_app")
        engine.dispose()


def test_startup_guard_refuses_insert_on_the_checkpoint_table(
    migrated_pg, conn, monkeypatch,
):
    """把 INSERT 發回給檢查點表 = 重新打開「封存被卡死」那條路,開機就該擋。"""
    import app.services.startup_security as ss

    engine = _role_engine(migrated_pg, "csp_app")
    monkeypatch.setattr("app.database.engine", engine, raising=False)
    monkeypatch.setattr(ss, "_is_dev_mode", lambda: False)
    owner = conn.cursor()
    try:
        ss.assert_audit_ledger_locked_down()
        owner.execute("GRANT INSERT ON audit_checkpoints TO csp_app")
        with pytest.raises(RuntimeError) as exc:
            ss.assert_audit_ledger_locked_down()
        assert "audit_checkpoints" in str(exc.value)
        assert "INSERT" in str(exc.value)
    finally:
        owner.execute("REVOKE INSERT ON audit_checkpoints FROM csp_app")
        engine.dispose()
