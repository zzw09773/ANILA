"""Absorb `startup_migrations.py` DDL into alembic (W2-6).

Revision ID: r1_0036
Revises: r1_0035

為什麼有這支 migration
----------------------
`services/csp/app/services/startup_migrations.py` 是 alembic **之外的第二套
schema 機制**:它在每次啟動時對 8 張表跑 ``ADD COLUMN IF NOT EXISTS`` /
``CREATE INDEX IF NOT EXISTS``,甚至跑 ``ALTER TYPE``。後果是
``alembic upgrade head`` 對乾淨 DB **得不到可用 schema** —— 也就是說
**DR 還原路徑是壞的**(還原後的庫要「啟動過一次 app」才會補齊欄位,而那些
DDL 沒有任何 migration 版本紀錄)。

本 revision 把那些 DDL 全量收編,讓 alembic 重新成為唯一的 schema 權威。
收編後 `run_startup_migrations()` 降級為「檢查 + 拒啟動」,不再自癒。

⚠ 冪等性:這支必須能安全地跑在**已被 startup DDL 補過欄位的既有庫**上
--------------------------------------------------------------------
`.15`(內網平台主機)與各 dev stack 的庫都已經被 startup DDL 補過欄位/索引,
所以本 revision 一律寫成「**補齊缺失**」而不是「新建」:

* 欄位:``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``
* 索引:``CREATE INDEX IF NOT EXISTS``
* 約束(``ADD CONSTRAINT`` 沒有 ``IF NOT EXISTS``):先查 ``pg_constraint``
  再決定要不要建
* 型別轉換:先查 ``information_schema.columns``,已經是目標型別就跳過

⚠ ``alerts.fingerprint`` 的 UNIQUE:必須先去重才建得起來
--------------------------------------------------------
startup DDL 當初是用 ``fingerprint VARCHAR(200) NOT NULL DEFAULT ''`` 補這個
欄位的,所以**既有庫裡每一列在該欄位加上之前就存在的告警,fingerprint 都是
空字串**。直接 ``ADD CONSTRAINT ... UNIQUE`` 會在第二列就撞
``duplicate key value violates unique constraint`` 而讓整個 upgrade 炸掉。

處理順序(見 ``_dedupe_alert_fingerprints``):

1. 空字串是 startup DDL 的**哨兵值**,不是真的指紋 → 換成 ``legacy:<id>``。
   刻意**不刪列**:那些是彼此不同的歷史告警,刪掉就是無紀錄的資料銷毀。
2. 同一個**非空** fingerprint 真的有多列(併發下 read-then-write 去重失效
   造成的重複)→ 保留最新一列(``last_seen_at DESC NULLS LAST, id DESC``),
   其餘刪除。這是 upstream 缺陷的殘留,唯一能保留的語意就是最新那筆。
3. 去掉 ``DEFAULT ''``。ORM 沒宣告 server_default,而留著它 + UNIQUE 等於
   埋一顆「第二筆沒帶 fingerprint 的 insert 會炸」的雷;拿掉之後這種 insert
   會立刻以 NOT NULL 失敗,錯得明顯。

⚠ ``audit_logs.resource_id``:INTEGER → VARCHAR(100)
---------------------------------------------------
0001 baseline 宣告 INTEGER 而 ORM 宣告 ``String(100)``。這個不一致**已經造成
過生產事故**:``GET /api/audit-logs`` 因為 PG 回 int 而觸發 Pydantic
``ResponseValidationError``(見補救計畫 §W2-2 引用的
``startup_migrations.py:245-247`` 註解)。startup DDL 有在補,所以收編進來。

downgrade 的取捨
----------------
downgrade 會把本 revision 新增的欄位/索引/約束移除,但**只在乾淨庫上是無損
的**。``audit_logs.resource_id`` 回轉 INTEGER 必然是有損的(非數字的
resource_id 會變 NULL),已在 ``downgrade()`` 內註明。降版是 DR/測試用途,
不是生產操作。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "r1_0036"
down_revision: Union[str, None] = "r1_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── 收編清單:每一條都對應 startup_migrations.py 現在做的一件事 ─────────────
#
# (table, column, ddl_type_and_constraints)
#
# 順序刻意與 startup_migrations._ensure_schema_backfills 一致,方便逐條對照。
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # --- users (startup_migrations.py:86-108) ---------------------------
    ("users", "token_version", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "is_approved", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("users", "department_id", "INTEGER"),          # FK 另外建,見 _FOREIGN_KEYS
    ("users", "updated_at", "TIMESTAMP NULL"),
    # --- model_registry (startup_migrations.py:111-152) -----------------
    ("model_registry", "health_status", "VARCHAR(20) DEFAULT 'offline'"),
    ("model_registry", "health_checked_at", "TIMESTAMP NULL"),
    ("model_registry", "context_window", "INTEGER NULL"),
    ("model_registry", "base_model_id", "INTEGER"),  # FK 另外建
    ("model_registry", "is_internal", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("model_registry", "updated_at", "TIMESTAMP NULL"),
    # --- token_usage (startup_migrations.py:155-178) --------------------
    ("token_usage", "department_id", "INTEGER"),     # FK 另外建
    (
        "token_usage",
        "request_timestamp",
        "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    ),
    ("token_usage", "request_duration_ms", "INTEGER NULL"),
    # --- departments (startup_migrations.py:191-195) --------------------
    ("departments", "updated_at", "TIMESTAMP NULL"),
    # --- api_keys (startup_migrations.py:198-206) ----------------------
    ("api_keys", "expires_at", "TIMESTAMP NULL"),
    ("api_keys", "key_suffix", "VARCHAR(4) NOT NULL DEFAULT ''"),
    # --- alerts (startup_migrations.py:209-227) ------------------------
    ("alerts", "category", "VARCHAR(50) NOT NULL DEFAULT 'general'"),
    ("alerts", "severity", "VARCHAR(20) NOT NULL DEFAULT 'info'"),
    ("alerts", "status", "VARCHAR(20) NOT NULL DEFAULT 'open'"),
    ("alerts", "fingerprint", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("alerts", "source_type", "VARCHAR(50) NULL"),
    ("alerts", "source_id", "VARCHAR(100) NULL"),
    ("alerts", "first_seen_at", "TIMESTAMP NULL"),
    ("alerts", "last_seen_at", "TIMESTAMP NULL"),
    ("alerts", "acknowledged_at", "TIMESTAMP NULL"),
    ("alerts", "acknowledged_by_user_id", "INTEGER NULL"),  # FK 另外建
    ("alerts", "resolved_at", "TIMESTAMP NULL"),
    ("alerts", "metadata_json", "TEXT NULL"),
    # --- audit_logs (startup_migrations.py:230-241) --------------------
    ("audit_logs", "status", "VARCHAR(20) NOT NULL DEFAULT 'ok'"),
    ("audit_logs", "actor_user_id", "INTEGER NULL"),
    ("audit_logs", "actor_username", "VARCHAR(100) NULL"),
    ("audit_logs", "ip_address", "VARCHAR(64) NULL"),
    ("audit_logs", "metadata_json", "TEXT NULL"),
    # --- platform_links (startup_migrations.py:250-258) ----------------
    ("platform_links", "icon", "VARCHAR(50) NULL"),
    ("platform_links", "sort_order", "INTEGER NULL DEFAULT 0"),
)


# token_usage 的 5 條複合索引(startup_migrations.py:181-188)
# + ORM 宣告了 index=True 但 migration chain 從未建立的三條
#   (users.department_id / attachments.message_id / alerts 的 5 條)。
_INDEXES: tuple[tuple[str, str, str], ...] = (
    # (index_name, table, column_list)
    ("idx_usage_user_time", "token_usage", "user_id, request_timestamp"),
    ("idx_usage_department_time", "token_usage", "department_id, request_timestamp"),
    ("idx_usage_model_time", "token_usage", "model_id, request_timestamp"),
    ("idx_usage_timestamp", "token_usage", "request_timestamp"),
    ("idx_usage_apikey_time", "token_usage", "api_key_id, request_timestamp"),
    # ORM `index=True` 但 chain 沒建(drift gate 的 MISSING_INDEX)
    ("ix_users_department_id", "users", "department_id"),
    ("ix_attachments_message_id", "attachments", "message_id"),
    ("ix_alerts_category", "alerts", "category"),
    ("ix_alerts_severity", "alerts", "severity"),
    ("ix_alerts_source_type", "alerts", "source_type"),
    ("ix_alerts_status", "alerts", "status"),
    ("ix_alerts_last_seen_at", "alerts", "last_seen_at"),
)


# ORM 宣告了 FK 但 chain / startup DDL 沒建齊的。
# startup DDL 只在「欄位不存在」時才會帶上 REFERENCES —— 對已經有該欄位的舊庫
# 就永遠補不上 FK,所以這裡一律獨立檢查 pg_constraint 再建。
_FOREIGN_KEYS: tuple[tuple[str, str, str, str, str], ...] = (
    # (constraint_name, table, column, ref_table, ref_column + 動作)
    (
        "alerts_acknowledged_by_user_id_fkey",
        "alerts",
        "acknowledged_by_user_id",
        "users",
        "id",
    ),
)


def _has_table(bind, table: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def _has_column(bind, table: str, column: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_constraint(bind, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar()
    )


def _has_index(bind, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind = 'i' AND c.relname = :n "
                "AND n.nspname = current_schema()"
            ),
            {"n": name},
        ).scalar()
    )


def _column_data_type(bind, table: str, column: str) -> str | None:
    return bind.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    ).scalar()


def _dedupe_alert_fingerprints(bind) -> None:
    """建 UNIQUE 之前把 ``alerts.fingerprint`` 的重複清掉。

    見模組 docstring「⚠ alerts.fingerprint 的 UNIQUE」段。對乾淨庫這三個
    statement 全部是 no-op(表是空的);對 `.15` 這種被 startup DDL 補過欄位
    的既有庫,它們是本 revision 能不能跑完的前提。
    """
    # 1) startup DDL 的 '' 哨兵 → 唯一的合成指紋。刻意不刪列。
    op.execute(
        "UPDATE alerts SET fingerprint = 'legacy:' || id "
        "WHERE fingerprint IS NULL OR fingerprint = ''"
    )
    # 2) 真重複 → 保留最新一列(last_seen_at 最新、同值時 id 最大)
    op.execute(
        """
        DELETE FROM alerts
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY fingerprint
                           ORDER BY last_seen_at DESC NULLS LAST, id DESC
                       ) AS rn
                FROM alerts
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    # 3) 拿掉 DEFAULT ''(ORM 沒宣告 server_default,留著 + UNIQUE 是雷)
    op.execute("ALTER TABLE alerts ALTER COLUMN fingerprint DROP DEFAULT")


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. 欄位:逐條 ADD COLUMN IF NOT EXISTS ────────────────────────────
    for table, column, spec in _COLUMNS:
        if not _has_table(bind, table):
            # 收編清單裡的表都由更早的 revision 建立;真的缺表代表 chain
            # 斷了,讓後續 statement 自然爆比在這裡靜默跳過安全。
            continue
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {spec}"
        )

    # ── 2. alerts:legacy 時間戳回填 → 去重 → UNIQUE ─────────────────────
    #
    # 0001 baseline 的 alerts 有 created_at(ORM 沒宣告,是遺留欄)。既有列的
    # first_seen_at / last_seen_at 是 NULL,而 `GET /api/alerts` 用
    # `ORDER BY last_seen_at DESC` —— 不回填的話歷史告警在列表裡永遠沉底。
    if _has_column(bind, "alerts", "created_at"):
        op.execute(
            "UPDATE alerts SET first_seen_at = created_at "
            "WHERE first_seen_at IS NULL"
        )
        op.execute(
            "UPDATE alerts SET last_seen_at = created_at "
            "WHERE last_seen_at IS NULL"
        )

    _dedupe_alert_fingerprints(bind)

    # UNIQUE 約束(不是只有 unique index):`alert_service` 的
    # `INSERT ... ON CONFLICT (fingerprint) DO UPDATE` 需要它,而 ORM 也宣告
    # 了 `unique=True`。
    if not _has_constraint(bind, "uq_alerts_fingerprint"):
        op.execute(
            "ALTER TABLE alerts ADD CONSTRAINT uq_alerts_fingerprint "
            "UNIQUE (fingerprint)"
        )

    # ── 3. 索引 ──────────────────────────────────────────────────────────
    for index_name, table, columns in _INDEXES:
        if not _has_table(bind, table):
            continue
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({columns})"
        )

    # ── 4. 既有 unique index 升級為 UNIQUE 約束 ───────────────────────────
    #
    # `registered_services.slug`(r1_0006:163)與 `tasks.trace_id`
    # (r1_0001:97)是用 `create_index(..., unique=True)` 建的 —— 那是 unique
    # **index**,不是 unique **constraint**。ORM 兩邊都宣告 `unique=True`,而
    # drift gate 查的是 `get_unique_constraints()`,所以會報 MISSING_UNIQUE。
    #
    # `ADD CONSTRAINT <同名> UNIQUE USING INDEX <同名>` 把現有索引原地升級成
    # 約束背後的索引:**不會多一份索引、不會改索引名稱**(實測 psql \d 顯示
    # 「UNIQUE CONSTRAINT」而名稱仍是 ix_...),所以既有 migration 的
    # drop_index 與任何按名字找索引的程式都不受影響。
    for index_name, table in (
        ("ix_registered_services_slug", "registered_services"),
        ("ix_tasks_trace_id", "tasks"),
    ):
        if not _has_table(bind, table):
            continue
        if _has_constraint(bind, index_name):
            continue
        if not _has_index(bind, index_name):
            continue
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {index_name} "
            f"UNIQUE USING INDEX {index_name}"
        )

    # ── 5. FK ────────────────────────────────────────────────────────────
    for name, table, column, ref_table, ref_column in _FOREIGN_KEYS:
        if not _has_table(bind, table) or not _has_column(bind, table, column):
            continue
        if _has_constraint(bind, name):
            continue
        # 既有庫的 acknowledged_by_user_id 是 startup DDL 以「純 INTEGER、無
        # FK」補上的 → 可能殘留指向已刪除使用者的孤兒值。不先清掉,ADD
        # CONSTRAINT 會以 violates foreign key constraint 讓 upgrade 中止。
        op.execute(
            f"UPDATE {table} SET {column} = NULL WHERE {column} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {ref_table} r "
            f"WHERE r.{ref_column} = {table}.{column})"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column})"
        )

    # users.department_id / model_registry.base_model_id / token_usage.
    # department_id 的 FK 在既有 chain 裡已經有(0012 / 0001),startup DDL 只
    # 是在欄位缺席時順手帶上 REFERENCES。這裡一併補檢查:若某個庫是「欄位有、
    # FK 沒有」的中間態(startup DDL 補欄位時 REFERENCES 被別的路徑吃掉),
    # 也要能補齊。
    for name, table, column, ref_table, ref_column, ondelete in (
        (
            "users_department_id_fkey", "users", "department_id",
            "departments", "id", "ON DELETE SET NULL",
        ),
        (
            "model_registry_base_model_id_fkey", "model_registry",
            "base_model_id", "model_registry", "id", "",
        ),
        (
            "token_usage_department_id_fkey", "token_usage", "department_id",
            "departments", "id", "",
        ),
    ):
        if not _has_table(bind, table) or not _has_column(bind, table, column):
            continue
        existing = bind.execute(
            sa.text(
                """
                SELECT 1
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid
                 AND att.attnum = ANY (con.conkey)
                WHERE con.contype = 'f'
                  AND nsp.nspname = current_schema()
                  AND rel.relname = :t
                  AND att.attname = :c
                """
            ),
            {"t": table, "c": column},
        ).scalar()
        if existing:
            continue
        op.execute(
            f"UPDATE {table} SET {column} = NULL WHERE {column} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {ref_table} r "
            f"WHERE r.{ref_column} = {table}.{column})"
        )
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} "
            f"FOREIGN KEY ({column}) REFERENCES {ref_table} ({ref_column}) "
            f"{ondelete}".strip()
        )

    # ── 6. audit_logs.resource_id INTEGER → VARCHAR(100) ─────────────────
    #
    # startup_migrations._ensure_column_type_varchar(:245-247)。這是啟動時跑
    # ALTER TYPE 的那一條 —— 收編後啟動路徑不再碰 schema。
    if _has_column(bind, "audit_logs", "resource_id"):
        current = _column_data_type(bind, "audit_logs", "resource_id")
        if current not in ("character varying", "text", None):
            op.execute(
                "ALTER TABLE audit_logs ALTER COLUMN resource_id "
                "TYPE VARCHAR(100) USING resource_id::varchar"
            )


# ── downgrade 刻意與 upgrade 不對稱 ────────────────────────────────────────
#
# upgrade 是「補齊缺失」的**超集**:它對既有庫(`.15`)也要能把 startup DDL
# 補過的每一條都納管,所以清單包含許多在乾淨 chain 上早就由別的 revision 建好
# 的欄位/索引(例:`users.token_version`、`idx_usage_*` 由 r1_0018 建)。
#
# downgrade 只能撤銷「**在乾淨 chain 上真的由本 revision 產生**」的那些,否則
# `upgrade head` → `downgrade -1` 會把 r1_0018/0001 的成果一起刪掉,把庫留在
# 比 r1_0035 更殘缺的狀態。以下兩個清單就是「r1_0035 上實測不存在」的集合。
_OWNED_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (table, column) for table, column, _spec in _COLUMNS if table == "alerts"
)
_OWNED_INDEXES: tuple[str, ...] = (
    "ix_users_department_id",
    "ix_attachments_message_id",
    "ix_alerts_category",
    "ix_alerts_severity",
    "ix_alerts_source_type",
    "ix_alerts_status",
    "ix_alerts_last_seen_at",
)


def downgrade() -> None:
    """回到 r1_0035。

    ⚠ 這個方向對**有資料的庫是有損的**:``audit_logs.resource_id`` 回轉
    INTEGER 時,任何非數字的值(收編之後才寫得進去的 UUID 型 resource_id)會
    變成 NULL。降版是 DR/測試用途,生產請走前滾。
    """
    bind = op.get_bind()

    # audit_logs.resource_id → INTEGER(有損,見 docstring)
    if _has_column(bind, "audit_logs", "resource_id"):
        current = _column_data_type(bind, "audit_logs", "resource_id")
        if current in ("character varying", "text"):
            op.execute(
                "ALTER TABLE audit_logs ALTER COLUMN resource_id TYPE INTEGER "
                "USING NULLIF(regexp_replace(resource_id, '[^0-9]', '', 'g'), '')"
                "::integer"
            )

    # UNIQUE 約束降回 unique index(名稱不變;DROP CONSTRAINT 會連帶刪掉約束
    # 背後的索引,所以要重建)
    for index_name, table, column in (
        ("ix_registered_services_slug", "registered_services", "slug"),
        ("ix_tasks_trace_id", "tasks", "trace_id"),
    ):
        if not _has_table(bind, table):
            continue
        if not _has_constraint(bind, index_name):
            continue
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {index_name}")
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
            f"ON {table} ({column})"
        )

    for name, table, *_rest in _FOREIGN_KEYS:
        if _has_constraint(bind, name):
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")

    for index_name in _OWNED_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")

    if _has_constraint(bind, "uq_alerts_fingerprint"):
        op.execute("ALTER TABLE alerts DROP CONSTRAINT uq_alerts_fingerprint")

    for table, column in _OWNED_COLUMNS:
        if not _has_table(bind, table):
            continue
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
