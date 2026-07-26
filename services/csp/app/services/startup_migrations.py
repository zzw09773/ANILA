"""Startup schema verification —— **檢查,不自癒**(W2-6)。

這個模組以前是什麼
------------------
它以前是 alembic **之外的第二套 schema 機制**:每次啟動對 8 張表跑
``ADD COLUMN IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS``,甚至在
啟動路徑上跑 ``ALTER TABLE ... ALTER COLUMN ... TYPE``;另外還會無條件探測
``/app/legacy-data/csp.db`` 與 ``data/csp.db``,存在就把整個 SQLite 庫倒進
PostgreSQL(docstring 說是 opt-in,實作是無條件)。

兩個後果:

1. ``alembic upgrade head`` 對乾淨 DB **得不到可用 schema** —— 那些欄位/索引
   沒有任何 migration 版本紀錄,只有「啟動過一次 app」才會出現。**DR 還原路徑
   因此是壞的**:還原完的庫在跑起 app 之前不符合 ORM,而還原驗證通常只看
   ``alembic_version``。
2. 啟動時自癒會遮蔽真正的問題:一個 schema 落後的庫會被靜默補到「差不多能
   用」,然後以 healthy 的樣子上線。

現在是什麼
----------
``r1_0036_absorb_startup_ddl`` 把那些 DDL 全量收編進 alembic,所以 alembic
重新成為唯一的 schema 權威。本模組只剩一件事:**驗證跑起來的庫真的符合 ORM,
不符合就拒絕啟動並指向 alembic**。不再有任何 DDL。

legacy SQLite 匯入路徑已整段刪除(不是停用、不是留 flag):威脅前提已死
(現行部署一律 PostgreSQL,沒有 SQLite 來源),而「無條件探測檔案路徑並在
命中時整庫複製」本身就是個不該留在啟動路徑上的動作。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine

import app.models  # noqa: F401  —— 讓 Base.metadata 收齊所有表再做比對
from app.database import Base, engine

logger = logging.getLogger(__name__)


_ALEMBIC_HINT = (
    "schema 權威是 alembic:請對該資料庫執行 `alembic upgrade head`"
    "(容器內:`cd /app && python -m alembic upgrade head`,需要 "
    "MIGRATION_DATABASE_URL 指向具備 DDL 權限的角色)。"
    "本服務**不再**於啟動時自行補 DDL —— 自癒會把落後的庫偽裝成健康的庫。"
)


class SchemaOutOfDateError(RuntimeError):
    """資料庫 schema 與 ORM 宣告不符 —— 拒絕啟動。

    ``main._apply_schema_migrations`` 會把它記成 ``migration_status=failed``
    並讓 ``/ready`` 維持 503,所以這個例外**必須**往外傳,不能只 log。
    """


def run_startup_migrations() -> None:
    """啟動前的 schema 驗證。名稱保留是因為 ``app.main`` 在呼叫它。

    ⚠ 這裡刻意不做任何 DDL。若拋出 ``SchemaOutOfDateError``,呼叫端
    (``main._apply_schema_migrations``)會把它轉成 ``RuntimeError`` 並讓
    程序拒絕啟動。
    """
    verify_schema(engine)


def verify_schema(bind: Engine | Connection) -> None:
    """庫不符合 ORM 宣告就丟 ``SchemaOutOfDateError``。"""
    gaps = collect_schema_gaps(bind)
    if not gaps:
        logger.info("schema 驗證通過(%d 張 ORM 表)", len(Base.metadata.tables))
        return

    shown = gaps[:20]
    more = f"(另有 {len(gaps) - len(shown)} 項未列出)" if len(gaps) > len(shown) else ""
    raise SchemaOutOfDateError(
        "資料庫 schema 落後於程式碼,拒絕啟動。缺少:"
        + "、".join(shown)
        + more
        + " —— "
        + _ALEMBIC_HINT
    )


def collect_schema_gaps(bind: Engine | Connection) -> list[str]:
    """列出「ORM 要求有、資料庫沒有」的項目。

    刻意只查三類**名稱層面**的缺口,不做通用型別比對:

    * ``MISSING_TABLE`` / ``MISSING_COLUMN`` —— 缺了就是 API 直接 500。
    * ``MISSING_UNIQUE`` —— ORM 宣告 ``unique=True`` 的單欄。這一項不是潔癖:
      ``alert_service.upsert_alert`` 的 ``INSERT ... ON CONFLICT (fingerprint)``
      在缺少對應唯一索引的庫上會以 ``InvalidColumnReference`` 失敗,而那條路徑
      是每 60 秒跑一次的健康檢查 —— 缺約束的庫必須在啟動時就被擋下,而不是等
      第一輪健康檢查才炸。

    通用型別比對留給 CI 的 ``infra/ci/check_orm_pg_drift.py``(它在乾淨庫上
    跑,結果才有意義);啟動路徑要的是快、且零誤報。
    """
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())
    gaps: list[str] = []

    for table in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        if table.name not in existing_tables:
            gaps.append(f"表 {table.name}")
            continue

        db_columns = {c["name"] for c in inspector.get_columns(table.name)}
        missing_columns = [c.name for c in table.columns if c.name not in db_columns]
        gaps.extend(f"欄位 {table.name}.{name}" for name in sorted(missing_columns))

        unique_declared = [
            c.name for c in table.columns if c.unique and c.name not in missing_columns
        ]
        if not unique_declared:
            continue

        # UNIQUE 可以由 unique 約束、unique 索引、或主鍵滿足 —— 三者都算,
        # 否則會對 `create_index(..., unique=True)` 建出來的欄位誤報
        # (`registered_services.slug`、`tasks.trace_id` 就是那樣建的)。
        satisfied: set[tuple[str, ...]] = {
            tuple(uc["column_names"])
            for uc in inspector.get_unique_constraints(table.name)
        }
        satisfied |= {
            tuple(ix["column_names"])
            for ix in inspector.get_indexes(table.name)
            if ix.get("unique")
        }
        pk = tuple(inspector.get_pk_constraint(table.name).get("constrained_columns") or ())
        if pk:
            satisfied.add(pk)

        gaps.extend(
            f"唯一約束 {table.name}.{name}"
            for name in sorted(unique_declared)
            if (name,) not in satisfied
        )

    return gaps
