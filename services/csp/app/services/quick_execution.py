"""以真實提問者執行流程，並提供 OpenAI 相容回覆。"""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import hashlib
import json
import re
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import DBAPIError

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
from app.services.completion_metadata import normalize_completion

_SENSITIVE_KEYS = re.compile(
    r"password|passwd|secret|token|authorization|credential|cookie|api[_-]?key|密碼|憑證|身分證|身份證|員工編號|employee[_-]?id",
    re.I,
)
_IDENTITY = re.compile(r"\b[A-Z][12]\d{8}\b")
_QUOTED_FIELD = re.compile(
    r"(?P<label>(?P<quote>[\"'])(?P<key>(?:\\.|(?!(?P=quote))[^\\])+)(?P=quote)\s*[:=：]\s*)"
)
TEST_LEASE_HEARTBEAT_SECONDS = 5
TEST_LEASE_BUSY_RETRY_SECONDS = 1
MAX_ACTOR_TEST_RUNS = 2


def test_tracking(run_row):
    """Read only the server-created recovery marker; never expose its hash."""
    value = (
        {
            "testing": True,
            "request_id": run_row.test_request_id,
            "request_hash": run_row.test_request_hash,
            "actor_department_id": run_row.test_actor_department_id,
        }
        if run_row.is_test
        else (run_row.result or {}).get("test_tracking")
    )
    if not isinstance(value, dict) or value.get("testing") is not True:
        return None
    if set(value) != {"testing", "request_id", "request_hash", "actor_department_id"}:
        return None
    request_id = value["request_id"]
    try:
        if request_id is not None and str(uuid.UUID(request_id)) != request_id:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    department_id = value["actor_department_id"]
    if department_id is not None and (
        type(department_id) is not int or department_id <= 0
    ):
        return None
    if not isinstance(value["request_hash"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["request_hash"]
    ):
        return None
    return {key: value[key] for key in value}


def authorize_test_read(db, user, assistant_id, version_id, *, tracking=None):
    """The same current edit/review and source/model gates as a new test."""
    actor_id, token_version, department_id = (
        user.id,
        user.token_version,
        user.department_id,
    )
    current = db.get(User, actor_id, populate_existing=True)
    assistant = db.get(QuickAssistant, assistant_id, populate_existing=True)
    version = (
        db.get(QuickVersion, version_id, populate_existing=True)
        if version_id is not None
        else None
    )
    if (
        not current
        or not current.is_active
        or not current.is_approved
        or current.token_version != token_version
        or current.department_id != department_id
        or not assistant
        or assistant.disabled
        or (
            version_id is not None
            and (not version or version.assistant_id != assistant.id)
        )
        or (
            tracking is not None
            and tracking["actor_department_id"] != current.department_id
        )
        or not (can_edit(db, current, assistant) or can_review(db, current, assistant))
    ):
        fail(404, "NOT_FOUND", "找不到這筆試跑紀錄")
    try:
        if version is not None:
            validate_sources(
                db, current, version.payload, complete=True, check_model_access=True
            )
    except HTTPException:
        fail(404, "NOT_FOUND", "找不到這筆試跑紀錄")
    return assistant, version


def stored_public_result(run_row, version):
    """Project authoritative row state, including provenance for recoverable tests."""
    result = dict(run_row.result or {})
    artifacts = result.pop("artifacts", [])
    result["status"] = run_row.status
    tracking = test_tracking(run_row)
    if tracking is not None:
        result.update(
            version_id=version.id,
            revision=version.revision,
            request_id=tracking["request_id"],
        )
    return public_result(run_row.id, result, artifacts)


def _start_test(db, user, assistant_id, version_id, message, request_id, *, admit=None):
    from app.services.quick_test_lifecycle import claim_new

    # Preserve the authenticated identity before populate_existing can refresh it.
    actor_id, token_version, department_id = (
        user.id,
        user.token_version,
        user.department_id,
    )
    try:
        nonce = str(uuid.UUID(str(request_id))) if request_id is not None else None
    except (ValueError, TypeError, AttributeError):
        fail(422, "INVALID_INPUT", "試跑識別碼格式不正確")
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    try:
        current = (
            db.query(User)
            .filter_by(id=actor_id)
            .populate_existing()
            .with_for_update(nowait=True, of=User)
            .first()
        )
        if (
            not current
            or not current.is_active
            or not current.is_approved
            or current.token_version != token_version
            or current.department_id != department_id
        ):
            fail(404, "NOT_FOUND", "找不到可測試的流程版本")
        if nonce is not None:
            previous = (
                db.query(QuickRun)
                .filter(
                    QuickRun.user_id == actor_id,
                    QuickRun.is_test.is_(True),
                    QuickRun.test_request_id == nonce,
                )
                .order_by(QuickRun.id)
                .populate_existing()
                .first()
            )
            if previous is not None:
                tracking = test_tracking(previous)
                previous_version = db.get(
                    QuickVersion, previous.version_id, populate_existing=True
                )
                if tracking is None or tracking["actor_department_id"] != department_id:
                    fail(404, "NOT_FOUND", "找不到這筆試跑紀錄")
                if previous_version is None:
                    fail(404, "NOT_FOUND", "找不到這筆試跑紀錄")
                authorize_test_read(
                    db,
                    current,
                    previous_version.assistant_id,
                    previous_version.id,
                    tracking=tracking,
                )
                if (
                    previous.version_id != version_id
                    or not previous_version
                    or previous_version.assistant_id != assistant_id
                    or tracking["request_hash"] != digest
                ):
                    fail(
                        409,
                        "TEST_REQUEST_CONFLICT",
                        "這個試跑識別碼已用於不同內容或版本，請重新開始試跑",
                    )
                recover_test_runs(
                    db, current, previous_version.assistant_id, run_id=previous.id
                )
                db.refresh(previous)
                response = stored_public_result(previous, previous_version)
                db.commit()
                return None, response
        assistant = load(db, current, assistant_id)
        assistant = db.get(QuickAssistant, assistant.id, populate_existing=True)
        version = version_for(db, assistant, version_id)
        if not (can_edit(db, current, assistant) or can_review(db, current, assistant)):
            fail(404, "NOT_FOUND", "找不到可測試的流程版本")
        if assistant.disabled:
            fail(409, "ASSISTANT_DISABLED", "助手已停用，無法執行")
        payload = deepcopy(version.payload)
        validate_sources(db, current, payload, complete=True, check_model_access=True)
        clock = (
            func.clock_timestamp()
            if db.get_bind().dialect.name == "postgresql"
            else func.current_timestamp()
        )
        active = (
            db.query(QuickRun.id)
            .filter(
                QuickRun.user_id == actor_id,
                QuickRun.is_test.is_(True),
                QuickRun.status == "running",
                QuickRun.execution_owner_token.is_not(None),
                QuickRun.lease_expires_at > clock,
            )
            .limit(MAX_ACTOR_TEST_RUNS)
            .all()
        )
        if len(active) >= MAX_ACTOR_TEST_RUNS:
            fail(
                429,
                "TEST_RUN_BUSY",
                "本人已有兩筆試跑正在執行，請先等待或停止其中一筆；系統未建立新執行。",
            )
        if admit is not None:
            admit()
        marker = {
            "testing": True,
            "request_id": nonce,
            "request_hash": digest,
            "actor_department_id": department_id,
        }
        row = QuickRun(
            version_id=version.id,
            user_id=actor_id,
            status="running",
            result={
                "status": "running",
                "output": "",
                "steps": [],
                "test_tracking": marker,
            },
        )
        db.add(row)
        lease = claim_new(
            db,
            row,
            request_id=nonce,
            message=message,
            actor_department_id=department_id,
        )
        audit(
            db,
            current,
            "execute_start",
            assistant.id,
            version_id=version.id,
            run_id=row.id,
            testing=True,
        )
        agent = db.get(Agent, version.agent_id) if version.agent_id else None
        db.commit()  # Release the User mutex before any model/tool await.
        return (
            assistant,
            version,
            agent,
            payload,
            row,
            actor_id,
            department_id,
            token_version,
            lease,
        ), None
    except DBAPIError as exc:
        db.rollback()
        if (
            getattr(exc.orig, "sqlstate", None) == "55P03"
            or getattr(exc.orig, "pgcode", None) == "55P03"
        ):
            fail(
                409, "TEST_REQUEST_BUSY", "另一個試跑請求正在建立，請稍後重試同一識別碼"
            )
        raise
    except BaseException:
        db.rollback()
        raise


def _mask_quoted_fields(text):
    parts, cursor = [], 0
    for match in _QUOTED_FIELD.finditer(text):
        if match.start() < cursor:
            continue
        key = re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda item: chr(int(item.group(1), 16)),
            match["key"],
        )
        if not _SENSITIVE_KEYS.search(key):
            continue
        tail = text[match.end() :]
        try:
            decoded, end = json.JSONDecoder().raw_decode(tail)
            if not isinstance(decoded, (str, dict, list)):
                end = len(re.match(r"[^\s,;，；}\]]*", tail)[0])
        except (ValueError, TypeError, RecursionError):
            # An unfinished value cannot safely disclose a suffix.
            end = len(tail)
        parts.append(text[cursor : match.end()] + "[已遮罩]")
        cursor = match.end() + end
    return "".join(parts) + text[cursor:]


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
        if len(value) < 200000:
            stripped = value.lstrip()
            position = len(value) - len(stripped)
            if not stripped.startswith(("{", "[", '"')):
                position = value.find("{")
            structured = value[position:] if position >= 0 else ""
            if structured:
                try:
                    decoded, end = json.JSONDecoder().raw_decode(structured)
                except (ValueError, TypeError, RecursionError):
                    looks_json = structured.startswith('"') or re.match(
                        r'(?:\{\s*(?:["}]|$)|\[\s*(?:[\[{"\d-]|true|false|null|$))',
                        structured,
                    )
                    if looks_json:
                        return (
                            mask(value[:position], depth=depth + 1)
                            + "[結構化內容不完整或過深，已遮罩]"
                        )[:20000]
                else:
                    if isinstance(decoded, (dict, list, str)):
                        return (
                            mask(value[:position], depth=depth + 1)
                            + json.dumps(
                                mask(decoded, depth=depth + 1), ensure_ascii=False
                            )
                            + mask(structured[end:], depth=depth + 1)
                        )[:20000]
        text = _mask_quoted_fields(value)
        text = re.sub(r"(?i)bearer\s+[A-Za-z0-9_.-]+", "Bearer [已遮罩]", text)
        text = re.sub(
            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
            "[權杖已遮罩]",
            text,
        )
        text = re.sub(
            r"(?i)([\"']?(?:password|passwd|secret|token|api[_-]?key|apikey|credential|cookie|authorization|employee[_-]?id|員工編號|密碼|憑證)[\"']?\s*[:=：]\s*)(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;，；}\]]+)",
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


def _snapshot_level(db, user_id, snapshot_id, *, task_id=None):
    from app.models.source_snapshot import SourceSnapshot
    from app.models.task import Task
    from app.modules.policy import effective_level

    snapshot = (
        db.query(SourceSnapshot).filter_by(id=snapshot_id).populate_existing().first()
    )
    task = (
        db.query(Task).filter_by(id=snapshot.task_id).populate_existing().first()
        if snapshot
        else None
    )
    if (
        not snapshot
        or not task
        or task.requester_user_id != user_id
        or (task_id is not None and snapshot.task_id != task_id)
    ):
        fail(403, "CONTEXT_DENIED", "文件來源快照已無法存取，請重新確認任務來源")
    return max(
        effective_level(
            db, resource_type="source_snapshot", resource_id=str(snapshot.id)
        ),
        effective_level(db, resource_type="task", resource_id=str(task.id)),
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
        if resource_type == "task" and resource.source_snapshot_id is not None:
            level = max(
                level,
                _snapshot_level(
                    db, run.user_id, resource.source_snapshot_id, task_id=resource.id
                ),
            )
    if context.get("source_snapshot_id") is not None:
        level = max(
            level,
            _snapshot_level(
                db,
                run.user_id,
                context["source_snapshot_id"],
                task_id=context.get("task_id"),
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


def _resume_test(db, user, assistant_id, version_id, message, request_id, lease):
    from app.services.quick_test_lifecycle import RunLease, check_in_session

    actor_id, department_id, token_version = (
        user.id,
        user.department_id,
        user.token_version,
    )
    row = (
        db.get(QuickRun, lease.run_id, populate_existing=True)
        if isinstance(lease, RunLease)
        else None
    )
    if (
        row is None
        or row.user_id != actor_id
        or lease.user_id != actor_id
        or not row.is_test
        or row.version_id != version_id
        or row.test_request_id != str(request_id)
        or row.test_request_hash != hashlib.sha256(message.encode("utf-8")).hexdigest()
        or row.test_actor_department_id != department_id
        or not check_in_session(db, lease)
    ):
        db.rollback()
        fail(409, "TEST_RUN_INACTIVE", "這筆試跑的執行權已失效，請查詢原執行紀錄")
    assistant, version = authorize_test_read(
        db, user, assistant_id, version_id, tracking=test_tracking(row)
    )
    agent = db.get(Agent, version.agent_id) if version.agent_id else None
    payload = deepcopy(version.payload)
    db.commit()
    return (
        assistant,
        version,
        agent,
        payload,
        row,
        actor_id,
        department_id,
        token_version,
        lease,
    )


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
    history=None,
    on_delta=None,
    on_step=None,
    test_request_id=None,
    test_lease=None,
    on_lease_lost=None,
):
    """Execute once; a detached Builder observer never owns this task's lifetime."""
    started = None
    heartbeat = None
    lease_state = {"lost": False}
    if testing:
        if test_lease is None:
            started, previous = _start_test(
                db, user, assistant_id, version_id, message, test_request_id
            )
            if previous is not None:
                return previous
        else:
            started = _resume_test(
                db, user, assistant_id, version_id, message, test_request_id, test_lease
            )
        lease = started[-1]
        owner_task = asyncio.current_task()
        bind = db.get_bind()

        async def maintain():
            from app.services.quick_test_lifecycle import renew_lease

            delay = TEST_LEASE_HEARTBEAT_SECONDS
            while True:
                await asyncio.sleep(delay)
                try:
                    valid = await asyncio.to_thread(renew_lease, bind, lease)
                except DBAPIError as exc:
                    if (
                        getattr(exc.orig, "sqlstate", None) == "55P03"
                        or getattr(exc.orig, "pgcode", None) == "55P03"
                    ):
                        delay = TEST_LEASE_BUSY_RETRY_SECONDS
                        continue
                    valid = False
                except Exception:
                    valid = False
                if not valid:
                    lease_state["lost"] = True
                    if on_lease_lost is not None:
                        on_lease_lost()
                    owner_task.cancel()
                    return
                delay = TEST_LEASE_HEARTBEAT_SECONDS

        heartbeat = asyncio.create_task(maintain())
    elif test_lease is not None:
        fail(422, "INVALID_INPUT", "執行租約僅供試跑使用")
    try:
        return await _execute_run(
            db,
            user,
            assistant_id,
            version_id,
            message,
            testing=testing,
            api_key_id=api_key_id,
            task_ctx=task_ctx,
            conversation_id=conversation_id,
            dispatch_claims=dispatch_claims,
            history=history,
            on_delta=on_delta,
            on_step=on_step,
            test_request_id=test_request_id,
            _started=started,
            _lease_state=lease_state,
        )
    except asyncio.CancelledError:
        if lease_state["lost"]:
            fail(409, "TEST_RUN_INACTIVE", "試跑的執行權無法確認，請查詢原執行紀錄")
        raise
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


async def _execute_run(
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
    history=None,
    on_delta=None,
    test_request_id=None,
    on_step=None,
    _started=None,
    _lease_state=None,
):
    from app.services.quick_flow import execute_graph

    if testing:
        (
            assistant,
            version,
            agent,
            payload,
            run_row,
            captured_user_id,
            captured_department,
            captured_token_version,
            run_lease,
        ) = _started
        test_provenance = {
            "version_id": version.id,
            "revision": version.revision,
            "request_id": test_tracking(run_row)["request_id"],
        }
    else:
        run_lease = None
        if test_request_id is not None:
            fail(422, "INVALID_INPUT", "試跑識別碼僅供測試流程使用")
        assistant = load(db, user, assistant_id)
        version = version_for(db, assistant, version_id)
        agent = db.get(Agent, version.agent_id) if version.agent_id else None
        if (
            not agent
            or agent.approval_status != "approved"
            or not quick_agent_allowed(db, user, agent.id)
        ):
            fail(404, "NOT_FOUND", "這個助手尚未發布或已停用")
        payload = deepcopy(version.payload)
    task_id = conv_id = source_snapshot_id = None
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
            context = (
                db.query(context_model)
                .filter_by(id=context_id)
                .populate_existing()
                .first()
            )
            if not context or getattr(context, owner_field) != user.id:
                fail(403, "CONTEXT_DENIED", "任務或對話不屬於提問者")
            context_level = max(
                context_level,
                effective_level(
                    db, resource_type=resource_type, resource_id=str(context_id)
                ),
            )
            if resource_type == "task":
                source_snapshot_id = context.source_snapshot_id
                if source_snapshot_id is not None:
                    context_level = max(
                        context_level,
                        _snapshot_level(
                            db, user.id, source_snapshot_id, task_id=task_id
                        ),
                    )
        payload["classification_level"] = max(
            context_level,
            ClassificationLevel.from_storage(payload["classification_level"]),
        ).to_storage()
    if not testing:
        if assistant.disabled:
            fail(409, "ASSISTANT_DISABLED", "助手已停用，無法執行")
        validate_sources(db, user, payload, complete=True, check_model_access=False)
        captured_user_id, captured_department = user.id, user.department_id
        captured_token_version = user.token_version
        run_row = QuickRun(
            version_id=version.id, user_id=user.id, status="running", result={}
        )
        db.add(run_row)
        db.flush()
        audit(
            db,
            user,
            "execute_start",
            assistant.id,
            version_id=version.id,
            run_id=run_row.id,
            testing=False,
        )
        db.commit()
    captured_agent_id = agent.id if agent else None
    run_id = run_row.id
    artifacts = []

    def reauthorize():
        if _lease_state is not None and _lease_state["lost"]:
            fail(409, "TEST_RUN_INACTIVE", "試跑的執行權無法確認，請查詢原執行紀錄")
        if run_lease is not None:
            from app.services.quick_test_lifecycle import check_in_session

            if not _lease_phase(db, lambda: check_in_session(db, run_lease)):
                fail(
                    409, "TEST_RUN_INACTIVE", "這筆試跑的執行權已失效，請查詢原執行紀錄"
                )
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
        if testing and not (
            can_edit(db, current, current_assistant)
            or can_review(db, current, current_assistant)
        ):
            fail(403, "TEST_PERMISSION_REVOKED", "流程測試權限已撤銷，執行已中止")
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
                if resource_type == "task":
                    if current_context.source_snapshot_id != source_snapshot_id:
                        fail(403, "CONTEXT_DENIED", "任務來源已變更，請重新執行助手")
                    if source_snapshot_id is not None:
                        current_level = max(
                            current_level,
                            _snapshot_level(
                                db, current.id, source_snapshot_id, task_id=task_id
                            ),
                        )
                payload["classification_level"] = max(
                    current_level,
                    ClassificationLevel.from_storage(payload["classification_level"]),
                ).to_storage()
        validate_sources(
            db, current, payload, complete=True, check_model_access=testing
        )
        if artifacts and not outbound_action_allowed(
            ClassificationLevel.from_storage(payload["classification_level"])
        ):
            fail(403, "EXPORT_CLASSIFICATION", "任務或來源密等已提高，文件回答已中止")
        if run_lease is not None:
            db.commit()  # Never carry the execution-row fence lock into an await.
        return current

    def model_context(messages):
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
        return current, model, messages

    async def model_call(messages, temperature=0.2):
        from app.services.internal_llm import complete_chat

        current, model, messages = model_context(messages)
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
            return_metadata=True,
        )

    async def stream_model_call(messages, temperature=0.2):
        from app.services.quick_model_stream import stream_chat

        def authorize_delivery():
            model_context([])
            # 串流背壓等待期間，其他請求仍須能取得連線。
            db.commit()

        current, model, messages = model_context(messages)
        if model.supports_streaming is False:
            return await model_call(messages, temperature)
        return await stream_chat(
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
            caller_agent_id=captured_agent_id,
            api_key_id=api_key_id,
            on_delta=lambda text: on_delta(text, before_send=authorize_delivery),
            before_chunk=authorize_delivery,
            return_metadata=True,
        )

    async def rag_call(collection_ids, query, top_k=5, *, mode="hybrid"):
        from app.services.quick_retrieval import retrieve_collections

        current = reauthorize()
        if not set(collection_ids).issubset(payload["collection_ids"]):
            fail(403, "SOURCE_NOT_DECLARED", "流程嘗試讀取未核准的文件集")
        return await retrieve_collections(
            db, current, collection_ids, query, top_k, mode=mode
        )

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
            before_send=reauthorize,
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

            def document_before_persist():
                # Called within the synchronous registration phase and its
                # nested before_commit hooks. Never commit/retry here: acquire
                # the execution row before any pending Artifact can autoflush,
                # and retain it until the existing service commits this phase.
                from app.services.quick_test_lifecycle import check_in_session

                if (_lease_state is not None and _lease_state["lost"]) or not (
                    check_in_session(db, run_lease)
                ):
                    fail(
                        409,
                        "TEST_RUN_INACTIVE",
                        "這筆試跑的執行權已失效，請查詢原執行紀錄",
                    )

            metadata = register_document_artifact(
                db,
                current,
                run_id=run_id,
                rendered=rendered,
                classification_level=payload["classification_level"],
                task_id=task_id,
                source_snapshot_id=source_snapshot_id,
                agent_id=captured_agent_id,
                model_id=payload["model_id"],
                artifact_index=len(artifacts),
                before_persist=(
                    document_before_persist if run_lease is not None else None
                ),
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

    def authorize_final_delivery():
        reauthorize()
        db.commit()

    observed_steps = []

    async def observe_step(event, trace):
        reauthorize()
        safe = _mask_flow_result({"steps": [trace]})["steps"][0]
        index = next(
            (
                i
                for i, step in enumerate(observed_steps)
                if step["node_id"] == safe["node_id"]
            ),
            None,
        )
        if index is None:
            observed_steps.append(safe)
        else:
            observed_steps[index] = safe
        if run_lease is not None:
            from app.services.quick_test_lifecycle import checkpoint_in_session

            if not _lease_phase(
                db, lambda: checkpoint_in_session(db, run_lease, steps=observed_steps)
            ):
                fail(
                    409, "TEST_RUN_INACTIVE", "這筆試跑的執行權已失效，請查詢原執行紀錄"
                )
        db.commit()
        if on_step is not None:
            await on_step(event, deepcopy(safe), before_send=authorize_final_delivery)

    try:
        if on_delta is not None:
            await on_delta("", before_send=authorize_final_delivery)
        result = await asyncio.wait_for(
            execute_graph(
                payload["graph"],
                message,
                model_call=model_call,
                rag_call=rag_call,
                tool_call=tool_call,
                document_call=document_call,
                history=history,
                stream_model_call=stream_model_call if on_delta is not None else None,
                on_step=observe_step if testing or on_step is not None else None,
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
    except (asyncio.CancelledError, GeneratorExit):
        if _lease_state is not None and _lease_state["lost"]:
            # Failed renewal proves neither a user cancel nor safe completion.
            # Stop side effects and let the persisted DB expiry revoke authority.
            db.rollback()
            raise
        result = {
            "output": "",
            "status": "cancelled",
            "steps": [
                {**step, "status": "cancelled"} if step["status"] == "running" else step
                for step in observed_steps
            ]
            if testing
            else [],
            "answer_complete": False,
            "usage_complete": False,
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
                "source_snapshot_id": source_snapshot_id,
                "conversation_id": conv_id,
            },
            lease=run_lease,
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
    if _lease_state is not None and _lease_state["lost"]:
        db.rollback()
        fail(409, "TEST_RUN_INACTIVE", "試跑的執行權無法確認，較晚回覆未保存")
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
            "source_snapshot_id": source_snapshot_id,
            "conversation_id": conv_id,
        },
        lease=run_lease,
    )
    response = public_result(run_id, result, artifacts)
    if testing:
        response.update(test_provenance)
    return response


def _lease_phase(db, operation):
    """Retry only a bounded NOWAIT metadata collision, never a provider call."""
    for attempt in range(3):
        try:
            return operation()
        except DBAPIError as exc:
            db.rollback()
            busy = (
                getattr(exc.orig, "sqlstate", None) == "55P03"
                or getattr(exc.orig, "pgcode", None) == "55P03"
            )
            if not busy or attempt == 2:
                raise


def invalidate_test_artifacts(db, user, assistant_id, run_id, status, error_type):
    from app.models.artifact import Artifact, ArtifactVersion

    for artifact in (
        db.query(Artifact)
        .filter(
            Artifact.owner_user_id == user.id,
            Artifact.metadata_json["quick_run_id"].as_integer() == run_id,
        )
        .all()
    ):
        artifact.status = "failed"
        artifact.metadata_json = {
            **(artifact.metadata_json or {}),
            "quick_run_status": status,
            "failure_reason": error_type,
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
            status=status,
        )


def recover_test_runs(db, user, assistant_id, *, run_id):
    from app.services.quick_test_lifecycle import recover_in_session

    recovered = recover_in_session(db, user_id=user.id, run_id=run_id)
    for recovered_id in recovered:
        invalidate_test_artifacts(
            db, user, assistant_id, recovered_id, "interrupted", "RESULT_UNCONFIRMED"
        )
        audit(
            db,
            user,
            "execute_recover",
            assistant_id,
            run_id=recovered_id,
            status="interrupted",
        )
    return recovered


def _finish(
    db, user, assistant_id, run_id, result, artifacts, execution_context, *, lease=None
):
    db.rollback()
    stored = db.get(QuickRun, run_id)
    tracking = test_tracking(stored)
    safe = {
        **_mask_flow_result(result),
        "artifacts": artifacts,
        "execution_context": execution_context,
    }
    if tracking is not None:
        safe["test_tracking"] = tracking
    if lease is not None:
        from app.services.quick_test_lifecycle import finish_in_session

        if not _lease_phase(
            db,
            lambda: finish_in_session(db, lease, status=result["status"], result=safe),
        ):
            db.rollback()
            fail(409, "TEST_RUN_INACTIVE", "試跑已失去執行權，較晚回覆未覆寫原紀錄")
    else:
        stored.status = result["status"]
        stored.result = safe
    if result["status"] != "completed":
        invalidate_test_artifacts(
            db,
            user,
            assistant_id,
            run_id,
            result["status"],
            result.get("error", {}).get("type", "FLOW_FAILED"),
        )
    audit(
        db, user, "execute_finish", assistant_id, run_id=run_id, status=result["status"]
    )
    db.commit()


def _mask_flow_result(result):
    safe = mask(result)

    def strip_artifact_handles(state):
        if not isinstance(state, dict):
            return
        for artifact in state.get("artifacts", []):
            if isinstance(artifact, dict):
                for key in ("download_url", "storage_ref", "file_refs"):
                    artifact.pop(key, None)
        # These are flow state containers, not arbitrary retrieval/source data.
        for key in ("input", "output", "state"):
            strip_artifact_handles(state.get(key))

    def project(original, destination):
        if "completion" in original:
            destination["completion"] = normalize_completion(
                {"text": "", "completion": original["completion"]}
            )["completion"]
        if "model_completions" in original:
            destination["model_completions"] = [
                normalize_completion({"text": "", "completion": item})["completion"]
                for item in original["model_completions"]
            ]

    # Only validated server completion records bypass the generic token-key mask.
    # Arbitrary tool, retrieval and model content retain the existing mask.
    project(result, safe)
    strip_artifact_handles(safe)
    for original, destination in zip(result.get("steps", []), safe.get("steps", [])):
        project(original, destination)
        strip_artifact_handles(destination)
    return safe


def completion_envelope(completion):
    metadata = normalize_completion({"text": "", "completion": completion})[
        "completion"
    ]
    return {
        "completion": metadata,
        "finish_reason": metadata["finish_reason"],
        "usage": metadata["usage"],
        "usage_complete": metadata["usage_complete"],
    }


def public_result(run_id, result, artifacts):
    return {
        "id": run_id,
        **_mask_flow_result(
            {
                key: value
                for key, value in result.items()
                if key not in {"execution_context", "test_tracking"}
            }
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
    last_user = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], dict)
            and messages[index].get("role") == "user"
        ),
        None,
    )
    message = messages[last_user].get("content") if last_user is not None else None
    if not isinstance(message, str) or not message.strip() or len(message) > 16000:
        fail(422, "INVALID_MESSAGE", "請提供長度不超過 16000 字的文字訊息")
    history = []
    remaining = 32000
    for item in reversed(messages[:last_user]):
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, str):
            fail(
                422,
                "INVALID_HISTORY",
                "助手目前僅支援文字對話，請移除歷史訊息中的非文字內容",
            )
        if not content.strip():
            continue
        if len(history) >= 20 or remaining <= 0:
            break
        limit = min(8000, remaining)
        if len(content) > limit:
            marker = "[較早內容已省略]\n"
            if limit <= len(marker):
                break
            content = marker + content[-(limit - len(marker)) :]
        history.append({"role": item["role"], "content": content})
        remaining -= len(content)
    history.reverse()
    assistant_id, version_id, agent_name = version.assistant_id, version.id, agent.name

    async def execute(execution_db, execution_user, on_delta=None):
        return await run(
            execution_db,
            execution_user,
            assistant_id,
            version_id,
            message,
            api_key_id=api_key_id,
            task_ctx=task_ctx,
            conversation_id=conversation_id,
            dispatch_claims=dispatch_claims,
            history=history,
            on_delta=on_delta,
        )

    def finalize(status):
        if task_ctx:
            from app.services.proxy.task_link import finalize_task_run

            finalize_task_run(task_ctx.task_run_id, status)

    if body.get("stream"):
        from app.services.quick_stream_response import flow_stream_response

        response = flow_stream_response(
            bind=db.get_bind(),
            user_id=user.id,
            token_version=user.token_version,
            department_id=user.department_id,
            assistant_id=assistant_id,
            model_name=agent_name,
            execute=execute,
            finalize=finalize,
        )
        # 身分與版本已捕成值；串流 Session 接手前先歸還請求連線。
        db.commit()
        return response
    try:
        result = await execute(db, user)
    except BaseException:
        finalize("failed")
        raise
    finalize("completed" if result["status"] == "completed" else "failed")
    if result["status"] != "completed":
        if result.get("completion"):
            detail = result.get("error") or {}
            raise HTTPException(
                status_code=502,
                detail={
                    "type": detail.get("type", "FLOW_FAILED"),
                    "message": detail.get("message", "助手流程未能完成（FLOW_FAILED）"),
                    "answer_complete": False,
                    "completion": completion_envelope(result["completion"])[
                        "completion"
                    ],
                },
            )
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
    terminal_meta = (
        completion_envelope(result["completion"]) if result.get("completion") else None
    )
    return JSONResponse(
        {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": agent_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": (terminal_meta["finish_reason"] or "stop")
                    if terminal_meta
                    else "stop",
                }
            ],
            **(
                {"anila_meta": terminal_meta, "usage": terminal_meta["usage"]}
                if terminal_meta
                else {}
            ),
        }
    )
