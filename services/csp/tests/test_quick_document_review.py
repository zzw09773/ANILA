"""文件委託、出站前撤權與成品生命週期的獨立回歸反例。"""
from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import pytest
from fastapi import HTTPException
from jose import jwt

from app.api.quick_builder import download_artifact
from app.models.artifact import Artifact, ArtifactVersion, ExportRecord
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.quick_builder import QuickRun
from app.models.registered_service import RegisteredService
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.modules import policy
from app.schemas.contracts.classification import ClassificationLevel
from app.services import internal_llm, quick_document as documents, quick_mcp
from app.services.auth_service import resolve_presented_user
from app.services.proxy.task_link import TaskRunContext
from app.services.quick_execution import run
from app.services.studio_job_token import verify_studio_job_token
from tests.conftest import make_user
from tests.test_quick_builder_review import (
    _assistant,
    _graph,
    review_rows as _review_rows_fixture,
)
from tests.test_quick_mcp import synthetic_keys as _synthetic_keys_fixture

review_rows = _review_rows_fixture
synthetic_keys = _synthetic_keys_fixture

STUDIO_URL = "https://studio.internal"
DOCUMENT_CONFIG = {
    "mode": "studio",
    "title": "合成草稿",
    "preset": "key_summary",
    "format": "html",
}
CONTENT = b"<!doctype html><html><body>synthetic report</body></html>"


def _rendered():
    return {
        "content": CONTENT,
        "filename": "assistant-draft.html",
        "media_type": "text/html",
        "content_hash": hashlib.sha256(CONTENT).hexdigest(),
        "job_id": "quick-review-synthetic",
        "preset": "key_summary",
        "format": "html",
        "title": "合成草稿",
    }


def test_render_only_token_cannot_authenticate_general_csp(db, synthetic_keys):
    user = make_user(db, "synthetic-render-scope")
    payload = {
        "job_id": "quick-review",
        "preset": "key_summary",
        "format": "html",
        "title": "合成草稿",
        "text": "合成內容",
    }
    token = documents._render_token(db, user, payload, ClassificationLevel.UNCLASSIFIED)
    claims = jwt.get_unverified_claims(token)
    assert claims["type"] == "studio_quick_document"
    assert claims["aud"] == "studio-quick-document"
    assert claims["purpose"] == "quick_document"
    assert verify_studio_job_token(token, db) is None
    with pytest.raises(HTTPException) as denied:
        resolve_presented_user(token, db)
    assert denied.value.status_code == 401


@pytest.mark.parametrize("change", ["task", "conversation", "agent", "snapshot"])
def test_inner_studio_preflight_rechecks_live_context_before_post(
    db, review_rows, synthetic_keys, monkeypatch, change
):
    _, user, _, _, department = review_rows
    assistant, version, agent = _assistant(
        db,
        review_rows,
        graph=_graph({"id": "document", "kind": "document", "config": DOCUMENT_CONFIG}),
    )
    task = Task(
        title="合成任務",
        task_type="draft",
        requester_user_id=user.id,
        department_id=department.id,
        classification_level="無機密",
    )
    conversation = Conversation(user_id=user.id, classification_level="無機密")
    studio = RegisteredService(
        slug="studio",
        name="合成 Studio",
        entry_url=STUDIO_URL,
        is_active=True,
        is_public=True,
        required_roles=[],
        classification_ceiling="營業秘密",
    )
    db.add_all([task, conversation, studio])
    db.flush()
    snapshot = SourceSnapshot(
        task_id=task.id, origin="none", source_scope="none", classification_level="無機密"
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    db.commit()
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", lambda url: None)
    original_resolve = documents.resolve_studio_url
    resolves = 0

    async def resolve(*args, **kwargs):
        nonlocal resolves
        endpoint = await original_resolve(*args, **kwargs)
        resolves += 1
        if resolves == 2:
            # 第一次為流程預檢，第二次為 adapter 內最後的 DNS await。
            if change == "task":
                task.classification_level = "機密"
            elif change == "conversation":
                conversation.classification_level = "機密"
            elif change == "snapshot":
                snapshot.classification_level = "機密"
            else:
                agent.approval_status = "registered"
            db.commit()
        return endpoint

    monkeypatch.setattr(documents, "resolve_studio_url", resolve)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        documents.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = asyncio.run(
        run(
            db,
            user,
            assistant.id,
            version.id,
            "合成草稿",
            task_ctx=TaskRunContext(
                task_id=task.id, trace_id=task.trace_id, task_run_id=1
            ),
            conversation_id=conversation.id,
        )
    )
    assert result["status"] == "failed"
    assert resolves == 2
    assert calls == []
    assert result["artifacts"] == []


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_later_failure_invalidates_registered_studio_artifact(
    db, review_rows, monkeypatch, terminal
):
    _, user, _, _, _ = review_rows
    graph = _graph(
        {"id": "document", "kind": "document", "config": DOCUMENT_CONFIG},
        {"id": "model", "kind": "model"},
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)

    async def resolve(*args, **kwargs):
        return STUDIO_URL

    async def render(*args, **kwargs):
        return _rendered()

    async def stop_model(*args, **kwargs):
        if terminal == "cancelled":
            raise asyncio.CancelledError
        raise RuntimeError("synthetic model unavailable")

    monkeypatch.setattr(documents, "resolve_studio_url", resolve)
    monkeypatch.setattr(documents, "render_document", render)
    monkeypatch.setattr(internal_llm, "complete_chat", stop_model)
    execution = run(db, user, assistant.id, version.id, "合成草稿")
    if terminal == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(execution)
    else:
        assert asyncio.run(execution)["status"] == "failed"
    stored = db.query(QuickRun).one()
    artifact = db.query(Artifact).one()
    artifact_version = db.query(ArtifactVersion).one()
    assert stored.status == terminal and stored.result["artifacts"] == []
    assert artifact.owner_user_id == user.id and artifact.status == "failed"
    assert artifact.metadata_json["quick_run_status"] == terminal
    assert artifact_version.storage_ref is None and artifact_version.file_refs == []
    assert artifact_version.content_hash == hashlib.sha256(CONTENT).hexdigest()
    assert db.query(ExportRecord).filter_by(artifact_id=artifact.id).count() == 1
    assert (
        db.query(AuditLog).filter_by(action="quick_builder.artifact_invalidate").count()
        == 1
    )
    with pytest.raises(HTTPException) as denied:
        download_artifact(stored.id, 0, user, db)
    assert denied.value.status_code == 404


def test_bound_snapshot_upgrade_blocks_document_download(db, review_rows):
    _, user, _, _, department = review_rows
    _, version, _ = _assistant(db, review_rows)
    task = Task(
        title="合成快照任務",
        task_type="draft",
        requester_user_id=user.id,
        department_id=department.id,
        classification_level="無機密",
    )
    db.add(task)
    db.flush()
    snapshot = SourceSnapshot(
        task_id=task.id, origin="none", source_scope="none", classification_level="無機密"
    )
    stored = QuickRun(
        version_id=version.id, user_id=user.id, status="completed", result={}
    )
    db.add_all([snapshot, stored])
    db.commit()
    metadata = documents.register_document_artifact(
        db,
        user,
        run_id=stored.id,
        rendered=_rendered(),
        classification_level="無機密",
        task_id=task.id,
        source_snapshot_id=snapshot.id,
    )
    stored.result = {
        "artifacts": [
            {
                **metadata,
                "content": base64.b64encode(CONTENT).decode("ascii"),
                "content_hash": hashlib.sha256(CONTENT).hexdigest(),
                "encoding": "base64",
                "media_type": "text/html",
                "filename": "assistant-draft.html",
                "format": "html",
            }
        ],
        "execution_context": {"task_id": task.id, "classification_level": "無機密"},
    }
    db.commit()
    assert download_artifact(stored.id, 0, user, db).body == CONTENT
    policy.apply_classification(
        db,
        resource_type="source_snapshot",
        resource_id=str(snapshot.id),
        new_level="機密",
        actor_type="user",
        actor_id=str(user.id),
        reason="source_selected",
        task_id=task.id,
        source="synthetic_review",
    )
    with pytest.raises(HTTPException) as denied:
        download_artifact(stored.id, 0, user, db)
    assert denied.value.status_code == 403
    assert denied.value.detail["type"] == "EXPORT_CLASSIFICATION"
