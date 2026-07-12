"""Static invariants for the PostgreSQL-only Gate 2 memory migration."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "services/csp/migrations/versions/r1_0016_gate2_memory_classification_governance.py"
SERVICE = ROOT / "services/csp/app/services/memory_service.py"
PROXY = ROOT / "services/csp/app/api/proxy.py"
CLEARANCE = ROOT / "services/csp/app/modules/clearance/service.py"


def test_memory_migration_chains_after_source_snapshot_sealing():
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "r1_0016"' in source
    assert 'down_revision: Union[str, None] = "r1_0015"' in source


def test_memory_migration_is_fail_closed_and_preserves_provenance():
    source = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "r1_0016:legacy_unknown_source",
        "'絕對機密'",
        "classification_level IS NULL",
        "classification_source IS NULL",
        "anila_gate2_memory_no_write_down",
        "anila_gate2_memory_source_floor",
        "memory source conversation owner mismatch",
        "user_fact_required_compartments",
        "memory_chunk_required_compartments",
        "user_fact_source_collections",
        "memory_chunk_source_collections",
    ):
        assert required in source


def test_memory_inference_has_no_direct_http_sink():
    source = SERVICE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "httpx" not in imports
    assert "enforce_model_ceiling(" in source
    assert "trusted_classification_level=classification_level" in source
    assert "finalize_task_run_on_completion=False" in source
    assert "proxy_request(" in source
    assert 'action="memory.model_inference"' in source


def test_memory_latch_does_not_hardcode_confidential():
    source = PROXY.read_text(encoding="utf-8")
    start = source.index("def _latch_inherited_classification")
    end = source.index("\ndef _propagate_conversation_level_to_task", start)
    function = source[start:end]
    assert "inherited_level.to_storage()" in function
    assert "ClassificationLevel.CONFIDENTIAL" not in function


def test_task_classification_propagation_is_fail_closed_on_both_paths():
    source = PROXY.read_text(encoding="utf-8")
    start = source.index("def _propagate_conversation_level_to_task_or_fail")
    end = source.index("\ndef _extract_assistant_text", start)
    function = source[start:end]
    assert "finalize_task_run_in_session(" in function
    assert '"task_classification_propagation"' in function
    assert "status_code=503" in function
    # Definition plus agent and direct-model call sites.
    assert source.count("_propagate_conversation_level_to_task_or_fail(") == 3


def test_all_chat_completion_exits_carry_governance_into_memory_writer():
    source = PROXY.read_text(encoding="utf-8")
    assert source.count("_schedule_memory_write(") == 5  # definition + four exits
    assert source.count("input_classification=(") == 4
    assert source.count("inherited_compartment_ids=(") == 4
    assert source.count("inherited_source_collection_ids=(") == 4
    assert source.count("task_id=task_ctx.task_id if task_ctx else None") >= 8


def test_runtime_decision_dependencies_are_share_locked_and_mutations_update_locked():
    service = SERVICE.read_text(encoding="utf-8")
    clearance = CLEARANCE.read_text(encoding="utf-8")
    # Grants, membership/NTK, associations, source rows, requirements,
    # consumer conversation, and memory rows all use the same DB transaction.
    assert service.count(".with_for_update(read=True)") >= 12
    assert "def _share_get" in service
    # Administrative revoke/assignment paths serialize against those reads.
    assert "def _for_update_get" in clearance
    assert ".with_for_update()" in clearance
