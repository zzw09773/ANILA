from __future__ import annotations

import importlib.util
import inspect
import os
import ast
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
CHECK_PATH = ROOT / "infra/policy/gate2/check_classification_reconciliation.py"
MIGRATION_PATH = (
    ROOT
    / "services/csp/migrations/versions/r1_0011_gate2_classification_reconciliation.py"
)
STORE_PATH = (
    ROOT
    / "packages/anila-core/src/anila_core/storage/adapters/pgvector_store.py"
)
HANDLER_PATH = (
    ROOT / "services/ingestion-worker/src/ingestion_worker/handlers.py"
)
DOCUMENTS_PATH = ROOT / "services/csp/app/api/ingestion/documents.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checker = _load("gate2_classification_reconciliation", CHECK_PATH)
migration = _load("r1_0011_gate2_classification_reconciliation", MIGRATION_PATH)


def _passing_report() -> dict:
    return {
        "manifest": [
            {"table": spec.table, "column": spec.column}
            for spec in checker.COLUMNS
        ],
        "snapshot": {
            "isolation": "repeatable read",
            "read_only": "on",
            "passed": True,
        },
        "schema": {"passed": True},
        "columns": [
            {
                "key": spec.key,
                "expected_rows": 1,
                "actual_rows": 1,
                "null_count": 0,
                "invalid_count": 0,
                "passed": True,
            }
            for spec in checker.COLUMNS
        ],
        "hierarchy": {
            "documents": {
                "expected_rows": 1,
                "actual_joined_rows": 1,
                "orphan_rows": 0,
                "below_collection": 0,
                "passed": True,
            },
            "chunks": {
                "expected_rows": 1,
                "actual_joined_rows": 1,
                "orphan_or_cross_collection_rows": 0,
                "below_document": 0,
                "below_collection": 0,
                "passed": True,
            },
            "callbacks": {
                "expected_linked_rows": 1,
                "actual_joined_rows": 1,
                "orphan_rows": 0,
                "below_launch": 0,
                "cross_service_rows": 0,
                "passed": True,
            },
        },
    }


class Gate2ClassificationReconciliationTests(unittest.TestCase):
    def test_checker_and_migration_use_the_same_complete_manifest(self) -> None:
        checker_columns = {(item.table, item.column) for item in checker.COLUMNS}
        migration_columns = set(migration._CLASSIFICATION_COLUMNS)
        self.assertEqual(checker_columns, migration_columns)
        self.assertEqual(len(checker_columns), len(checker.COLUMNS))
        self.assertIn(("document_chunks", "classification_level"), checker_columns)
        self.assertIn(("trace_spans", "classification_level"), checker_columns)
        self.assertIn(("artifacts", "classification_level"), checker_columns)
        self.assertIn(
            ("export_records", "target_classification_floor"), checker_columns
        )
        checker_required = {
            (item.table, item.column)
            for item in checker.REQUIRED_NOT_NULL_DEFAULTS
        }
        self.assertEqual(
            checker_required, set(migration._REQUIRED_NOT_NULL_DEFAULTS)
        )

    def test_manifest_matches_orm_metadata_and_pg_only_source_inventory(self) -> None:
        csp_dir = ROOT / "services/csp"
        core_src = ROOT / "packages/anila-core/src"
        contracts_src = ROOT / "packages/anila-contracts/src"
        security_src = ROOT / "packages/anila-security/src"
        for path in (csp_dir, core_src, contracts_src, security_src):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))

        import app.models  # noqa: F401
        from app.database import Base

        value_names = {
            "classification_level",
            "classification_ceiling",
            "default_classification_level",
            "target_classification_floor",
            "previous_level",
            "new_level",
            "from_level",
            "to_level",
        }
        discovered = {
            (table.name, column.name)
            for table in Base.metadata.sorted_tables
            for column in table.columns
            if column.name in value_names
        }

        # document_chunks is deliberately raw-SQL/PG-only, so prove its source
        # declaration separately instead of silently hardcoding it in the list.
        chunk_migration = (
            ROOT / "services/csp/migrations/versions/r1_0003_five_level_classification.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"document_chunks"', chunk_migration)
        self.assertIn('"classification_level",', chunk_migration)
        discovered.add(("document_chunks", "classification_level"))

        manifest = {(item.table, item.column) for item in checker.COLUMNS}
        self.assertEqual(discovered, manifest)

    def test_g1_writer_source_contract_is_present_before_g1b_can_pass(self) -> None:
        store_tree = ast.parse(STORE_PATH.read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(store_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in ("index_chunks", "add_parent_chunks"):
            self.assertIn(name, methods)
            kwonly = [arg.arg for arg in methods[name].args.kwonlyargs]
            self.assertIn("classification_level", kwonly)

        store_source = STORE_PATH.read_text(encoding="utf-8")
        handler_source = HANDLER_PATH.read_text(encoding="utf-8")
        documents_source = DOCUMENTS_PATH.read_text(encoding="utf-8")
        for token in (
            "classification_level",
            "classification_latched_at",
            "classification_source",
            "_resolve_write_classification",
        ):
            self.assertIn(token, store_source)
        for token in (
            "_effective_chunk_classification",
            "document_classification_level",
            "collection_classification_level",
            "classification_level=effective_classification",
        ):
            self.assertIn(token, handler_source)
        for token in (
            "_locked_collection_classification",
            ".with_for_update(read=True)",
            "classification_source=\"collection_inherited\"",
        ):
            self.assertIn(token, documents_source)

    def test_g1_writer_behavior_contracts_are_executed_by_owning_suites(self) -> None:
        core_tests = (
            ROOT / "packages/anila-core/tests/test_collection_scoped_pgvector_store.py"
        ).read_text(encoding="utf-8")
        worker_tests = (
            ROOT / "services/ingestion-worker/tests/test_handlers_helpers.py"
        ).read_text(encoding="utf-8")
        csp_tests = (
            ROOT / "services/csp/tests/test_ingestion_enqueue_rollback.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "test_parent_and_leaf_insert_sql_persists_classification_provenance",
            core_tests,
        )
        self.assertIn(
            "test_ingest_propagates_effective_classification_to_parent_and_leaf_writes",
            worker_tests,
        )
        self.assertIn(
            "test_single_upload_inherits_locked_collection_classification",
            csp_tests,
        )

    def test_report_requires_every_column_and_hierarchy_check_to_pass(self) -> None:
        report = _passing_report()
        self.assertTrue(checker.report_is_compliant(report))

        for section, key in (
            ("columns", 0),
            ("hierarchy", "documents"),
            ("hierarchy", "chunks"),
            ("hierarchy", "callbacks"),
        ):
            mutated = _passing_report()
            mutated[section][key]["passed"] = False
            self.assertFalse(checker.report_is_compliant(mutated))

        for section in ("schema", "snapshot"):
            mutated = _passing_report()
            mutated[section]["passed"] = False
            self.assertFalse(checker.report_is_compliant(mutated))

    def test_missing_or_malformed_report_fails_closed(self) -> None:
        self.assertFalse(checker.report_is_compliant({}))
        self.assertFalse(checker.report_is_compliant({"columns": [], "hierarchy": {}}))
        self.assertFalse(
            checker.report_is_compliant(
                {
                    "columns": _passing_report()["columns"],
                    "hierarchy": {},
                }
            )
        )

    def test_cli_requires_admin_database_url(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(checker.main([]), 2)

    def test_semantic_sample_never_selects_sensitive_payload_fields(self) -> None:
        source = inspect.getsource(checker._collect_semantic_sample).lower()
        for forbidden in ("filename", "content ", "payload", "username", "email"):
            self.assertNotIn(forbidden, source)

    def test_migration_is_monotonic_and_fail_closed(self) -> None:
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("_fail_on_invalid_values()", source)
        self.assertIn("_fail_on_broken_hierarchy()", source)
        self.assertIn("_lock_classification_tables()", source)
        self.assertIn("_fail_on_below_hierarchy()", source)
        self.assertIn("GREATEST(", source)
        self.assertIn("NOT VALID", source)
        self.assertNotIn("SET classification_level = '無機密' WHERE", source)

    def test_checker_uses_one_read_only_repeatable_read_snapshot(self) -> None:
        source = inspect.getsource(checker.collect_report)
        set_position = source.index(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        visibility_position = source.index("_require_full_visibility")
        self.assertLess(set_position, visibility_position)


if __name__ == "__main__":
    unittest.main()
