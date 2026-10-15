"""獨立審閱反例：只用合成 DB 與假模型／MCP，不啟動 HTTP client。"""

import asyncio
import json

import pytest

from app.models.agent import Agent, UserAgentPermission
from app.models.department import Department
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import (
    QuickAssistant,
    QuickCollectionPublication,
    QuickVersion,
)
from app.models.task import Task
from app.models.user import User, UserModelPermission
from app.services.quick_flow import validate_graph


def _graph(*middle):
    nodes = [{"id": "in", "kind": "input"}, *middle, {"id": "out", "kind": "output"}]
    return validate_graph(
        {
            "nodes": nodes,
            "edges": [
                {"source": nodes[i]["id"], "target": nodes[i + 1]["id"]}
                for i in range(len(nodes) - 1)
            ],
        }
    )


@pytest.fixture
def review_rows(db):
    department = Department(name="審閱合成單位")
    db.add(department)
    db.flush()
    owner = User(
        username="review_owner",
        hashed_password="synthetic-not-a-login",
        role="user",
        department_id=department.id,
        is_active=True,
        is_approved=True,
    )
    colleague = User(
        username="review_colleague",
        hashed_password="synthetic-not-a-login",
        role="user",
        department_id=department.id,
        is_active=True,
        is_approved=True,
    )
    admin = User(
        username="review_admin",
        hashed_password="synthetic-not-a-login",
        role="admin",
        department_id=department.id,
        is_active=True,
        is_approved=True,
    )
    model = ModelRegistry(
        name="review-synthetic-model",
        display_name="審閱合成模型",
        endpoint_url="http://model.invalid",
        is_active=True,
        model_type="llm",
        classification_ceiling="機密",
    )
    db.add_all([owner, colleague, admin, model])
    db.flush()
    db.add(UserModelPermission(user_id=owner.id, model_id=model.id))
    db.commit()
    return owner, colleague, admin, model, department


def _assistant(db, rows, *, published=True, graph=None, tool_fingerprints=None):
    owner, colleague, _, model, department = rows
    assistant = QuickAssistant(
        owner_user_id=owner.id,
        department_id=department.id,
        draft_revision=1,
        published_revision=1 if published else None,
    )
    agent = Agent(
        name="review-synthetic-agent",
        owner_user_id=owner.id,
        owner_department_id=department.id,
        base_model_id=model.id,
        endpoint_url="anila://quick-builder/review",
        runtime_type="custom_http",
        approval_status="approved" if published else "registered",
    )
    db.add_all([assistant, agent])
    db.flush()
    payload = {
        "name": "審閱助手",
        "description": "合成資料",
        "model_id": model.id,
        "classification_level": "無機密",
        "collection_ids": [],
        "scope": "unit",
        "target_department_ids": [],
        "tool_fingerprints": tool_fingerprints or {},
        "graph": graph or _graph({"id": "model", "kind": "model"}),
    }
    version = QuickVersion(
        assistant_id=assistant.id,
        revision=1,
        payload=payload,
        payload_hash="a" * 64,
        created_by=owner.id,
        agent_id=agent.id,
        status="published" if published else "registered",
    )
    db.add(version)
    if published:
        db.add(UserAgentPermission(user_id=colleague.id, agent_id=agent.id))
    db.commit()
    return assistant, version, agent


def test_review_json_trace_masks_sensitive_values_in_serialized_state():
    from app.services.quick_execution import mask

    secret = "synthetic-private-value"
    trace = {
        "tool": [{"result": {"password": secret}}],
        "output": {"text": json.dumps({"password": secret, "employee_id": "E000001"})},
        "input": {"text": "password=synthetic-private-value"},
    }
    masked = mask(trace)
    assert secret not in json.dumps(masked)
    assert "E000001" not in json.dumps(masked)


def test_review_unit_publication_does_not_share_personal_collection_with_unitless_users(
    db, review_rows
):
    from app.services.collection_scope import can_read_collection

    owner, colleague, admin, _, _ = review_rows
    owner.department_id = colleague.department_id = None
    collection = IngestionCollection(
        name="個人合成文件",
        created_by=owner.id,
        department_id=None,
        status="active",
        embedding_model="synthetic",
        embedding_dim=3,
        chunking_config={"strategy": "fixed"},
    )
    db.add(collection)
    db.flush()
    db.add(
        QuickCollectionPublication(
            collection_id=collection.id,
            scope="unit",
            target_department_ids=[],
            approved_by=admin.id,
        )
    )
    db.commit()
    assert can_read_collection(db, owner, collection)
    assert not can_read_collection(db, colleague, collection)


def test_review_user_agent_assignment_activates_registered_quick_version(
    db, review_rows
):
    """治理中心使用者指派也是既有 Agent 核准入口，須同步快製發布指標。"""
    from app.api.users import update_user_allowed_agents
    from app.schemas.user import UserAllowedAgentsUpdate

    _, colleague, admin, _, _ = review_rows
    assistant, version, agent = _assistant(db, review_rows, published=False)
    update_user_allowed_agents(
        colleague.id, UserAllowedAgentsUpdate(agent_ids=[agent.id]), admin, db
    )
    db.refresh(assistant)
    db.refresh(version)
    assert agent.approval_status == "approved"
    assert assistant.published_revision == 1
    assert version.status == "published"


def test_review_adopted_collection_rejects_parent_child_unit_write(db, review_rows):
    from app.services.collection_scope import can_read_collection, can_write_collection

    owner, _, admin, _, department = review_rows
    child = Department(name="合成下級單位", parent_id=department.id)
    db.add(child)
    db.flush()
    owner.department_id = child.id
    collection = IngestionCollection(
        name="精確單位文件",
        created_by=owner.id,
        department_id=department.id,
        status="active",
        embedding_model="synthetic",
        embedding_dim=3,
        chunking_config={},
    )
    db.add(collection)
    db.flush()
    db.add(
        QuickCollectionPublication(
            collection_id=collection.id,
            scope="unit",
            target_department_ids=[],
            approved_by=admin.id,
        )
    )
    db.commit()
    assert not can_read_collection(db, owner, collection)
    assert not can_write_collection(db, owner, collection)
    assert can_write_collection(db, admin, collection)


def test_review_task_classification_upgrade_during_model_call_blocks_document_export(
    db, review_rows, monkeypatch
):
    from app.services import internal_llm
    from app.services.quick_execution import run
    from app.services.proxy.task_link import TaskRunContext

    _, colleague, _, _, department = review_rows
    graph = _graph(
        {"id": "model", "kind": "model"},
        {"id": "document", "kind": "document", "config": {"template": "{{text}}"}},
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    task = Task(
        title="合成分類任務",
        task_type="draft",
        requester_user_id=colleague.id,
        department_id=department.id,
        classification_level="無機密",
    )
    db.add(task)
    db.commit()

    async def fake_model(*args, **kwargs):
        # 代表在模型等待期間已提交的分類升級；沒有發出模型請求。
        task.classification_level = "機密"
        db.commit()
        return "合成機密草稿"

    monkeypatch.setattr(internal_llm, "complete_chat", fake_model)
    context = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)
    result = asyncio.run(
        run(db, colleague, assistant.id, version.id, "合成事由", task_ctx=context)
    )
    assert result["status"] == "failed"
    assert not result["artifacts"]


def test_review_grant_revocation_during_model_call_stops_following_document(
    db, review_rows, monkeypatch
):
    from app.services import internal_llm
    from app.services.quick_execution import run

    _, colleague, _, _, _ = review_rows
    graph = _graph(
        {"id": "model", "kind": "model"},
        {"id": "document", "kind": "document", "config": {"template": "{{text}}"}},
    )
    assistant, version, agent = _assistant(db, review_rows, graph=graph)

    async def fake_model(*args, **kwargs):
        db.query(UserAgentPermission).filter_by(
            user_id=colleague.id, agent_id=agent.id
        ).delete()
        db.commit()
        return "合成草稿"

    monkeypatch.setattr(internal_llm, "complete_chat", fake_model)
    result = asyncio.run(run(db, colleague, assistant.id, version.id, "合成事由"))
    assert result["status"] == "failed"
    assert not result["artifacts"]


def test_review_classification_upgrade_after_document_creation_discards_artifact(
    db, review_rows, monkeypatch
):
    from app.services import internal_llm
    from app.services.quick_execution import run
    from app.services.proxy.task_link import TaskRunContext
    from app.models.quick_builder import QuickRun

    _, colleague, _, _, department = review_rows
    graph = _graph(
        {"id": "document", "kind": "document", "config": {"template": "{{text}}"}},
        {"id": "model", "kind": "model"},
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    task = Task(
        title="合成匯出後升密",
        task_type="draft",
        requester_user_id=colleague.id,
        department_id=department.id,
        classification_level="無機密",
    )
    db.add(task)
    db.commit()

    async def fake_model(*args, **kwargs):
        task.classification_level = "機密"
        db.commit()
        return "合成結果"

    monkeypatch.setattr(internal_llm, "complete_chat", fake_model)
    context = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)
    result = asyncio.run(
        run(db, colleague, assistant.id, version.id, "合成草稿", task_ctx=context)
    )
    assert result["status"] == "failed"
    assert result["artifacts"] == []
    assert db.get(QuickRun, result["id"]).result["artifacts"] == []


def test_review_download_rechecks_current_task_classification_and_hides_context(
    db, review_rows
):
    from fastapi import HTTPException
    from app.api.quick_builder import download_artifact, read_run
    from app.services.quick_execution import run
    from app.services.proxy.task_link import TaskRunContext

    _, colleague, _, _, department = review_rows
    graph = _graph(
        {"id": "document", "kind": "document", "config": {"template": "{{text}}"}}
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    task = Task(
        title="合成下載前升密",
        task_type="draft",
        requester_user_id=colleague.id,
        department_id=department.id,
        classification_level="無機密",
    )
    db.add(task)
    db.commit()
    context = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)
    result = asyncio.run(
        run(db, colleague, assistant.id, version.id, "合成草稿", task_ctx=context)
    )
    assert result["status"] == "completed"
    assert "execution_context" not in result
    assert "execution_context" not in read_run(result["id"], colleague, db)
    assert download_artifact(result["id"], 0, colleague, db).status_code == 200
    task.classification_level = "機密"
    db.commit()
    with pytest.raises(HTTPException) as denied:
        download_artifact(result["id"], 0, colleague, db)
    assert denied.value.status_code == 403
    assert denied.value.detail["type"] == "EXPORT_CLASSIFICATION"


def test_review_mcp_nested_iserror_is_not_a_successful_flow(
    db, review_rows, monkeypatch
):
    from app.services import quick_mcp
    from app.services.quick_execution import run

    _, colleague, _, _, _ = review_rows
    graph = _graph(
        {"id": "mcp", "kind": "mcp", "config": {"tool_id": 17, "arguments": {}}}
    )
    assistant, version, _ = _assistant(
        db, review_rows, graph=graph, tool_fingerprints={"17": "synthetic"}
    )
    monkeypatch.setattr(
        quick_mcp, "list_tools", lambda *_: [{"id": 17, "fingerprint": "synthetic"}]
    )

    async def failed_tool(*args, **kwargs):
        return {
            "tool_id": 17,
            "trust": "untrusted_external_data",
            "data": {
                "isError": True,
                "content": [{"type": "text", "text": "合成查詢失敗"}],
            },
        }

    monkeypatch.setattr(quick_mcp, "call_tool", failed_tool)
    result = asyncio.run(run(db, colleague, assistant.id, version.id, "合成查詢"))
    assert result["status"] == "failed"
    assert result["error"]["type"] == "TOOL_FAILED"


def test_review_api_key_attribution_and_zero_quota_reach_real_internal_model_gate(
    db, review_rows, monkeypatch
):
    """保留真正 complete_chat 與額度檢查，只替換網路傳輸及 Redis。"""
    from app.models.api_key import ApiKey
    from app.models.usage_quota import UsageQuota
    from app.services import quota_service
    from app.services.proxy import service as model_proxy
    from app.services.quota_redis import MemoryQuotaRedis
    from app.services.quick_execution import run

    _, colleague, _, _, _ = review_rows
    assistant, version, agent = _assistant(db, review_rows)
    key = ApiKey(
        user_id=colleague.id,
        name="合成額度金鑰",
        key_prefix="sk-test-",
        key_suffix="test",
        key_hash="synthetic-review-hash",
        is_active=True,
    )
    db.add(key)
    db.commit()
    calls = []

    async def fake_proxy(**kwargs):
        calls.append(kwargs)
        return {"choices": [{"message": {"content": "合成回答"}}]}

    memory_redis = MemoryQuotaRedis()
    monkeypatch.setattr(quota_service, "get_quota_redis", lambda: memory_redis)
    monkeypatch.setattr(
        quota_service, "notify_alert_opened", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(model_proxy, "proxy_request", fake_proxy)
    monkeypatch.setattr(model_proxy, "resolve_proxy_tuning", lambda _db: None)

    allowed = asyncio.run(
        run(db, colleague, assistant.id, version.id, "合成提問", api_key_id=key.id)
    )
    assert allowed["status"] == "completed"
    assert len(calls) == 1
    assert calls[0]["api_key_id"] == key.id
    assert calls[0]["user_id"] == colleague.id
    assert calls[0]["caller_agent_id"] == agent.id
    assert calls[0]["usage_kind"] == "inference"

    db.add(
        UsageQuota(
            scope_type="api_key",
            api_key_id=key.id,
            period="daily",
            metric="tokens",
            limit_value=0,
            on_limit="block",
        )
    )
    db.commit()
    blocked = asyncio.run(
        run(
            db, colleague, assistant.id, version.id, "不應送出的提問", api_key_id=key.id
        )
    )
    assert blocked["status"] == "failed"
    assert len(calls) == 1, "API 金鑰額度為零時不得再進模型傳輸"


def test_review_conversation_and_task_levels_both_apply_to_document(db, review_rows):
    from app.models.conversation import Conversation
    from app.services.quick_execution import run
    from app.services.proxy.task_link import TaskRunContext

    _, colleague, _, _, department = review_rows
    graph = _graph(
        {"id": "document", "kind": "document", "config": {"template": "{{text}}"}}
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    task = Task(
        title="合成低密任務",
        task_type="draft",
        requester_user_id=colleague.id,
        department_id=department.id,
        classification_level="無機密",
    )
    conversation = Conversation(
        user_id=colleague.id, title="合成高密對話", classification_level="機密"
    )
    db.add_all([task, conversation])
    db.commit()
    context = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)
    result = asyncio.run(
        run(
            db,
            colleague,
            assistant.id,
            version.id,
            "合成內容",
            task_ctx=context,
            conversation_id=conversation.id,
        )
    )
    assert result["status"] == "failed"
    assert result["artifacts"] == []


def test_review_studio_artifact_keeps_actual_requester_and_export_policy(
    db, review_rows, monkeypatch
):
    import hashlib
    from fastapi import HTTPException
    from app.api.quick_builder import download_artifact
    from app.models.artifact import Artifact, ExportRecord
    from app.models.quick_builder import QuickRun
    from app.services import quick_document
    from app.services.quick_execution import run

    _, colleague, _, _, _ = review_rows
    graph = _graph(
        {
            "id": "document",
            "kind": "document",
            "config": {
                "mode": "studio",
                "template": "主旨：{{text}}／簽核：承辦",
                "title": "合成文件",
                "preset": "key_summary",
                "format": "html",
            },
        }
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    content = b"<!doctype html><html><body>synthetic report</body></html>"

    async def resolve(*args, **kwargs):
        return "http://studio.synthetic"

    async def render(_db, current, **kwargs):
        assert current.id == colleague.id
        assert kwargs["preset"] == "key_summary"
        assert kwargs["text"] == "主旨：合成內容／簽核：承辦"
        return {
            "content": content,
            "filename": "assistant-draft.html",
            "media_type": "text/html",
            "content_hash": hashlib.sha256(content).hexdigest(),
            "job_id": "quick-synthetic-job",
            "preset": "key_summary",
            "format": "html",
            "title": "合成文件",
        }

    monkeypatch.setattr(quick_document, "resolve_studio_url", resolve)
    monkeypatch.setattr(quick_document, "render_document", render)
    result = asyncio.run(run(db, colleague, assistant.id, version.id, "合成內容"))
    assert result["status"] == "completed", result
    assert result["output"] == "主旨：合成內容／簽核：承辦"
    stored = db.get(QuickRun, result["id"]).result["artifacts"][0]
    artifact = db.get(Artifact, stored["artifact_id"])
    assert artifact.owner_user_id == colleague.id
    assert db.query(ExportRecord).filter_by(artifact_id=artifact.id).count() == 1
    response = download_artifact(result["id"], 0, colleague, db)
    assert response.body == content
    assert response.headers["content-disposition"].endswith(".html")
    assert db.query(ExportRecord).filter_by(artifact_id=artifact.id).count() == 2
    artifact.classification_level = "機密"
    db.commit()
    with pytest.raises(HTTPException) as denied:
        download_artifact(result["id"], 0, colleague, db)
    assert denied.value.status_code == 403


def test_review_studio_missing_registration_has_actionable_typed_error(db, review_rows):
    from app.services.quick_execution import run

    graph = _graph(
        {
            "id": "document",
            "kind": "document",
            "config": {
                "mode": "studio",
                "title": "合成文件",
                "preset": "key_summary",
                "format": "docx",
            },
        }
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    result = asyncio.run(run(db, review_rows[1], assistant.id, version.id, "合成內容"))
    assert result["status"] == "failed"
    assert result["error"]["type"] == "DOCUMENT_RENDERER_NOT_CONFIGURED"
    assert "治理中心" in result["error"]["message"]


@pytest.mark.parametrize("cancel", [False, True])
def test_review_later_failure_invalidates_already_registered_document(
    db, review_rows, monkeypatch, cancel
):
    import hashlib
    from app.models.artifact import Artifact, ArtifactVersion, ExportRecord
    from app.models.quick_builder import QuickRun
    from app.services import internal_llm, quick_document
    from app.services.quick_execution import run

    _, colleague, _, _, _ = review_rows
    graph = _graph(
        {
            "id": "document",
            "kind": "document",
            "config": {
                "mode": "studio",
                "title": "合成中間文件",
                "preset": "key_summary",
                "format": "html",
            },
        },
        {"id": "model", "kind": "model"},
    )
    assistant, version, _ = _assistant(db, review_rows, graph=graph)
    content = b"<html><body>synthetic intermediate</body></html>"

    async def resolve(*args, **kwargs):
        return "http://studio.synthetic"

    async def render(*args, **kwargs):
        return {
            "content": content,
            "filename": "assistant-draft.html",
            "media_type": "text/html",
            "content_hash": hashlib.sha256(content).hexdigest(),
            "job_id": "quick-synthetic-intermediate",
            "preset": "key_summary",
            "format": "html",
            "title": "合成中間文件",
        }

    async def fail_later(*args, **kwargs):
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic model failed")

    monkeypatch.setattr(quick_document, "resolve_studio_url", resolve)
    monkeypatch.setattr(quick_document, "render_document", render)
    monkeypatch.setattr(internal_llm, "complete_chat", fail_later)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run(db, colleague, assistant.id, version.id, "合成內容"))
    else:
        result = asyncio.run(run(db, colleague, assistant.id, version.id, "合成內容"))
        assert result["status"] == "failed"
    stored = db.query(QuickRun).one()
    assert stored.status == ("cancelled" if cancel else "failed")
    assert stored.result["artifacts"] == []
    artifact = db.query(Artifact).one()
    assert artifact.status == "failed"
    assert artifact.metadata_json["quick_run_status"] == stored.status
    artifact_version = db.query(ArtifactVersion).one()
    assert artifact_version.storage_ref is None
    assert artifact_version.file_refs == []
    assert db.query(ExportRecord).count() == 1, "保留已發生的匯出允許判定稽核"
