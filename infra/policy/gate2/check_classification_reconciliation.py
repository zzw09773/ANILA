#!/usr/bin/env python3
"""Prove full classification coverage without reading classified content.

The checker must run with ``MIGRATION_DATABASE_URL`` (or ``--database-url``)
using a PostgreSQL role that bypasses RLS. It emits only aggregate counts and
numeric row identifiers/levels for semantic sampling; filenames, content, and
user identities are never selected.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection


LEVELS = ("無機密", "營業秘密", "機密", "極機密", "絕對機密")
LEVEL_SQL = ", ".join(f"'{level}'" for level in LEVELS)
EXPECTED_ALEMBIC_REVISION = "r1_0011"
ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ColumnSpec:
    table: str
    column: str

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


COLUMNS: tuple[ColumnSpec, ...] = tuple(
    ColumnSpec(*item)
    for item in (
        ("tasks", "classification_level"),
        ("task_runs", "classification_level"),
        ("trace_spans", "classification_level"),
        ("policy_decisions", "classification_level"),
        ("source_snapshots", "classification_level"),
        ("citations", "classification_level"),
        ("conversations", "classification_level"),
        ("messages", "classification_level"),
        ("ingestion_collections", "classification_level"),
        ("ingestion_documents", "classification_level"),
        ("document_chunks", "classification_level"),
        ("agents", "default_classification_level"),
        ("agents", "classification_ceiling"),
        ("model_registry", "classification_ceiling"),
        ("registered_services", "classification_ceiling"),
        ("service_launches", "classification_level"),
        ("service_audit_callbacks", "classification_level"),
        ("artifacts", "classification_level"),
        ("artifact_versions", "classification_level"),
        ("export_records", "target_classification_floor"),
        ("export_records", "classification_level"),
        ("classification_events", "previous_level"),
        ("classification_events", "new_level"),
        ("declassification_requests", "from_level"),
        ("declassification_requests", "to_level"),
    )
)

REQUIRED_NOT_NULL_DEFAULTS: tuple[ColumnSpec, ...] = tuple(
    ColumnSpec(*item)
    for item in (
        ("agents", "classification_ceiling"),
        ("model_registry", "classification_ceiling"),
        ("registered_services", "classification_ceiling"),
        ("service_audit_callbacks", "classification_level"),
        ("export_records", "target_classification_floor"),
    )
)


class ReconciliationError(RuntimeError):
    """The checker cannot produce trustworthy full-database evidence."""


def _rank(expression: str) -> str:
    return (
        f"CASE {expression} "
        "WHEN '無機密' THEN 0 "
        "WHEN '營業秘密' THEN 1 "
        "WHEN '機密' THEN 2 "
        "WHEN '極機密' THEN 3 "
        "WHEN '絕對機密' THEN 4 END"
    )


def _constraint_name(spec: ColumnSpec) -> str:
    return f"ck_{spec.table}_{spec.column}_gate2_level"


def _scalar(conn: Connection, statement: str) -> int:
    value = conn.execute(text(statement)).scalar_one()
    return int(value)


def _require_full_visibility(conn: Connection) -> dict[str, str]:
    if conn.dialect.name != "postgresql":
        raise ReconciliationError("classification reconciliation requires PostgreSQL")
    row = conn.execute(
        text(
            """
            SELECT current_database() AS database_name,
                   current_user AS role_name,
                   (rolsuper OR rolbypassrls) AS can_bypass_rls
              FROM pg_roles
             WHERE rolname = current_user
            """
        )
    ).mappings().one()
    if not row["can_bypass_rls"]:
        raise ReconciliationError(
            "classification reconciliation role must be superuser or BYPASSRLS"
        )
    return {
        "database_name": str(row["database_name"]),
        "role_name": str(row["role_name"]),
    }


def _collect_schema(conn: Connection) -> dict[str, Any]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    alembic_config = Config(str(ROOT / "services/csp/alembic.ini"))
    alembic_config.set_main_option(
        "script_location", str(ROOT / "services/csp/migrations")
    )
    script = ScriptDirectory.from_config(alembic_config)
    source_heads = list(script.get_heads())
    current_revisions = [
        str(row[0])
        for row in conn.execute(
            text("SELECT version_num FROM alembic_version ORDER BY version_num")
        ).all()
    ]
    reconciliation_in_head_history = False
    if len(source_heads) == 1:
        reconciliation_in_head_history = any(
            revision.revision == EXPECTED_ALEMBIC_REVISION
            for revision in script.walk_revisions(
                base="base", head=source_heads[0]
            )
        )
    alembic = {
        "required_reconciliation_revision": EXPECTED_ALEMBIC_REVISION,
        "source_heads": source_heads,
        "current_revisions": current_revisions,
        "reconciliation_in_head_history": reconciliation_in_head_history,
        "passed": (
            len(source_heads) == 1
            and current_revisions == source_heads
            and reconciliation_in_head_history
        ),
    }

    constraints: list[dict[str, Any]] = []
    for spec in COLUMNS:
        name = _constraint_name(spec)
        row = conn.execute(
            text(
                """
                SELECT c.convalidated,
                       pg_get_constraintdef(c.oid, true) AS definition
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                 WHERE t.relname = :table
                   AND c.conname = :name
                   AND c.contype = 'c'
                   AND pg_table_is_visible(t.oid)
                """
            ),
            {"table": spec.table, "name": name},
        ).mappings().first()
        definition = str(row["definition"]) if row is not None else ""
        definition_valid = spec.column in definition and all(
            level in definition for level in LEVELS
        )
        item = {
            "key": spec.key,
            "constraint": name,
            "exists": row is not None,
            "validated": bool(row["convalidated"]) if row is not None else False,
            "definition_valid": definition_valid,
        }
        item["passed"] = (
            item["exists"] and item["validated"] and item["definition_valid"]
        )
        constraints.append(item)

    required_columns: list[dict[str, Any]] = []
    for spec in REQUIRED_NOT_NULL_DEFAULTS:
        row = conn.execute(
            text(
                """
                SELECT a.attnotnull,
                       pg_get_expr(ad.adbin, ad.adrelid) AS default_expression
                  FROM pg_attribute a
                  JOIN pg_class t ON t.oid = a.attrelid
                  LEFT JOIN pg_attrdef ad
                    ON ad.adrelid = a.attrelid
                   AND ad.adnum = a.attnum
                 WHERE t.relname = :table
                   AND a.attname = :column
                   AND a.attnum > 0
                   AND NOT a.attisdropped
                   AND pg_table_is_visible(t.oid)
                """
            ),
            {"table": spec.table, "column": spec.column},
        ).mappings().first()
        default_expression = (
            str(row["default_expression"] or "") if row is not None else ""
        )
        item = {
            "key": spec.key,
            "exists": row is not None,
            "not_null": bool(row["attnotnull"]) if row is not None else False,
            "default_is_unclassified": "無機密" in default_expression,
        }
        item["passed"] = (
            item["exists"]
            and item["not_null"]
            and item["default_is_unclassified"]
        )
        required_columns.append(item)

    result = {
        "alembic": alembic,
        "constraints": constraints,
        "required_columns": required_columns,
    }
    result["passed"] = (
        alembic["passed"]
        and all(item["passed"] for item in constraints)
        and all(item["passed"] for item in required_columns)
    )
    return result


def _collect_columns(conn: Connection) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in COLUMNS:
        row = conn.execute(
            text(
                f"""
                SELECT COUNT(*) AS expected_rows,
                       COUNT(*) FILTER (
                           WHERE {spec.column} IN ({LEVEL_SQL})
                       ) AS actual_rows,
                       COUNT(*) FILTER (
                           WHERE {spec.column} IS NULL
                       ) AS null_count,
                       COUNT(*) FILTER (
                           WHERE {spec.column} IS NOT NULL
                             AND {spec.column} NOT IN ({LEVEL_SQL})
                       ) AS invalid_count
                  FROM {spec.table}
                """
            )
        ).mappings().one()
        item = {"key": spec.key, **{key: int(value) for key, value in row.items()}}
        item["passed"] = (
            item["expected_rows"] == item["actual_rows"]
            and item["null_count"] == 0
            and item["invalid_count"] == 0
        )
        results.append(item)
    return results


def _collect_hierarchy(conn: Connection) -> dict[str, dict[str, int | bool]]:
    document_expected = _scalar(conn, "SELECT COUNT(*) FROM ingestion_documents")
    document_joined = _scalar(
        conn,
        """
        SELECT COUNT(*)
          FROM ingestion_documents d
          JOIN ingestion_collections c ON c.id = d.collection_id
        """,
    )
    documents_below = _scalar(
        conn,
        f"""
        SELECT COUNT(*)
          FROM ingestion_documents d
          JOIN ingestion_collections c ON c.id = d.collection_id
         WHERE {_rank('d.classification_level')} < {_rank('c.classification_level')}
        """,
    )

    chunk_expected = _scalar(conn, "SELECT COUNT(*) FROM document_chunks")
    chunk_joined = _scalar(
        conn,
        """
        SELECT COUNT(*)
          FROM document_chunks ch
          JOIN ingestion_documents d
            ON d.id = ch.document_id
           AND d.collection_id = ch.collection_id
          JOIN ingestion_collections c ON c.id = ch.collection_id
        """,
    )
    chunks_below_document = _scalar(
        conn,
        f"""
        SELECT COUNT(*)
          FROM document_chunks ch
          JOIN ingestion_documents d
            ON d.id = ch.document_id
           AND d.collection_id = ch.collection_id
         WHERE {_rank('ch.classification_level')} <
               {_rank('d.classification_level')}
        """,
    )
    chunks_below_collection = _scalar(
        conn,
        f"""
        SELECT COUNT(*)
          FROM document_chunks ch
          JOIN ingestion_collections c ON c.id = ch.collection_id
         WHERE {_rank('ch.classification_level')} <
               {_rank('c.classification_level')}
        """,
    )

    documents: dict[str, int | bool] = {
        "expected_rows": document_expected,
        "actual_joined_rows": document_joined,
        "orphan_rows": document_expected - document_joined,
        "below_collection": documents_below,
    }
    documents["passed"] = (
        documents["expected_rows"] == documents["actual_joined_rows"]
        and documents["orphan_rows"] == 0
        and documents["below_collection"] == 0
    )

    chunks: dict[str, int | bool] = {
        "expected_rows": chunk_expected,
        "actual_joined_rows": chunk_joined,
        "orphan_or_cross_collection_rows": chunk_expected - chunk_joined,
        "below_document": chunks_below_document,
        "below_collection": chunks_below_collection,
    }
    chunks["passed"] = (
        chunks["expected_rows"] == chunks["actual_joined_rows"]
        and chunks["orphan_or_cross_collection_rows"] == 0
        and chunks["below_document"] == 0
        and chunks["below_collection"] == 0
    )

    callback_expected = _scalar(
        conn,
        "SELECT COUNT(*) FROM service_audit_callbacks WHERE launch_id IS NOT NULL",
    )
    callback_joined = _scalar(
        conn,
        """
        SELECT COUNT(*)
          FROM service_audit_callbacks cb
          JOIN service_launches sl ON sl.launch_id = cb.launch_id
         WHERE cb.launch_id IS NOT NULL
        """,
    )
    callbacks_below_launch = _scalar(
        conn,
        f"""
        SELECT COUNT(*)
          FROM service_audit_callbacks cb
          JOIN service_launches sl ON sl.launch_id = cb.launch_id
         WHERE {_rank('cb.classification_level')} <
               {_rank('sl.classification_level')}
        """,
    )
    callbacks_cross_service = _scalar(
        conn,
        """
        SELECT COUNT(*)
          FROM service_audit_callbacks cb
          JOIN service_launches sl ON sl.launch_id = cb.launch_id
         WHERE cb.service_id IS DISTINCT FROM sl.service_id
        """,
    )
    callbacks: dict[str, int | bool] = {
        "expected_linked_rows": callback_expected,
        "actual_joined_rows": callback_joined,
        "orphan_rows": callback_expected - callback_joined,
        "below_launch": callbacks_below_launch,
        "cross_service_rows": callbacks_cross_service,
    }
    callbacks["passed"] = (
        callbacks["expected_linked_rows"] == callbacks["actual_joined_rows"]
        and callbacks["orphan_rows"] == 0
        and callbacks["below_launch"] == 0
        and callbacks["cross_service_rows"] == 0
    )
    return {"documents": documents, "chunks": chunks, "callbacks": callbacks}


def _rows_as_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _collect_semantic_sample(conn: Connection, sample_size: int) -> dict[str, Any]:
    documents = conn.execute(
        text(
            """
            SELECT d.id AS document_id,
                   d.collection_id,
                   c.classification_level AS collection_level,
                   d.classification_level AS document_level
              FROM ingestion_documents d
              JOIN ingestion_collections c ON c.id = d.collection_id
             ORDER BY d.id
             LIMIT :limit
            """
        ),
        {"limit": sample_size},
    ).mappings()
    chunks = conn.execute(
        text(
            """
            SELECT ch.id AS chunk_id,
                   ch.document_id,
                   ch.collection_id,
                   c.classification_level AS collection_level,
                   d.classification_level AS document_level,
                   ch.classification_level AS chunk_level
              FROM document_chunks ch
              JOIN ingestion_documents d
                ON d.id = ch.document_id
               AND d.collection_id = ch.collection_id
              JOIN ingestion_collections c ON c.id = ch.collection_id
             ORDER BY ch.id
             LIMIT :limit
            """
        ),
        {"limit": sample_size},
    ).mappings()
    return {
        "purpose": "semantic review only; full completeness is proven by counts",
        "documents": _rows_as_dicts(documents),
        "chunks": _rows_as_dicts(chunks),
    }


def report_is_compliant(report: dict[str, Any]) -> bool:
    columns = report.get("columns")
    hierarchy = report.get("hierarchy")
    schema = report.get("schema")
    snapshot = report.get("snapshot")
    manifest = report.get("manifest")
    if not all(
        (
            isinstance(columns, list),
            isinstance(hierarchy, dict),
            isinstance(schema, dict),
            isinstance(snapshot, dict),
            isinstance(manifest, list),
        )
    ):
        return False
    expected_keys = {spec.key for spec in COLUMNS}
    observed_keys = {
        item.get("key") for item in columns if isinstance(item, dict)
    }
    if len(columns) != len(COLUMNS) or observed_keys != expected_keys:
        return False
    manifest_keys = {
        f"{item.get('table')}.{item.get('column')}"
        for item in manifest
        if isinstance(item, dict)
    }
    if len(manifest) != len(COLUMNS) or manifest_keys != expected_keys:
        return False
    if set(hierarchy) != {"documents", "chunks", "callbacks"}:
        return False
    return (
        schema.get("passed") is True
        and snapshot.get("passed") is True
        and all(item.get("passed") is True for item in columns)
        and all(
            hierarchy[name].get("passed") is True
            for name in ("documents", "chunks", "callbacks")
        )
    )


def collect_report(database_url: str, sample_size: int = 20) -> dict[str, Any]:
    if not database_url.startswith(("postgresql://", "postgresql+psycopg2://")):
        raise ReconciliationError("MIGRATION_DATABASE_URL must be PostgreSQL")
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            with conn.begin():
                # This must be the first transaction statement. Every count,
                # schema assertion, and semantic sample then observes one
                # immutable database snapshot.
                conn.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                )
                identity = _require_full_visibility(conn)
                isolation = str(
                    conn.execute(text("SHOW transaction_isolation")).scalar_one()
                )
                read_only = str(
                    conn.execute(text("SHOW transaction_read_only")).scalar_one()
                )
                snapshot = {
                    "isolation": isolation,
                    "read_only": read_only,
                    "passed": isolation == "repeatable read" and read_only == "on",
                }
                report: dict[str, Any] = {
                    "schema_version": 2,
                    "policy_id": "gate2-g1b-classification-reconciliation",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "database": identity,
                    "snapshot": snapshot,
                    "manifest": [asdict(spec) for spec in COLUMNS],
                    "schema": _collect_schema(conn),
                    "columns": _collect_columns(conn),
                    "hierarchy": _collect_hierarchy(conn),
                    "semantic_sample": _collect_semantic_sample(conn, sample_size),
                }
                report["passed"] = report_is_compliant(report)
                return report
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL admin URL; defaults to MIGRATION_DATABASE_URL",
    )
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0 <= args.sample_size <= 100:
        print("[FAIL] --sample-size must be between 0 and 100", file=sys.stderr)
        return 2
    database_url = args.database_url or os.environ.get("MIGRATION_DATABASE_URL", "")
    if not database_url:
        print("[FAIL] MIGRATION_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        report = collect_report(database_url, args.sample_size)
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(rendered)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
