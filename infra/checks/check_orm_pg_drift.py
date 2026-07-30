#!/usr/bin/env python3
"""ORM ↔ 真 Postgres schema 漂移檢查（手動、非 CI）。

來源：attic/2026-07-28/main `infra/ci/check_orm_pg_drift.py`（4524598）——
**改寫回收**：保留 exit code 分流、FK 形狀比對、UNDECLARED_COLUMN、TZ 屬性讀取；
拿掉 CI ratchet baseline；補上本任務要的 TYPE_MISMATCH／NULLABILITY_MISMATCH。

刻意不做「所有 SQL 型別字串全等比對」：embedding/halfvec、JSON↔JSONB 等會誤報。
型別比對走精簡正規化＋已知例外表；其餘只報明顯分叉。

用法
----
    # 政策：所有 DateTime 須 timezone=True（不需 DB）
    python infra/checks/check_orm_pg_drift.py --mode policy

    # 漂移：對已 alembic upgrade 的 scratch DSN
    python infra/checks/check_orm_pg_drift.py --mode drift \\
        --dsn postgresql+psycopg2://user:pw@host:5432/scratch

Exit codes
----------
    0  乾淨
    1  檢查本身失敗（連不上、缺 driver、import 失敗）
    2  用法錯誤
    3  有發現（人看報告決定要不要修；不擋 commit）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CSP_ROOT = REPO_ROOT / "services" / "csp"

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2
EXIT_FINDINGS = 3

# 已知「看起來型別名不同、但契約正確」的 ORM→PG 配對（小寫）。
TYPE_EQUIV = {
    ("text", "halfvec"),
    ("varchar", "character varying"),
    ("string", "character varying"),
    ("string", "varchar"),
    ("json", "jsonb"),
    ("float", "double precision"),
    ("double", "double precision"),
    ("bool", "boolean"),
    ("int", "integer"),
    ("largebinary", "bytea"),
    ("decimal", "numeric"),
}

STRUCTURAL_KINDS = (
    "MISSING_TABLE",
    "MISSING_COLUMN",
    "TYPE_MISMATCH",
    "NULLABILITY_MISMATCH",
    "MISSING_INDEX",
    "MISSING_UNIQUE",
    "MISSING_FK",
)


def _load_metadata():
    sys.path.insert(0, str(CSP_ROOT))
    from app.database import Base  # noqa: E402
    import app.models  # noqa: F401,E402

    return Base.metadata


def collect_naive_datetime_columns(metadata) -> list[str]:
    from sqlalchemy import DateTime

    out: list[str] = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        for col in table.columns:
            type_ = col.type
            if isinstance(type_, DateTime) and not getattr(type_, "timezone", False):
                out.append(f"{table.name}.{col.name}")
    return out


def run_policy() -> int:
    metadata = _load_metadata()
    naive = collect_naive_datetime_columns(metadata)
    print(f"Policy[naive DateTime]: {len(naive)} 欄未宣告 timezone=True")
    for col in naive[:40]:
        print(f"  ✖ {col}")
    if len(naive) > 40:
        print(f"  …另有 {len(naive) - 40} 欄")
    if naive:
        print(
            "\n新欄請一律 DateTime(timezone=True)。"
            "既有 naive 欄是歷史債，進 PLAN 逐批清，不在本檢查修。",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    return EXIT_OK


def _norm_type_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", str(raw).strip().lower())
    s = re.sub(r"\(.*\)$", "", s).strip()
    aliases = {
        "character varying": "varchar",
        "character": "char",
        "double precision": "float",
        "timestamptz": "timestamptz",
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
        "timestamp": "timestamp",
    }
    return aliases.get(s, s)


def _orm_type_key(col) -> tuple[str, bool | None]:
    """回傳 (正規化型別名, timezone 或 None)。"""
    from sqlalchemy import DateTime, JSON
    from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID

    t = col.type
    name = type(t).__name__.lower()

    if isinstance(t, DateTime):
        tz = bool(getattr(t, "timezone", False))
        return ("timestamptz" if tz else "timestamp", tz)

    if isinstance(t, (JSON, JSONB)) or name in {"json", "jsonb"}:
        return ("json", None)
    if isinstance(t, UUID) or name == "uuid":
        return ("uuid", None)
    if isinstance(t, BYTEA) or name in {"bytea", "largebinary"}:
        return ("bytea", None)
    if isinstance(t, ARRAY) or name == "array":
        return ("array", None)

    # JSON().with_variant(JSONB, "postgresql") → 仍當 json
    variants = getattr(t, "variants", None) or {}
    if variants or name == "jsonvariant":
        return ("json", None)

    visit = str(getattr(t, "__visit_name__", None) or name).lower()
    mapping = {
        "string": "varchar",
        "varchar": "varchar",
        "text": "text",
        "unicode": "text",
        "unicodetext": "text",
        "integer": "integer",
        "int": "integer",
        "bigint": "bigint",
        "smallint": "smallint",
        "boolean": "boolean",
        "bool": "boolean",
        "float": "float",
        "double": "float",
        "numeric": "numeric",
        "decimal": "numeric",
        "date": "date",
        "enum": "enum",
    }
    if visit in mapping:
        return (mapping[visit], None)
    return (_norm_type_name(t), None)


def _types_compatible(orm_key: str, db_key: str) -> bool:
    if orm_key == db_key:
        return True
    pair = (orm_key, db_key)
    if pair in TYPE_EQUIV or (db_key, orm_key) in TYPE_EQUIV:
        return True
    # halfvec / vector 常被 ORM 宣告成 Text
    if orm_key == "text" and db_key in {"halfvec", "vector", "USER-DEFINED".lower()}:
        return True
    if db_key.startswith("halfvec") or db_key.startswith("vector"):
        return orm_key in {"text", "varchar", "user-defined", "null"}
    if orm_key == "json" and db_key in {"json", "jsonb"}:
        return True
    if orm_key == "varchar" and db_key in {"varchar", "text", "character varying"}:
        return True
    return False


def run_drift(dsn: str) -> int:
    from sqlalchemy import DateTime, create_engine, inspect

    metadata = _load_metadata()
    engine = create_engine(dsn)
    insp = inspect(engine)

    db_tables = set(insp.get_table_names())
    findings: list[dict] = []

    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        if table.name not in db_tables:
            findings.append({"kind": "MISSING_TABLE", "target": table.name})
            continue

        db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        declared = {c.name for c in table.columns}

        for name in sorted(set(db_cols) - declared):
            findings.append(
                {
                    "kind": "UNDECLARED_COLUMN",
                    "target": f"{table.name}.{name}",
                    "detail": "DB 有、ORM 無 → 接進 ORM 或 migration 刪掉",
                }
            )

        for col in table.columns:
            if col.name not in db_cols:
                findings.append(
                    {"kind": "MISSING_COLUMN", "target": f"{table.name}.{col.name}"}
                )
                continue

            db_meta = db_cols[col.name]
            db_type = db_meta["type"]
            db_type_name = _norm_type_name(db_type)
            orm_key, orm_tz = _orm_type_key(col)

            # timezone 專查（DateTime）
            if isinstance(col.type, DateTime) or orm_tz is not None:
                db_tz = bool(getattr(db_type, "timezone", False)) or (
                    "WITH TIME ZONE" in str(db_type).upper()
                    or "TIMESTAMPTZ" in str(db_type).upper()
                    or db_type_name == "timestamptz"
                )
                want_tz = bool(orm_tz)
                if want_tz != db_tz:
                    findings.append(
                        {
                            "kind": "TZ_MISMATCH",
                            "target": f"{table.name}.{col.name}",
                            "detail": (
                                f"ORM timezone={want_tz} / DB "
                                f"{'timestamptz' if db_tz else 'timestamp'}"
                            ),
                        }
                    )

            if not _types_compatible(orm_key, db_type_name):
                # TZ 已報就不重複 TYPE
                if not (
                    orm_key in {"timestamp", "timestamptz"}
                    and db_type_name in {"timestamp", "timestamptz"}
                ):
                    findings.append(
                        {
                            "kind": "TYPE_MISMATCH",
                            "target": f"{table.name}.{col.name}",
                            "detail": f"ORM~{orm_key} / DB~{db_type_name}",
                        }
                    )

            orm_null = bool(col.nullable)
            db_null = bool(db_meta.get("nullable", True))
            if orm_null != db_null:
                findings.append(
                    {
                        "kind": "NULLABILITY_MISMATCH",
                        "target": f"{table.name}.{col.name}",
                        "detail": f"ORM nullable={orm_null} / DB nullable={db_null}",
                    }
                )

        db_index_cols = {
            tuple(ix["column_names"]) for ix in insp.get_indexes(table.name)
        }
        db_unique_cols = {
            tuple(uc["column_names"])
            for uc in insp.get_unique_constraints(table.name)
        }
        # unique index 也算 unique 約束形狀
        for ix in insp.get_indexes(table.name):
            if ix.get("unique"):
                db_unique_cols.add(tuple(ix["column_names"]))
        pk_cols = tuple(
            insp.get_pk_constraint(table.name).get("constrained_columns") or ()
        )
        for col in table.columns:
            single = (col.name,)
            if col.index and single not in db_index_cols and single != pk_cols:
                findings.append(
                    {"kind": "MISSING_INDEX", "target": f"{table.name}.{col.name}"}
                )
            if col.unique and single not in db_unique_cols and single != pk_cols:
                findings.append(
                    {"kind": "MISSING_UNIQUE", "target": f"{table.name}.{col.name}"}
                )

        orm_fk_shapes = {}
        for fkc in table.foreign_key_constraints:
            elements = list(fkc.elements)
            if not elements:
                continue
            shape = (
                elements[0].column.table.name,
                frozenset((fk.parent.name, fk.column.name) for fk in elements),
            )
            orm_fk_shapes[shape] = fkc.name or "(未命名)"

        db_fk_shapes = set()
        for fk in insp.get_foreign_keys(table.name):
            constrained = fk.get("constrained_columns") or []
            referred_cols = fk.get("referred_columns") or []
            if not constrained or not referred_cols:
                continue
            db_fk_shapes.add(
                (fk["referred_table"], frozenset(zip(constrained, referred_cols)))
            )

        for shape, name in sorted(orm_fk_shapes.items(), key=lambda kv: str(kv[0])):
            if shape in db_fk_shapes:
                continue
            referred, pairs = shape
            cols = ", ".join(f"{a}→{referred}.{b}" for a, b in sorted(pairs))
            findings.append(
                {
                    "kind": "MISSING_FK",
                    "target": f"{table.name} [{name}]",
                    "detail": cols,
                }
            )

        for referred, pairs in sorted(db_fk_shapes - set(orm_fk_shapes), key=str):
            cols = ", ".join(f"{a}→{referred}.{b}" for a, b in sorted(pairs))
            findings.append(
                {
                    "kind": "UNDECLARED_FK",
                    "target": table.name,
                    "detail": cols,
                }
            )

    # 摘要：同 kind 合併計數，細節最多印 30 條
    from collections import Counter

    counts = Counter(f["kind"] for f in findings)
    print(
        f"Drift: ORM {len(metadata.tables)} 表 vs DB — {len(findings)} findings"
    )
    for kind, n in sorted(counts.items()):
        tag = "STRUCT" if kind in STRUCTURAL_KINDS else "info"
        print(f"  [{tag}] {kind}: {n}")

    show = findings[:30]
    for f in show:
        detail = f" ({f['detail']})" if "detail" in f else ""
        print(f"  {f['kind']:22s} {f['target']}{detail}")
    if len(findings) > 30:
        print(f"  …另有 {len(findings) - 30} 條")

    if not findings:
        return EXIT_OK

    structural = [f for f in findings if f["kind"] in STRUCTURAL_KINDS]
    if structural:
        print(
            f"\n結構性 {len(structural)} 條（缺表/欄/型別/可空/索引/UNIQUE/FK）"
            "—— 進 PLAN，本檢查不修。",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    # 僅 UNDECLARED_* / TZ_MISMATCH：仍算發現
    return EXIT_FINDINGS


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=("policy", "drift"), required=True)
    ap.add_argument("--dsn", help="PostgreSQL DSN（--mode drift 必填）")
    args = ap.parse_args()

    try:
        if args.mode == "policy":
            return run_policy()
        if not args.dsn:
            ap.error("--mode drift 需要 --dsn")
        return run_drift(args.dsn)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"BROKEN: {exc}", file=sys.stderr)
        return EXIT_BROKEN


if __name__ == "__main__":
    sys.exit(main())
