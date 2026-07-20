"""Static contracts for Gate 2 server-side retrieval and sealed evidence."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
WS_CHAT = ROOT / "apps/anilalm/src/workspace/WSChat.tsx"
CHAT_API = ROOT / "apps/anilalm/src/api/chat.ts"
PROXY = ROOT / "services/csp/app/api/proxy.py"
RETRIEVAL = ROOT / "services/csp/app/services/retrieval_service.py"
MIGRATION = (
    ROOT
    / "services/csp/migrations/versions/"
    / "r1_0015_gate2_source_snapshot_sealing.py"
)
PLATFORM = ROOT / "infra/compose/platform.yml"
DEPLOY = ROOT / "infra/deployment/scripts/deploy-prod.sh"


class Gate2RetrievalGovernanceTests(unittest.TestCase):
    def test_browser_declares_scope_but_never_builds_rag_prompt(self) -> None:
        source = WS_CHAT.read_text(encoding="utf-8")
        self.assertNotIn("searchCollection", source)
        self.assertNotIn("buildSystemPrompt", source)
        self.assertNotIn("RAG_CONTENT_LIMIT", source)
        self.assertIn("createQueryTask", source)
        self.assertIn("retrieval:", source)
        self.assertIn("taskId: task.taskId", source)

    def test_stream_requires_one_valid_server_evidence_event(self) -> None:
        source = CHAT_API.read_text(encoding="utf-8")
        self.assertIn("eventName === 'anila.retrieval'", source)
        self.assertIn("retrievalSeen", source)
        self.assertIn("正式 RAG 回應缺少", source)
        self.assertIn("task_id 與請求不一致", source)

    def test_server_boundary_owns_clearance_ranking_prompt_and_seal(self) -> None:
        source = RETRIEVAL.read_text(encoding="utf-8")
        for token in (
            "resolve_and_evaluate_data_access",
            "similarity_search_scoped_documents",
            "similarity_search_per_document_authorized",
            "classification_ceiling=ceiling",
            "_build_system_prompt",
            "_write_payload",
            "snapshot.content_hash",
            "Citation(",
            "db.flush()",
        ):
            self.assertIn(token, source)
        self.assertNotIn("await store.similarity_search(\n", source)

    def test_proxy_forbids_taskless_and_caller_system_prompt_fallbacks(self) -> None:
        source = PROXY.read_text(encoding="utf-8")
        self.assertIn("正式 RAG 必須先建立 Task", source)
        self.assertIn("system/developer prompt 只能由 CSP 產生", source)
        self.assertIn("ConfigDict(extra=\"forbid\")", source)
        self.assertIn("clearance_denied", source)

    def test_sealed_payload_uses_external_non_root_state(self) -> None:
        compose = PLATFORM.read_text(encoding="utf-8")
        deploy = DEPLOY.read_text(encoding="utf-8")
        self.assertIn(
            "SOURCE_SNAPSHOT_STORAGE_PATH: /var/lib/anila/source-snapshots",
            compose,
        )
        self.assertIn("${ANILA_STATE_DIR:?", compose)
        self.assertIn("source-snapshots:/var/lib/anila/source-snapshots", compose)
        self.assertIn(
            'prepare_csp_runtime_mount "$ANILA_STATE_DIR/source-snapshots" 700',
            deploy,
        )

    def test_database_seal_guards_are_stacked_after_auth(self) -> None:
        source = MIGRATION.read_text(encoding="utf-8")
        self.assertIn('revision: str = "r1_0015"', source)
        self.assertIn('down_revision: Union[str, None] = "r1_0014"', source)
        self.assertIn("sealed SourceSnapshot", source)
        self.assertIn("Citation requires a sealed SourceSnapshot", source)
        self.assertIn("persisted Citation rows are immutable", source)


if __name__ == "__main__":
    unittest.main()
