from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    ROOT
    / "services/csp/migrations/versions/"
    / "r1_0012_gate2_runtime_classification_invariants.py"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migration = _load("r1_0012_gate2_runtime_classification_invariants", MIGRATION_PATH)


class Gate2RuntimeClassificationInvariantTests(unittest.TestCase):
    def test_revision_follows_historical_reconciliation(self) -> None:
        self.assertEqual(migration.revision, "r1_0012")
        self.assertEqual(migration.down_revision, "r1_0011")

    def test_runtime_guards_lock_sources_and_cover_both_child_tables(self) -> None:
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("anila_gate2_enforce_document_floor", source)
        self.assertIn("anila_gate2_enforce_chunk_floor", source)
        self.assertGreaterEqual(source.count("FOR SHARE"), 3)
        self.assertIn("BEFORE INSERT ON ingestion_documents", source)
        self.assertIn("BEFORE INSERT ON document_chunks", source)
        self.assertIn(
            "BEFORE UPDATE OF classification_level, document_id, collection_id",
            source,
        )

    def test_parent_upgrades_only_raise_descendants(self) -> None:
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertIn("anila_gate2_cascade_collection_upgrade", source)
        self.assertIn("anila_gate2_cascade_document_upgrade", source)
        self.assertIn("document_upgrade_cascade", source)
        self.assertIn("collection_upgrade_cascade", source)
        self.assertIn(
            "anila_gate2_classification_rank(NEW.classification_level) >",
            source,
        )
        self.assertNotIn("classification_level = OLD.classification_level", source)

    def test_only_approved_parent_downgrades_pass_and_chunks_include_old_floor(self) -> None:
        source = MIGRATION_PATH.read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("declassification_approved"), 2)
        self.assertGreaterEqual(
            source.count(
                "NEW.classification_event_id IS DISTINCT FROM"
            ),
            2,
        )
        self.assertGreaterEqual(
            source.count(
                "NEW.classification_latched_at IS DISTINCT FROM"
            ),
            2,
        )
        self.assertGreaterEqual(
            source.count("NEW.classification_level := OLD.classification_level"),
            2,
        )
        self.assertIn(
            "anila_gate2_classification_rank(OLD.classification_level) >",
            source,
        )
        self.assertIn("effective_level := OLD.classification_level", source)

    def test_downgrade_removes_only_runtime_guards_not_classification_data(self) -> None:
        downgrade_source = MIGRATION_PATH.read_text(encoding="utf-8").split(
            "def downgrade() -> None:", maxsplit=1
        )[1]
        self.assertIn("DROP TRIGGER IF EXISTS", downgrade_source)
        self.assertIn("DROP FUNCTION IF EXISTS", downgrade_source)
        self.assertNotIn("UPDATE ingestion_", downgrade_source)
        self.assertNotIn("UPDATE document_chunks", downgrade_source)


if __name__ == "__main__":
    unittest.main()
