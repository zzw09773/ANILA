"""以真實提問者執行流程，並提供 OpenAI 相容回覆。"""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import json
import re
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import QuickAssistant, QuickRun, QuickVersion
from app.models.user import User
from app.schemas.contracts.classification import (
    ClassificationLevel,
    outbound_action_allowed,
)
from app.services.quick_builder import (
    audit,
    can_edit,
    can_review,
    fail,
    load,
    quick_agent_allowed,
    validate_sources,
    version_for,
)

_SENSITIVE_KEYS = re.compile(
    r"password|passwd|secret|token|authorization|credential|cookie|api[_-]?key|密碼|憑證|身分證|身份證|員工編號|employee[_-]?id",
    re.I,
)
_IDENTITY = re.compile(r"\b[A-Z][12]\d{8}\b")


def mask(value, *, depth=0):
    if depth > 12:
        return "[內容過深，已省略]"
    if isinstance(value, dict):
        return {
            str(k): "[已遮罩]"
            if _SENSITIVE_KEYS.search(str(k))
            else mask(v, depth=depth + 1)
            for k, v in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [mask(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        if len(value) < 200000 and value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(
                    mask(json.loads(value), depth=depth + 1), ensure_ascii=False
                )
            except (ValueError, TypeError):
                pass
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9_.-]+", "Bearer [已遮罩]", value)
        text = re.sub(
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
            "[權杖已遮罩]",
            text,
        )
        text = re.sub(
            r"(?i)((?:password|secret|token|api[_-]?key|apikey|credential|cookie|authorization|employee[_-]?id|員工編號|密碼|憑證)\s*[:=：]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;，；]+)",
            r"\1[已遮罩]",
            text,
        )
        text = _IDENTITY.sub("[身分證字號已遮罩]", text)
        return re.sub(r"\b09\d{8}\b", "[手機已遮罩]", text)[:20000]
    return value


def _rtf(text):
    def char(c):
        if c in "\\{}":
            return "\\" + c
        if c == "\n":
            return "\\par\n"
        if ord(c) < 128:
            return c
        encoded = c.encode("utf-16-le")
        return "".join(
            "\\u" + str(int.from_bytes(encoded[i : i + 2], "little", signed=True)) + "?"
            for i in range(0, len(encoded), 2)
        )

    return (
        "{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 sans-serif;}}\\uc1\n"
        + "".join(char(c) for c in text)
        + "}"
    )


def artifact_execution_level(db, run, version, artifact=None):
    from app.models.task import Task
    from app.models.conversation import Conversation
    from app.modules.policy import effective_level

    context = run.result.get("execution_context", {})
    level = max(
        ClassificationLevel.from_storage(version.payload["classification_level"]),
        ClassificationLevel.from_storage(context.get("classification_level", "無機密")),
    )
    for resource_type, model, field, owner_field in (
        ("task", Task, "task_id", "requester_user_id"),
        ("conversation", Conversation, "conversation_id", "user_id"),
    ):
        resource_id = context.get(field)
        if resource_id is None:
            continue
        resource = db.query(model).filter_by(id=resource_id).populate_existing().first()
        if resource is None or getattr(resource, owner_field) != run.user_id:
            fail(403, "CONTEXT_DENIED", "原任務或對話已無法存取，不能下載這份文件")
        level = max(
            level,
            effective_level(
                db, resource_type=resource_type, resource_id=str(resource_id)
            ),
        )
    if artifact and artifact.get("artifact_id"):
        from app.models.artifact import Artifact, ArtifactVersion

        registered = (
            db.query(Artifact)
            .filter_by(id=artifact["artifact_id"])
            .populate_existing()
            .first()
        )
        registered_version = db.get(
            ArtifactVersion, artifact.get("artifact_version_id")
        )
        if (
            not registered
            or registered.status != "completed"
            or registered.owner_user_id != run.user_id
            or not registered_version
            or registered_version.artifact_id != registered.id
        ):
            fail(403, "ARTIFACT_DENIED", "文件產物的歸屬或版本已變更，不能下載")
        level = max(
            level,
            effective_level(
                db, resource_type="artifact", resource_id=str(registered.id)
            ),
        )
        if registered.source_task_id:
            task = (
                db.query(Task)
                .filter_by(id=registered.source_task_id)
                .populate_existing()
                .first()
            )
            if task is None or task.requester_user_id != run.user_id:
                fail(403, "CONTEXT_DENIED", "原文件任務已無法存取，不能下載")
            level = max(
                level,
                effective_level(db, resource_type="task", resource_id=str(task.id)),
            )
        if registered.source_snapshot_id:
            from app.models.source_snapshot import SourceSnapshot

            snapshot = (
                db.query(SourceSnapshot)
                .filter_by(id=registered.source_snapshot_id)
                .populate_existing()
                .first()
            )
            snapshot_task = (
                db.query(Task)
                .filter_by(id=snapshot.task_id)
                .populate_existing()
                .first()
                if snapshot
                else None
            )
            if (
                not snapshot
                or not snapshot_task
                or snapshot_task.requester_user_id != run.user_id
                or (
                    registered.source_task_id is not None
                    and snapshot.task_id != registered.source_task_id
                )
            ):
                fail(403, "CONTEXT_DENIED", "文件來源快照的歸屬已變更，不能下載")
            level = max(
                level,
                effective_level(
                    db, resource_type="source_snapshot", resource_id=str(snapshot.id)
                ),
                effective_level(
                    db, resource_type="task", resource_id=str(snapshot_task.id)
                ),
            )
    return level


async def run(
    db,
    user,
    assistant_id,
    version_id,
    message,
    *,
    testing=False,
    api_key_id=None,
    task_ctx=None,
    conversation_id=None,
    dispatch_claims=None,
):
    from app.services.quick_flow import execute_graph

    assistant = load(db, user, assistant_id)
    version = version_for(db, assistant, version_id)
    agent = db.get(Agent, version.agent_id) if version.agent_id else None
    if testing:
        if not can_edit(db, user, assistant) and not can_review(db, user, assistant):
            fail(404, "NOT_FOUND", "找不到可測試的流程版本")
    elif (
        not agent
        or agent.approval_status != "approved"
        or not quick_agent_allowed(db, user, agent.id)
    ):
        fail(404, "NOT_FOUND", "這個助手尚未發布或已停用")
    payload = deepcopy(version.payload)
    task_id = conv_id = None
    if not testing:
        from app.models.task import Task
        from app.models.conversation import Conversation
        from app.modules.policy import effective_level

        task_id = (
            task_ctx.task_id if task_ctx else (dispatch_claims or {}).get("task_id")
        )
        conv_id = conversation_id or (dispatch_claims or {}).get("conversation_id")
        context_level = ClassificationLevel.UNCLASSIFIED
        for resource_type, context_model, context_id, owner_field in (
            ("task", Task, task_id, "requester_user_id"),
            ("conversation", Conversation, conv_id, "user_id"),
        ):
            if context_id is None:
                continue
            context = db.get(context_model, context_id)
            if not context or getattr(context, owner_field) != user.id:
                fail(403, "CONTEXT_DENIED", "任務或對話不屬於提問者")
            context_level = max(
                context_level,
                effective_level(
                    db, resource_type=resource_type, resource_id=str(context_id)
                ),
            )
        payload["classification_level"] = max(
            context_level,
            ClassificationLevel.from_storage(payload["classification_level"]),
        ).to_storage()
    if assistant.disabled:
        fail(409, "ASSISTANT_DISABLED", "助手已停用，無法執行")
    validate_sources(db, user, payload, complete=True, check_model_access=testing)
    captured_user_id, captured_department = user.id, user.department_id
    captured_token_version = user.token_version
    captured_agent_id = agent.id if agent else None
    run_row = QuickRun(
        version_id=version.id, user_id=user.id, status="running", result={}
    )
    db.add(run_row)
    db.flush()
    run_id = run_row.id
    audit(
        db,
        user,
        "execute_start",
        assistant.id,
        version_id=version.id,
        run_id=run_id,
        testing=testing,
    )
    db.commit()
    artifacts = []

    def reauthorize():
        if dispatch_claims and dispatch_claims.get("exp", 0) <= int(time.time()):
            fail(401, "DISPATCH_EXPIRED", "這次執行的身分憑證已到期，請重新提問")
        current = (
            db.query(User).filter_by(id=captured_user_id).populate_existing().first()
        )
        current_assistant = (
            db.query(QuickAssistant)
            .filter_by(id=assistant.id)
            .populate_existing()
            .first()
        )
        if (
            not current
            or not current.is_active
            or not current.is_approved
            or current.department_id != captured_department
            or current.token_version != captured_token_version
        ):
            fail(403, "IDENTITY_CHANGED", "使用者狀態或所屬單位已變更，請重新登入")
        if api_key_id is not None:
            from app.models.api_key import ApiKey
            from datetime import datetime, timezone

            api_key = (
                db.query(ApiKey)
                .filter_by(id=api_key_id, user_id=current.id)
                .populate_existing()
                .first()
            )
            expiry = api_key.expires_at if api_key else None
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if (
                not api_key
                or not api_key.is_active
                or (expiry and expiry <= datetime.now(timezone.utc))
            ):
                fail(403, "API_KEY_REVOKED", "API 金鑰已停用或到期，執行已中止")
        if not current_assistant or current_assistant.disabled:
            fail(403, "ASSISTANT_DISABLED", "助手已停用，執行已中止")
        if not testing:
            from app.services.api_key_service import check_agent_permission

            active = (
                db.query(Agent)
                .filter_by(id=captured_agent_id)
                .populate_existing()
                .first()
            )
            if (
                not active
                or active.approval_status != "approved"
                or active.unavailable_reason
                or not check_agent_permission(
                    db, user=current, api_key_id=api_key_id, agent_id=active.id
                )
            ):
                fail(403, "AGENT_REVOKED", "助手權限已撤銷，執行已中止")
            for resource_type, context_model, context_id, owner_field in (
                ("task", Task, task_id, "requester_user_id"),
                ("conversation", Conversation, conv_id, "user_id"),
            ):
                if context_id is None:
                    continue
                current_context = (
                    db.query(context_model)
                    .filter_by(id=context_id)
                    .populate_existing()
                    .first()
                )
                owner_id = getattr(current_context, owner_field, None)
                if not current_context or owner_id != current.id:
                    fail(403, "CONTEXT_DENIED", "任務或對話的存取權限已變更")
                current_level = effective_level(
                    db,
                    resource_type=resource_type,
                    resource_id=str(context_id),
                )
                payload["classification_level"] = max(
                    current_level,
                    ClassificationLevel.from_storage(payload["classification_level"]),
                ).to_storage()
        validate_sources(
            db, current, payload, complete=True, check_model_access=testing
        )
        return current

    async def model_call(messages, temperature=0.2):
        from app.services.internal_llm import complete_chat

        current = reauthorize()
        model = (
            db.query(ModelRegistry)
            .filter_by(id=payload["model_id"])
            .populate_existing()
            .first()
        )
        from app.services.quick_mcp import list_tools
        from app.services.quick_builder import _graph_sources

        _, selected_tools = _graph_sources(payload["graph"])
        if selected_tools:
            catalog = [
                {k: item[k] for k in ("id", "name", "description", "input_schema")}
                for item in list_tools(db, current)
                if item["id"] in selected_tools
            ]
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": json.dumps(
                        {"approved_tool_catalog": catalog}, ensure_ascii=False
                    ),
                },
            ]
        if dispatch_claims:
            from app.services.proxy.dispatch_chat import (
                dispatch_model_call,
                enforce_dispatched_model_ceiling,
            )

            enforce_dispatched_model_ceiling(
                db, dispatch_model_call(db, dispatch_claims), model
            )
        elif not testing:
            from types import SimpleNamespace
            from app.services.proxy.ceiling import enforce_model_ceiling

            enforce_model_ceiling(
                db,
                model=model,
                caller=SimpleNamespace(user=current),
                task_ctx=task_ctx,
                conv_id_int=conversation_id,
            )
        return await complete_chat(
            db,
            model,
            {
                "model": model.name,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 4096,
            },
            user_id=current.id,
            department_id=current.department_id,
            on_behalf_of_user=True,
            caller_agent_id=captured_agent_id,
            api_key_id=api_key_id,
        )

    async def rag_call(collection_ids, query, top_k=5):
        from app.api.ingestion.search import (
            SearchPrincipal,
            SearchRequest,
            search_collection,
        )

        current = reauthorize()
        if not set(collection_ids).issubset(payload["collection_ids"]):
            fail(403, "SOURCE_NOT_DECLARED", "流程嘗試讀取未核准的文件集")
        hits = []
        for collection_id in collection_ids:
            response = await search_collection(
                collection_id,
                SearchRequest(query=query, top_k=top_k, search_mode="hybrid"),
                db,
                SearchPrincipal(user=current),
            )
            hits.extend(item.model_dump() for item in response.results)
        return hits[:top_k]

    async def tool_call(tool_id, arguments):
        from app.services.quick_mcp import call_tool

        current = reauthorize()
        fingerprint = payload.get("tool_fingerprints", {}).get(str(tool_id))
        if not fingerprint:
            fail(403, "TOOL_NOT_DECLARED", "流程嘗試使用未核准的查詢工具")
        response = await call_tool(
            db,
            current,
            tool_id,
            arguments,
            assistant_id=assistant.id,
            expected_fingerprint=fingerprint,
        )
        if (
            isinstance(response, dict)
            and isinstance(response.get("data"), dict)
            and response["data"].get("isError")
        ):
            return {**response, "isError": True}
        return response

    async def document_call(template, text, *, config=None):
        from app.services.quick_flow import QuickFlowExecutionError

        try:
            return await create_document(template, text, config=config)
        except HTTPException as exc:
            detail = exc.detail
            code = detail.get("type", "") if isinstance(detail, dict) else ""
            if (
                re.fullmatch(r"DOCUMENT_[A-Z_]+", code)
                or code
                in {
                    "EXPORT_CLASSIFICATION",
                    "IDENTITY_CHANGED",
                    "CONTEXT_DENIED",
                    "RUN_NOT_FOUND",
                }
            ) and isinstance(detail.get("message"), str):
                raise QuickFlowExecutionError(
                    code, mask(detail["message"])[:300]
                ) from None
            raise

    async def create_document(template, text, *, config=None):
        current = reauthorize()
        if not outbound_action_allowed(
            ClassificationLevel.from_storage(payload["classification_level"])
        ):
            fail(403, "EXPORT_CLASSIFICATION", "這個密等的內容不能匯出文件")
        if len(artifacts) >= 5:
            fail(422, "DOCUMENT_LIMIT", "一次流程最多產生五份文件，請減少文件模組")
        if config and config.get("mode") == "studio":
            from app.services.quick_document import (
                resolve_studio_url,
                render_document,
                register_document_artifact,
            )

            studio_url = await resolve_studio_url(
                db, current, payload["classification_level"]
            )
            current = reauthorize()

            def document_before_send():
                current_user = reauthorize()
                return current_user, payload["classification_level"]

            document_text = (
                template.replace("{{text}}", text) if template.strip() else text
            )
            rendered = await render_document(
                db,
                current,
                studio_url=studio_url,
                run_id=run_id,
                text=document_text,
                preset=config["preset"],
                output_format=config["format"],
                title=config["title"],
                classification_level=payload["classification_level"],
                before_send=document_before_send,
            )
            current = reauthorize()
            metadata = register_document_artifact(
                db,
                current,
                run_id=run_id,
                rendered=rendered,
                classification_level=payload["classification_level"],
                task_id=task_id,
                agent_id=captured_agent_id,
                model_id=payload["model_id"],
                artifact_index=len(artifacts),
            )
            artifact = {
                **metadata,
                "filename": rendered["filename"],
                "media_type": rendered["media_type"],
                "content": base64.b64encode(rendered["content"]).decode("ascii"),
                "encoding": "base64",
                "format": rendered["format"],
                "content_hash": rendered["content_hash"],
            }
            artifacts.append(artifact)
            return {
                "filename": artifact["filename"],
                "text": document_text,
                "download_url": metadata["download_url"],
            }
        rendered = template.replace("{{text}}", text)
        if len(rendered) > 200000:
            fail(422, "DOCUMENT_TOO_LARGE", "文件內容過長，請縮小彙整範圍")
        artifact = {
            "filename": "助手草稿.rtf",
            "media_type": "application/rtf",
            "content": _rtf(rendered),
        }
        artifacts.append(artifact)
        return {
            "filename": artifact["filename"],
            "text": rendered,
            "download_url": f"/api/quick-builder/runs/{run_id}/artifacts/{len(artifacts) - 1}",
        }

    try:
        result = await asyncio.wait_for(
            execute_graph(
                payload["graph"],
                message,
                model_call=model_call,
                rag_call=rag_call,
                tool_call=tool_call,
                document_call=document_call,
            ),
            timeout=120,
        )
        reauthorize()
        if artifacts and not outbound_action_allowed(
            ClassificationLevel.from_storage(payload["classification_level"])
        ):
            fail(
                403, "EXPORT_CLASSIFICATION", "任務或對話的密等已提高，這次文件不能匯出"
            )
    except asyncio.CancelledError:
        result = {
            "output": "",
            "status": "cancelled",
            "steps": [],
            "error": {"type": "CANCELLED", "message": "連線已中斷，流程已停止"},
        }
        _finish(
            db,
            user,
            assistant.id,
            run_id,
            result,
            [],
            {
                "classification_level": payload["classification_level"],
                "task_id": task_id,
                "conversation_id": conv_id,
            },
        )
        raise
    except Exception as exc:
        code = (
            "EXECUTION_TIMEOUT"
            if isinstance(exc, asyncio.TimeoutError)
            else "EXECUTION_FAILED"
        )
        result = {
            "output": "",
            "status": "failed",
            "steps": [],
            "error": {
                "type": code,
                "message": "流程未能完成，請檢查資料來源與模型狀態",
            },
        }
    if result["status"] != "completed":
        artifacts = []
    _finish(
        db,
        user,
        assistant.id,
        run_id,
        result,
        artifacts,
        {
            "classification_level": payload["classification_level"],
            "task_id": task_id,
            "conversation_id": conv_id,
        },
    )
    return public_result(run_id, result, artifacts)


def _finish(db, user, assistant_id, run_id, result, artifacts, execution_context):
    db.rollback()
    stored = db.get(QuickRun, run_id)
    if result["status"] != "completed":
        from app.models.artifact import Artifact, ArtifactVersion

        for artifact in (
            db.query(Artifact)
            .filter(
                Artifact.owner_user_id == stored.user_id,
                Artifact.metadata_json["quick_run_id"].as_integer() == run_id,
            )
            .all()
        ):
            artifact.status = "failed"
            artifact.metadata_json = {
                **(artifact.metadata_json or {}),
                "quick_run_status": result["status"],
                "failure_reason": result.get("error", {}).get("type", "FLOW_FAILED"),
            }
            for version in (
                db.query(ArtifactVersion).filter_by(artifact_id=artifact.id).all()
            ):
                version.storage_ref = None
                version.file_refs = []
            audit(
                db,
                user,
                "artifact_invalidate",
                assistant_id,
                run_id=run_id,
                artifact_id=artifact.id,
                status=result["status"],
            )
    stored.status = result["status"]
    stored.result = {
        **mask(result),
        "artifacts": artifacts,
        "execution_context": execution_context,
    }
    audit(
        db, user, "execute_finish", assistant_id, run_id=run_id, status=result["status"]
    )
    db.commit()


def public_result(run_id, result, artifacts):
    return {
        "id": run_id,
        **mask(
            {key: value for key, value in result.items() if key != "execution_context"}
        ),
        "artifacts": [
            {
                "filename": item["filename"],
                "download_url": f"/api/quick-builder/runs/{run_id}/artifacts/{i}",
            }
            for i, item in enumerate(artifacts)
        ],
    }


async def agent_response(
    db,
    user,
    agent,
    body,
    *,
    task_ctx=None,
    api_key_id=None,
    conversation_id=None,
    dispatch_claims=None,
):
    version = db.query(QuickVersion).filter_by(agent_id=agent.id).first()
    if not version:
        fail(404, "NOT_FOUND", "找不到助手流程")
    messages = body.get("messages")
    if not isinstance(messages, list):
        fail(422, "MESSAGE_REQUIRED", "請提供對話訊息")
    message = next(
        (
            m.get("content")
            for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ),
        None,
    )
    if not isinstance(message, str) or not message.strip() or len(message) > 16000:
        fail(422, "INVALID_MESSAGE", "請提供長度不超過 16000 字的文字訊息")
    result = await run(
        db,
        user,
        version.assistant_id,
        version.id,
        message,
        api_key_id=api_key_id,
        task_ctx=task_ctx,
        conversation_id=conversation_id,
        dispatch_claims=dispatch_claims,
    )
    if task_ctx:
        from app.services.proxy.task_link import finalize_task_run

        finalize_task_run(
            task_ctx.task_run_id,
            "completed" if result["status"] == "completed" else "failed",
        )
    if result["status"] != "completed":
        fail(
            502,
            "FLOW_FAILED",
            result.get("error", {}).get("message", "助手流程未能完成"),
        )
    text = result["output"]
    if result["artifacts"]:
        text += "\n\n" + "\n".join(
            f"[{a['filename']}]({a['download_url']})" for a in result["artifacts"]
        )
    completion_id, created = "chatcmpl-quick-" + uuid.uuid4().hex, int(time.time())
    if body.get("stream"):

        async def stream():
            for i in range(0, len(text), 16):
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": agent.name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text[i : i + 16]},
                            "finish_reason": None,
                        }
                    ],
                }
                yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                await asyncio.sleep(0)
            yield (
                "data: "
                + json.dumps(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": agent.name,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")
    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": agent.name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
    )
