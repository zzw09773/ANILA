# -*- coding: utf-8 -*-
"""真 PostgreSQL 證據:治理帳 timestamp → timestamptz(W2-10 批次 1 / r1_0040)。

為什麼一定要真 PG
-----------------
本包要釘死的是 ``ALTER COLUMN ... TYPE timestamptz USING <col> AT TIME ZONE
'UTC'`` 的**資料語意**,而那在 SQLite 上結構上測不到:SQLite 沒有
``timestamptz``、沒有 ``AT TIME ZONE``、也沒有 session ``TimeZone`` GUC
(``DateTime(timezone=True)`` 在 SQLite dialect 上等同 naive)。

這裡最重要的一條是 ``test_naive_values_are_reinterpreted_as_utc_wall_clock``:
它把「既有 naive 值按 UTC 判讀、絕對時點零平移」**釘成明示行為**,並同時斷言
「不是台北判讀(那會早 8 小時)」—— 只斷言前者的話,測試無法證明自己真的在測
判讀方向。判讀決策軌跡(07-26 拍板台北、07-27 改判 UTC)在
``migrations/versions/r1_0040_governance_ledger_timestamptz.py`` 檔頭。有這條
測試,將來有人改動判讀時區時,會先撞到「這是深思後的決定」而不是默默漂移。

執行方式(需要一個**獨立、可丟棄**的 PostgreSQL,superuser DSN)::

    docker run -d --rm --name throwaway-pg -e POSTGRES_PASSWORD=x \\
        -e POSTGRES_DB=csp -p 55432:5432 pgvector/pgvector:pg16
    ANILA_TEST_PG_DSN=postgresql://postgres:x@127.0.0.1:55432/csp \\
        python -m pytest tests/test_w210_governance_timestamptz_pg.py

⚠ 每個測試自建/自刪**自己的資料庫**(名字帶 uuid),不碰 DSN 指到的那個庫的
內容。仍然請只對可丟棄的實例跑 —— 它會 ``CREATE DATABASE`` / ``DROP DATABASE``。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_DSN = os.environ.get("ANILA_TEST_PG_DSN") or os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ANILA_TEST_PG_DSN / TEST_POSTGRES_URL not set —— 需要可丟棄的真 PostgreSQL",
)

CSP_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = CSP_ROOT / "migrations"

_REVISION = "r1_0040"

# 判讀時區 —— 與 migration 的 ``_INTERPRETATION_TZ`` 必須一致。刻意在測試裡再寫
# 一次而不 import:如果哪天有人改了 migration 的常數,這裡要紅,而不是跟著改。
# 2026-07-27 user 拍板改為 UTC(決策軌跡見 migration 檔頭 ①)。
_INTERPRETATION_TZ = "UTC"
# 台北判讀與 UTC 判讀的差 —— 留著給「不是台北判讀」的反向斷言用。
_TAIPEI_OFFSET = timedelta(hours=8)

# 本批 (A) 組:乾淨鏈上是 naive、由 r1_0040 轉型的 9 欄。
_CONVERTED = (
    ("api_keys", "expires_at"),
    ("classification_authority_assignments", "created_at"),
    ("classification_authority_assignments", "revoked_at"),
    ("classification_events", "created_at"),
    ("declassification_requests", "created_at"),
    ("declassification_requests", "decided_at"),
    ("export_records", "classification_latched_at"),
    ("export_records", "created_at"),
    ("policy_decisions", "created_at"),
)

# 本批 (B) 組:乾淨鏈上**已經**是 timestamptz,只有 ORM 宣告要修。
# r1_0040 不動它們的資料 → 值不該有任何改動。
_ALREADY_TZ = (
    ("api_keys", "created_at"),
    ("api_keys", "last_used_at"),
    ("audit_logs", "created_at"),
)

# 種進去的已知 naive 值。刻意選 09:30(台北早上)—— 換成 UTC 判讀會落在同一天,
# 換成台北判讀會落到**前一天** 01:30 UTC,兩種判讀的差別因此一眼可見。
_SEED_NAIVE = datetime(2026, 3, 1, 9, 30, 0)
# 「把 _SEED_NAIVE 當 UTC 牆鐘」的絕對時點 —— 零平移。
_EXPECTED_AWARE = _SEED_NAIVE.replace(tzinfo=timezone.utc)


def _with_database(name: str) -> str:
    # ``str(URL)`` 會把密碼遮成 ``***``,直接拿去連線一定得到 auth failed。
    return make_url(_DSN).set(database=name).render_as_string(hide_password=False)


def _alembic_config(url: str) -> Config:
    # 刻意用「沒有 config file」的 Config:``env.py`` 只有在 config_file_name
    # 非 None 時才呼叫 ``fileConfig()``,而 alembic.ini 的 [loggers] 會把 pytest
    # 的 logging handler 全部 disable。
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


def _predecessor(url: str) -> str:
    """r1_0040 的前一個 revision —— 動態讀,不寫死。

    ``down_revision`` 目前是 ``r1_0038``,但併版時會被整合者改成 ``r1_0039``
    (並行工作)。寫死會讓這支測試在合併當下變紅,而紅的原因跟它要驗的東西無關。
    """
    rev = ScriptDirectory.from_config(_alembic_config(url)).get_revision(_REVISION)
    down = rev.down_revision
    assert isinstance(down, str), f"{_REVISION} 應該只有一個 down_revision,實得 {down!r}"
    return down


@pytest.fixture()
def throwaway_db():
    name = f"w210_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_with_database("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield _with_database(name)
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _column_types(engine, tables: set[str]) -> dict[str, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_name, column_name, data_type"
                " FROM information_schema.columns"
                " WHERE table_schema = current_schema()"
                "   AND table_name = ANY(:tables)"
                "   AND data_type LIKE 'timestamp%'"
            ),
            {"tables": sorted(tables)},
        ).all()
    return {f"{r[0]}.{r[1]}": r[2] for r in rows}


def _seed_naive_governance_rows(engine) -> None:
    """在轉型**之前**種下已知 naive 值(治理帳五張表 + api_keys)。

    刻意用原生 SQL 而不是 ORM:ORM 這一側已經宣告 ``timezone=True``,用它寫入
    會讓「既有 naive 值」這個前提不成立 —— 要重建的是**歷史事實**(轉型前的庫
    裡躺著什麼),不是現行程式碼的行為。
    """
    naive = _SEED_NAIVE.isoformat(sep=" ")
    aware = _EXPECTED_AWARE  # (B) 組用 aware 值寫 timestamptz 欄
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (username, hashed_password, role, is_active)"
                " VALUES ('w210-admin', 'x', 'admin', TRUE)"
            )
        )
        admin_id = connection.execute(
            text("SELECT id FROM users WHERE username = 'w210-admin'")
        ).scalar_one()

        connection.execute(
            text(
                "INSERT INTO classification_events"
                " (resource_type, resource_id, previous_level, new_level, reason,"
                "  created_at)"
                f" VALUES ('conversation', '1', '無機密', '營業秘密', 'manual_admin',"
                f" TIMESTAMP '{naive}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO policy_decisions"
                " (actor_type, action, resource_type, decision, created_at)"
                f" VALUES ('user', 'artifact.export', 'artifact', 'deny',"
                f" TIMESTAMP '{naive}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO declassification_requests"
                " (resource_type, resource_id, from_level, to_level,"
                "  requested_by_admin_id, reason, created_at, decided_at)"
                f" VALUES ('conversation', '1', '營業秘密', '無機密', {admin_id},"
                f" '紙本核准代錄', TIMESTAMP '{naive}', TIMESTAMP '{naive}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO classification_authority_assignments"
                " (user_id, authority_reference, created_at, revoked_at)"
                f" VALUES ({admin_id}, '院授字第 1150001 號',"
                f" TIMESTAMP '{naive}', TIMESTAMP '{naive}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO export_records"
                " (exporter_user_id, created_at, classification_latched_at)"
                f" VALUES ({admin_id}, TIMESTAMP '{naive}', TIMESTAMP '{naive}')"
            )
        )
        # api_keys:expires_at 是 (A) 組(naive),created_at / last_used_at 是
        # (B) 組(乾淨鏈上已是 timestamptz)→ 用 aware 值寫,轉型後不該平移。
        connection.execute(
            text(
                "INSERT INTO api_keys"
                " (user_id, name, key_prefix, key_suffix, key_hash, is_active,"
                "  expires_at, created_at, last_used_at)"
                f" VALUES ({admin_id}, 'w210', 'sk-w210', 'abcd', 'hash-w210', TRUE,"
                f" TIMESTAMP '{naive}', :aware, :aware)"
            ),
            {"aware": aware},
        )
        connection.execute(
            text(
                "INSERT INTO audit_logs (action, resource_type, status, created_at)"
                " VALUES ('w210.seed', 'test', 'success', :aware)"
            ),
            {"aware": aware},
        )


def _read_converted_values(engine, *, session_tz: str) -> dict[str, datetime]:
    """在指定 session TimeZone 下讀回本批欄位的值。

    session TZ 是刻意的參數:timestamptz 讀回來的**絕對時點**必須與 session TZ
    無關(C1 §g ②「跨 TZ 環境變數重跑一次,值不變」)。
    """
    out: dict[str, datetime] = {}
    with engine.connect() as connection:
        connection.execute(text(f"SET TIME ZONE '{session_tz}'"))
        for table, column in _CONVERTED + _ALREADY_TZ:
            value = connection.execute(
                text(f'SELECT "{column}" FROM "{table}" ORDER BY id LIMIT 1')  # noqa: S608
            ).scalar_one()
            out[f"{table}.{column}"] = value
    return out


# ── ③ UTC 判讀:絕對時點零平移是明示行為 ─────────────────────────────────────
def test_naive_values_are_reinterpreted_as_utc_wall_clock(throwaway_db):
    """轉型後讀回的 timestamptz = 「把既有 naive 值當 UTC 牆鐘」—— 絕對時點不變。

    這條把「零平移」**釘死成明示行為**,並反向斷言「不是台北判讀」——
    否則測試無法證明自己真的在測判讀方向。
    """
    url = throwaway_db
    _run_alembic(url, "upgrade", _predecessor(url))
    engine = create_engine(url)
    _seed_naive_governance_rows(engine)

    # 轉型前:(A) 組確實是 naive(前提檢查 —— 前提不成立的話後面的斷言沒意義)。
    before = _column_types(engine, {t for t, _ in _CONVERTED + _ALREADY_TZ})
    for table, column in _CONVERTED:
        assert before[f"{table}.{column}"] == "timestamp without time zone"
    for table, column in _ALREADY_TZ:
        assert before[f"{table}.{column}"] == "timestamp with time zone"

    _run_alembic(url, "upgrade", _REVISION)

    after = _column_types(engine, {t for t, _ in _CONVERTED + _ALREADY_TZ})
    for table, column in _CONVERTED + _ALREADY_TZ:
        assert after[f"{table}.{column}"] == "timestamp with time zone", (
            f"{table}.{column} 沒有轉成 timestamptz"
        )

    values = _read_converted_values(engine, session_tz="UTC")

    for table, column in _CONVERTED:
        key = f"{table}.{column}"
        actual = values[key]
        assert actual.tzinfo is not None, f"{key} 讀回來還是 naive"
        assert actual == _EXPECTED_AWARE, (
            f"{key}:期望「{_SEED_NAIVE} 視同 {_INTERPRETATION_TZ}」= "
            f"{_EXPECTED_AWARE.isoformat()},實得 {actual.isoformat()}"
        )
        # 把判讀方向寫成雙向斷言,而不是只寫在註解裡:若判讀被改回
        # 'Asia/Taipei',下面兩行會紅並指出差了幾小時。
        as_if_taipei = (_SEED_NAIVE - _TAIPEI_OFFSET).replace(tzinfo=timezone.utc)
        assert actual != as_if_taipei, (
            f"{key}:讀到「當成台北牆鐘」的時點 —— 判讀時區被改動了"
        )
        assert actual - as_if_taipei == _TAIPEI_OFFSET

    # (B) 組:既有值本來就是絕對時點,不該被平移。
    for table, column in _ALREADY_TZ:
        key = f"{table}.{column}"
        assert values[key] == _EXPECTED_AWARE, (
            f"{key} 被動到了 —— (B) 組在乾淨鏈上已是 timestamptz,r1_0040 不該轉換它"
        )

    # 跨 session TZ 重讀:絕對時點不變(C1 §g ②)。
    for tz in ("Asia/Taipei", "America/New_York", "Etc/UTC"):
        again = _read_converted_values(engine, session_tz=tz)
        assert again == values, f"session TimeZone={tz} 讀到不同的絕對時點"

    engine.dispose()


def test_post_migration_writes_are_true_utc_not_shifted(throwaway_db):
    """檔頭 ③ 的另一半:遷移**後**由 aware UTC 寫入的值與遷移前的紀錄**連續**。

    UTC 判讀下,同一個「09:30」牆鐘在遷移前(naive 轉型)與遷移後(aware 寫入)
    是同一個絕對時點 —— 零不連續。這條測試為「連續性」作證;若判讀被改成台北,
    這裡會出現 8 小時階梯並讓斷言紅掉。
    """
    url = throwaway_db
    _run_alembic(url, "upgrade", _predecessor(url))
    engine = create_engine(url)
    _seed_naive_governance_rows(engine)
    _run_alembic(url, "upgrade", _REVISION)

    written = datetime(2026, 3, 1, 9, 30, 0, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO classification_events"
                " (resource_type, resource_id, previous_level, new_level, reason,"
                "  created_at)"
                " VALUES ('conversation', '2', '無機密', '機密', 'manual_admin', :ts)"
            ),
            {"ts": written},
        )

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT resource_id, created_at FROM classification_events"
                " ORDER BY id"
            )
        ).all()
    engine.dispose()

    pre_migration = {r[0]: r[1] for r in rows}["1"]
    post_migration = {r[0]: r[1] for r in rows}["2"]

    assert post_migration == written, "遷移後寫入的 aware UTC 值不該被改動"
    # 同一個「09:30」的牆鐘,遷移前後是同一個絕對時點 —— 連續、零階梯。
    assert post_migration == pre_migration


# ── ①② 對稱 downgrade 與冪等 ─────────────────────────────────────────────────
def test_downgrade_restores_original_naive_bytes(throwaway_db):
    """``downgrade`` 用對稱 ``AT TIME ZONE 'UTC'`` 還原,資訊無損。"""
    url = throwaway_db
    predecessor = _predecessor(url)
    _run_alembic(url, "upgrade", predecessor)
    engine = create_engine(url)
    _seed_naive_governance_rows(engine)
    types_before = _column_types(engine, {t for t, _ in _CONVERTED + _ALREADY_TZ})

    _run_alembic(url, "upgrade", _REVISION)
    _run_alembic(url, "downgrade", predecessor)

    assert _column_types(engine, {t for t, _ in _CONVERTED + _ALREADY_TZ}) == types_before

    with engine.connect() as connection:
        for table, column in _CONVERTED:
            value = connection.execute(
                text(f'SELECT "{column}" FROM "{table}" ORDER BY id LIMIT 1')  # noqa: S608
            ).scalar_one()
            assert value == _SEED_NAIVE, (
                f"{table}.{column} downgrade 後不等於原始 naive 值:{value!r}"
            )
    engine.dispose()


def test_upgrade_downgrade_upgrade_is_idempotent(throwaway_db):
    """upgrade → downgrade → upgrade 的型別與值都回到第一次 upgrade 的狀態。"""
    url = throwaway_db
    predecessor = _predecessor(url)
    _run_alembic(url, "upgrade", predecessor)
    engine = create_engine(url)
    _seed_naive_governance_rows(engine)

    _run_alembic(url, "upgrade", _REVISION)
    first_types = _column_types(engine, {t for t, _ in _CONVERTED + _ALREADY_TZ})
    first_values = _read_converted_values(engine, session_tz="UTC")

    _run_alembic(url, "downgrade", predecessor)
    _run_alembic(url, "upgrade", _REVISION)

    assert _column_types(engine, {t for t, _ in _CONVERTED + _ALREADY_TZ}) == first_types
    assert _read_converted_values(engine, session_tz="UTC") == first_values
    engine.dispose()


def test_second_upgrade_on_already_converted_columns_is_a_noop(throwaway_db):
    """``upgrade`` 對已是 timestamptz 的欄跳過 —— 不會再平移一次 8 小時。

    這條防的是「重跑 migration 導致重複轉換」:如果 upgrade 沒有型別守衛,
    第二次 ``AT TIME ZONE`` 會先把 timestamptz 拆回 naive、再經 session TZ 的
    隱式轉換寫回 —— session TZ 非 UTC 時絕對時點就被平移,而治理紀錄不會有
    任何跡象顯示這件事發生過。
    """
    url = throwaway_db
    predecessor = _predecessor(url)
    _run_alembic(url, "upgrade", predecessor)
    engine = create_engine(url)
    _seed_naive_governance_rows(engine)
    _run_alembic(url, "upgrade", _REVISION)
    values = _read_converted_values(engine, session_tz="UTC")

    # 手動把 alembic_version 退回去、但**不**改 schema,然後再跑一次 upgrade ——
    # 模擬「版本表與實際 schema 不同步」的還原情境。
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :v"), {"v": predecessor}
        )
    _run_alembic(url, "upgrade", _REVISION)

    assert _read_converted_values(engine, session_tz="UTC") == values, (
        "重跑 upgrade 又平移了一次 —— 型別守衛沒生效"
    )
    engine.dispose()


# ── 例外掃描(C1 §c) ────────────────────────────────────────────────────────
def test_exception_scan_flags_future_governance_timestamps(throwaway_db, caplog):
    """治理帳出現「未來 > 1h」的時點時,upgrade 要留 WARNING 但**不阻斷**。"""
    import logging

    url = throwaway_db
    _run_alembic(url, "upgrade", _predecessor(url))
    engine = create_engine(url)
    _seed_naive_governance_rows(engine)
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO classification_events"
                " (resource_type, resource_id, previous_level, new_level, reason,"
                "  created_at) VALUES ('conversation', '99', '無機密', '機密',"
                " 'manual_admin', :ts)"
            ),
            {"ts": future},
        )

    with caplog.at_level(logging.WARNING, logger="alembic.runtime.migration"):
        _run_alembic(url, "upgrade", _REVISION)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "例外掃描" in messages and "classification_events.created_at" in messages, (
        f"例外掃描沒有標記未來時點。實際 log:\n{messages}"
    )
    # api_keys.expires_at 逐筆列出(C1 §c 對這個欄的特別要求)。
    assert "api_keys.expires_at" in messages and "逐筆列出" in messages

    # 不阻斷:轉型仍然完成。
    assert (
        _column_types(engine, {"classification_events"})[
            "classification_events.created_at"
        ]
        == "timestamp with time zone"
    )
    engine.dispose()
