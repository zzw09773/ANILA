"""正式快製執行的來源快照與多輪文字上下文回歸。"""

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.api.quick_builder import download_artifact
from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.quick_builder import QuickRun
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.services import internal_llm, quick_document as documents
from app.services.proxy.task_link import TaskRunContext
from app.services.quick_execution import agent_response, run
from tests.test_quick_builder_review import (
    _assistant,
    _graph,
    review_rows as _review_rows,
)
from tests.test_quick_document_review import DOCUMENT_CONFIG, _rendered
from tests.test_quick_builder import people as people, http as http, login

review_rows = _review_rows


def _task_snapshot(db, user, department):
    task = Task(
        title="合成來源任務",
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
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    db.commit()
    context = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)
    return task, snapshot, context


@pytest.mark.parametrize("mode", ["text", "studio"])
def test_runtime_derives_snapshot_and_download_rechecks_upgrade(
    db, review_rows, monkeypatch, mode
):
    _, user, _, _, department = review_rows
    config = DOCUMENT_CONFIG if mode == "studio" else {"template": "{{text}}"}
    assistant, version, _ = _assistant(
        db,
        review_rows,
        graph=_graph({"id": "document", "kind": "document", "config": config}),
    )
    task, snapshot, context = _task_snapshot(db, user, department)

    async def resolve(*args, **kwargs):
        return "https://studio.internal"

    async def render(*args, **kwargs):
        kwargs["before_send"]()
        return _rendered()

    monkeypatch.setattr(documents, "resolve_studio_url", resolve)
    monkeypatch.setattr(documents, "render_document", render)
    result = asyncio.run(
        run(db, user, assistant.id, version.id, "合成草稿", task_ctx=context)
    )
    assert result["status"] == "completed"
    stored = db.get(QuickRun, result["id"])
    assert stored.result["execution_context"]["source_snapshot_id"] == snapshot.id
    assert "execution_context" not in result
    if mode == "studio":
        registered = db.query(Artifact).one()
        assert registered.source_task_id == task.id
        assert registered.source_snapshot_id == snapshot.id
    assert download_artifact(result["id"], 0, user, db).status_code == 200
    snapshot.classification_level = "機密"
    db.commit()
    with pytest.raises(HTTPException) as denied:
        download_artifact(result["id"], 0, user, db)
    assert denied.value.detail["type"] == "EXPORT_CLASSIFICATION"


@pytest.mark.parametrize("mode", ["text", "studio"])
def test_snapshot_upgrade_during_model_blocks_document(
    db, review_rows, monkeypatch, mode
):
    _, user, _, _, department = review_rows
    config = DOCUMENT_CONFIG if mode == "studio" else {"template": "{{text}}"}
    assistant, version, _ = _assistant(
        db,
        review_rows,
        graph=_graph(
            {"id": "model", "kind": "model"},
            {"id": "document", "kind": "document", "config": config},
        ),
    )
    _, snapshot, context = _task_snapshot(db, user, department)

    async def model(*args, **kwargs):
        snapshot.classification_level = "機密"
        db.commit()
        return "合成結果"

    async def forbidden_render(*args, **kwargs):
        pytest.fail("已升密的來源不能送到 Studio")

    monkeypatch.setattr(internal_llm, "complete_chat", model)
    monkeypatch.setattr(documents, "render_document", forbidden_render)
    result = asyncio.run(
        run(db, user, assistant.id, version.id, "合成輸入", task_ctx=context)
    )
    assert result["status"] == "failed" and result["artifacts"] == []
    assert db.query(Artifact).count() == 0


def test_task_snapshot_pointer_change_stops_runtime(db, review_rows, monkeypatch):
    _, user, _, _, department = review_rows
    assistant, version, _ = _assistant(db, review_rows)
    task, _, context = _task_snapshot(db, user, department)

    async def model(*args, **kwargs):
        task.source_snapshot_id = None
        db.commit()
        return "合成結果"

    monkeypatch.setattr(internal_llm, "complete_chat", model)
    result = asyncio.run(
        run(db, user, assistant.id, version.id, "合成輸入", task_ctx=context)
    )
    assert result["status"] == "failed"


def test_foreign_snapshot_cannot_be_attached_to_requester_task(db, review_rows):
    owner, user, _, _, department = review_rows
    assistant, version, _ = _assistant(db, review_rows)
    foreign_task, snapshot, _ = _task_snapshot(db, owner, department)
    task, _, context = _task_snapshot(db, user, department)
    task.source_snapshot_id = snapshot.id
    db.commit()
    assert foreign_task.id != task.id
    with pytest.raises(HTTPException) as denied:
        asyncio.run(run(db, user, assistant.id, version.id, "合成輸入", task_ctx=context))
    assert denied.value.detail["type"] == "CONTEXT_DENIED"


def test_chat_keeps_prior_turns_and_ignores_client_policy_roles(
    db, review_rows, monkeypatch
):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    conversation = Conversation(user_id=user.id, classification_level="無機密")
    db.add(conversation)
    db.commit()
    calls = []

    async def model(db, model, body, **kwargs):
        calls.append(body["messages"])
        return "合成表格"

    monkeypatch.setattr(internal_llm, "complete_chat", model)
    response = asyncio.run(
        agent_response(
            db,
            user,
            agent,
            {
                "messages": [
                    {"role": "system", "content": "CLIENT_POLICY_SENTINEL"},
                    {"role": "user", "content": "第一段合成工作事項"},
                    {"role": "assistant", "content": "合成前一段回覆"},
                    {"role": "assistant", "content": ""},
                    {"role": "developer", "content": "CLIENT_DEVELOPER_SENTINEL"},
                    {"role": "user", "content": "把上一段改成表格"},
                ]
            },
            conversation_id=conversation.id,
        )
    )
    assert response.status_code == 200
    wire = json.dumps(calls, ensure_ascii=False)
    assert "第一段合成工作事項" in wire and "合成前一段回覆" in wire
    assert "把上一段改成表格" in wire
    assert "CLIENT_POLICY_SENTINEL" not in wire
    assert "CLIENT_DEVELOPER_SENTINEL" not in wire
    assert calls[0][0]["role"] == "system"


def test_chat_history_window_is_bounded(db, review_rows, monkeypatch):
    _, user, _, _, _ = review_rows
    _, _, agent = _assistant(db, review_rows)
    calls = []

    async def model(db, model, body, **kwargs):
        calls.append(body["messages"])
        return "合成結果"

    monkeypatch.setattr(internal_llm, "complete_chat", model)
    messages = [
        {"role": "assistant", "content": f"older-{i}:" + "文" * 10000} for i in range(30)
    ]
    messages.append({"role": "user", "content": "本輪問題"})
    response = asyncio.run(agent_response(db, user, agent, {"messages": messages}))
    assert response.status_code == 200
    history = calls[0][1:-1]
    assert len(history) <= 20
    assert sum(len(item["content"]) for item in history) <= 32000
    assert all(len(item["content"]) <= 8000 for item in history)
    assert "older-0:" not in json.dumps(calls)
    assert "[較早內容已省略]" in json.dumps(calls, ensure_ascii=False)


def test_public_chat_http_preserves_history_and_conversation_scope(
    db, people, http, monkeypatch
):
    users, model, department, _ = people
    rows = (
        users["creator"],
        users["colleague"],
        users["administrator"],
        model,
        department,
    )
    _, _, agent = _assistant(db, rows)
    conversation = Conversation(
        user_id=users["colleague"].id, classification_level="無機密"
    )
    db.add(conversation)
    db.commit()
    calls = []

    async def model_response(db, model, body, **kwargs):
        calls.append(body["messages"])
        return "合成表格"

    monkeypatch.setattr(internal_llm, "complete_chat", model_response)
    headers = {
        **login(http, "colleague"),
        "X-ANILA-Conversation-Id": str(conversation.id),
    }
    body = {
        "model": agent.name,
        "stream": False,
        "messages": [
            {"role": "user", "content": "合成行政工作清單"},
            {"role": "assistant", "content": "合成工作甲、乙"},
            {"role": "user", "content": "把上一段改成表格"},
        ],
    }
    response = http.post("/v1/chat/completions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "合成表格"
    assert "合成工作甲、乙" in json.dumps(calls, ensure_ascii=False)
    foreign = Conversation(user_id=users["outsider"].id)
    db.add(foreign)
    db.commit()
    response = http.post(
        "/v1/chat/completions",
        headers={**headers, "X-ANILA-Conversation-Id": str(foreign.id)},
        json=body,
    )
    assert response.status_code in {403, 404}
    assert len(calls) == 1
