"""Quick-builder → fixed Studio report layouts and the existing Artifact policy.

Addresses and identity are resolved on the server, never accepted from a graph.
The caller provides a synchronous before_send reauthorization callback, then
reauthorizes again after render_document awaits and registers the Artifact with
the latest effective classification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
import httpx
from jose import jwt

from app.models.quick_builder import QuickRun
from app.models.registered_service import RegisteredService
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.user import User
from app.schemas.contracts.classification import (
    ClassificationLevel,
    outbound_action_allowed,
)

PRESETS = {
    "deep_tech_review": "深度技術綜述",
    "key_summary": "重點摘要",
    "teaching_handout": "教學講義",
    "external_comms": "對外溝通文件",
}
MEDIA_TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


def _fail(status: int, kind: str, message: str):
    raise HTTPException(status, detail={"type": kind, "message": message})


def _level(value) -> ClassificationLevel:
    if isinstance(value, ClassificationLevel):
        return value
    return ClassificationLevel.from_storage(value)


def _owned_run(db, user, run_id: int):
    original_department = user.department_id
    current = db.query(User).filter_by(id=user.id).populate_existing().first()
    run = db.query(QuickRun).filter_by(id=run_id).populate_existing().first()
    if (
        not current
        or not current.is_active
        or not current.is_approved
        or current.department_id != original_department
    ):
        _fail(403, "IDENTITY_CHANGED", "提問者身分或所屬單位已變更")
    if not run or run.user_id != current.id:
        _fail(404, "RUN_NOT_FOUND", "找不到可產出文件的助手執行紀錄")
    return current, run


def _authorized_studio_service(
    db, user, level: ClassificationLevel, captured_department
):
    from app.services.access_control import can_access_service

    current = db.query(User).filter_by(id=user.id).populate_existing().first()
    service = (
        db.query(RegisteredService)
        .filter_by(slug="studio", is_active=True)
        .populate_existing()
        .first()
    )
    if (
        not current
        or not current.is_active
        or not current.is_approved
        or current.department_id != captured_department
    ):
        _fail(403, "IDENTITY_CHANGED", "提問者身分或所屬單位已變更")
    if service is None:
        _fail(
            503,
            "DOCUMENT_RENDERER_NOT_CONFIGURED",
            "請在治理中心登錄並啟用 studio 文件排版服務",
        )
    if not can_access_service(db, current, service, context_level=level.to_storage()):
        _fail(
            403,
            "DOCUMENT_RENDERER_DENIED",
            "目前沒有文件排版服務的使用權限，或內容超過服務密等上限",
        )
    return service


def _studio_origin(entry_url: str) -> str:
    """Resolve the registered UI entry to the fixed renderer API's origin.

    Same-origin /studio/... entries use the existing server-owned SITE_URL,
    never a request Host header, graph field, or response-supplied address.
    The resulting origin must still pass the ordinary private/trusted guard.
    """
    from app.config import settings

    def parts(raw):
        if (
            not isinstance(raw, str)
            or not raw
            or any(ord(char) <= 32 or ord(char) == 127 for char in raw)
            or "\\" in raw
        ):
            _fail(503, "DOCUMENT_RENDERER_ENDPOINT", "文件排版服務位址格式不正確")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
            host = parsed.hostname
        except ValueError:
            _fail(503, "DOCUMENT_RENDERER_ENDPOINT", "文件排版服務位址格式不正確")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            _fail(503, "DOCUMENT_RENDERER_ENDPOINT", "文件排版服務位址不能含帳密、參數或片段")
        return parsed, host, port

    parsed, host, port = parts(entry_url)
    if not parsed.scheme and not parsed.netloc:
        if (
            not re.fullmatch(r"/studio(?:/[A-Za-z0-9._~-]+)*/?", parsed.path)
            or any(segment in (".", "..") for segment in parsed.path.split("/"))
        ):
            _fail(503, "DOCUMENT_RENDERER_ENDPOINT", "同源文件服務入口必須是 /studio/ 路徑")
        parsed, host, port = parts(settings.SITE_URL)
        if parsed.path not in ("", "/"):
            _fail(503, "DOCUMENT_RENDERER_ENDPOINT", "平台站台位址必須是完整的 HTTP 或 HTTPS origin")
    if parsed.scheme not in ("http", "https") or not host:
        _fail(503, "DOCUMENT_RENDERER_ENDPOINT", "文件排版服務需要完整的院內 HTTP 或 HTTPS origin")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    # UI entry paths are deliberately not a backend API prefix. Both a direct
    # Studio origin and platform ingress expose the same fixed root API.
    return urlunsplit((parsed.scheme, authority, "", "", ""))


async def resolve_studio_url(db, user, classification_level) -> str:
    from app.services.quick_mcp import _guard_endpoint

    level = _level(classification_level)
    captured_department = user.department_id
    endpoint = _authorized_studio_service(
        db, user, level, captured_department
    ).entry_url
    origin = _studio_origin(endpoint)
    try:
        # Existing guard requires an explicit trusted host and private intranet
        # addresses, rejecting external addresses, credentials, query/fragment.
        await asyncio.wait_for(asyncio.to_thread(_guard_endpoint, origin), timeout=5)
    except HTTPException:
        _fail(
            503, "DOCUMENT_RENDERER_ENDPOINT", "文件排版服務位址未通過院內信任主機驗證"
        )
    except TimeoutError:
        _fail(504, "DOCUMENT_RENDERER_ENDPOINT_TIMEOUT", "文件排版服務位址驗證逾時")
    current_entry = _authorized_studio_service(
        db, user, level, captured_department
    ).entry_url
    if current_entry != endpoint or _studio_origin(current_entry) != origin:
        _fail(409, "DOCUMENT_RENDERER_CHANGED", "文件排版服務設定已變更，請重試")
    return origin


def _request_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _render_token(db, user, payload: dict, level: ClassificationLevel) -> str:
    from app.services.studio_job_token import build_studio_job_claims
    from app.services.jwt_keyring import active_signing_material

    now = datetime.now(timezone.utc)
    claims = build_studio_job_claims(
        user_id=user.id,
        job_id=payload["job_id"],
        classification=level.to_storage(),
        token_version=int(user.token_version or 0),
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
    )
    # Dedicated type/audience prevent this render-only credential from being
    # accepted as an interactive user or a general Studio data-plane job.
    claims.update(
        type="studio_quick_document",
        aud="studio-quick-document",
        purpose="quick_document",
        request_sha256=_request_hash(payload),
    )
    material = active_signing_material(db)
    return jwt.encode(
        claims,
        material.private_pem,
        algorithm="RS256",
        headers={"kid": material.kid, "typ": "JWT"},
    )


async def render_document(
    db,
    user,
    *,
    studio_url: str,
    run_id: int,
    text: str,
    preset: str,
    output_format: str,
    classification_level,
    before_send: Callable[[], tuple[User, str]],
    title: str = "助手草稿",
) -> dict:
    """Return bytes + trusted metadata; the caller stores bytes in QuickRun.

    No response-supplied URL or filename is followed. Only an identity-encoded,
    size-bounded response from the exact registered service is accepted.
    before_send must synchronously validate the caller's current grants and
    task/conversation context, returning the actual user and effective level.
    """
    captured_user_id, captured_department = user.id, user.department_id
    current, _ = _owned_run(db, user, run_id)
    level = _level(classification_level)
    if not outbound_action_allowed(level):
        _fail(403, "EXPORT_CLASSIFICATION", "這個密等的內容不能匯出文件")
    if preset not in PRESETS or output_format not in MEDIA_TYPES:
        _fail(422, "DOCUMENT_CONFIG", "請選擇既有 Studio 範本及 HTML、PDF 或 DOCX 格式")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > 100_000
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 200
    ):
        _fail(422, "DOCUMENT_INPUT", "文件標題或內容未填寫，或超過長度限制")
    approved_url = await resolve_studio_url(db, current, level)
    if studio_url.rstrip("/") != approved_url:
        _fail(409, "DOCUMENT_RENDERER_CHANGED", "文件排版服務設定已變更，請重試")
    # DNS validation is the last preflight await. The caller must synchronously
    # recheck its task/conversation/model/agent/source grants immediately after it.
    # Re-read service ACL/ceiling with the new level before signing or sending.
    current, latest_level = before_send()
    if current.id != captured_user_id:
        _fail(403, "IDENTITY_CHANGED", "文件提問者身分已變更")
    level = max(level, _level(latest_level))
    if not outbound_action_allowed(level):
        _fail(403, "EXPORT_CLASSIFICATION", "這個密等的內容不能匯出文件")
    service = _authorized_studio_service(db, current, level, captured_department)
    if _studio_origin(service.entry_url) != approved_url:
        _fail(409, "DOCUMENT_RENDERER_CHANGED", "文件排版服務設定已變更，請重試")
    job_id = f"quick-{run_id}-{uuid.uuid4().hex}"
    payload = {
        "job_id": job_id,
        "preset": preset,
        "format": output_format,
        "title": title,
        "text": text,
    }
    token = _render_token(db, current, payload, level)
    db.commit()  # release DB connection while the renderer is working
    try:
        async with asyncio.timeout(65):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    approved_url + "/api/quick-documents/render",
                    json=payload,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code != 200:
                        _fail(
                            502, "DOCUMENT_RENDERER_FAILED", "文件排版服務未能完成工作"
                        )
                    if response.headers.get(
                        "content-encoding", "identity"
                    ).lower() not in ("", "identity"):
                        _fail(502, "DOCUMENT_ENCODING", "文件排版服務不可回傳壓縮資料")
                    if (
                        response.headers.get("content-type", "")
                        .split(";", 1)[0]
                        .strip()
                        != MEDIA_TYPES[output_format]
                    ):
                        _fail(502, "DOCUMENT_RESPONSE", "文件排版服務回傳的格式不符")
                    if response.headers.get("x-anila-job-id") != job_id:
                        _fail(
                            502, "DOCUMENT_RESPONSE", "文件排版服務回傳的工作編號不符"
                        )
                    length = response.headers.get("content-length")
                    if length is not None and (
                        not length.isdigit() or int(length) > MAX_DOCUMENT_BYTES
                    ):
                        _fail(502, "DOCUMENT_TOO_LARGE", "文件超過大小限制")
                    content = bytearray()
                    async for chunk in response.aiter_raw():
                        content.extend(chunk)
                        if len(content) > MAX_DOCUMENT_BYTES:
                            _fail(502, "DOCUMENT_TOO_LARGE", "文件超過大小限制")
                    digest = hashlib.sha256(content).hexdigest()
                    if not content or digest != response.headers.get(
                        "x-content-sha256"
                    ):
                        _fail(502, "DOCUMENT_RESPONSE", "文件內容驗證失敗")
    except (httpx.HTTPError, TimeoutError):
        _fail(
            502, "DOCUMENT_RENDERER_UNAVAILABLE", "文件排版服務暫時無法使用，請稍後重試"
        )
    current, _ = _owned_run(db, user, run_id)
    if await resolve_studio_url(db, current, level) != approved_url:
        _fail(409, "DOCUMENT_RENDERER_CHANGED", "文件排版服務設定已變更，請重試")
    return {
        "content": bytes(content),
        "filename": f"assistant-draft.{output_format}",
        "media_type": MEDIA_TYPES[output_format],
        "content_hash": digest,
        "job_id": job_id,
        "preset": preset,
        "format": output_format,
        "title": title,
    }


def register_document_artifact(
    db,
    user,
    *,
    run_id: int,
    rendered: dict,
    classification_level,
    task_id: int | None = None,
    source_snapshot_id: int | None = None,
    agent_id: int | None = None,
    model_id: int | None = None,
    artifact_index: int = 0,
) -> dict:
    """Register under the actual requester and invoke the existing export gate."""
    from app.modules import artifacts, policy
    from app.schemas.contracts.artifacts import ArtifactIn, ArtifactExportIn
    from app.api.artifacts import _ExportCaller, export_artifact
    from app.services.auth_service import is_admin_tier

    current, _ = _owned_run(db, user, run_id)
    level = _level(classification_level)
    snapshot = None
    if source_snapshot_id is not None:
        snapshot = (
            db.query(SourceSnapshot)
            .filter_by(id=source_snapshot_id)
            .populate_existing()
            .first()
        )
        if not snapshot:
            _fail(404, "DOCUMENT_CONTEXT", "找不到文件來源快照")
        if task_id is not None and snapshot.task_id != task_id:
            _fail(403, "DOCUMENT_CONTEXT", "文件來源快照不屬於這個任務")
        task_id = snapshot.task_id
        level = max(level, _level(snapshot.classification_level))
    task = (
        db.query(Task).filter_by(id=task_id).populate_existing().first()
        if task_id
        else None
    )
    if task_id and (not task or task.requester_user_id != current.id):
        _fail(403, "DOCUMENT_CONTEXT", "文件任務不屬於實際提問者")
    if task:
        level = max(level, _level(task.classification_level))
    if not outbound_action_allowed(level):
        _fail(403, "EXPORT_CLASSIFICATION", "這個密等的內容不能匯出文件")
    content = rendered.get("content")
    if (
        not isinstance(content, bytes)
        or not content
        or len(content) > MAX_DOCUMENT_BYTES
        or hashlib.sha256(content).hexdigest() != rendered.get("content_hash")
    ):
        _fail(422, "DOCUMENT_RESPONSE", "文件產物不完整或驗證失敗")
    if not task:
        task = Task(
            title=rendered["title"],
            task_type="generate_artifact",
            requester_user_id=current.id,
            department_id=current.department_id,
            classification_level=level.to_storage(),
            requested_output_type="report",
            status="completed",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
    url = f"/api/quick-builder/runs/{run_id}/artifacts/{artifact_index}"
    payload = ArtifactIn(
        artifact_type="report",
        title=rendered["title"],
        storage_ref=url,
        task_id=task.id,
        source_snapshot_id=source_snapshot_id,
        job_id=rendered["job_id"],
        content_hash=rendered["content_hash"],
        metadata={
            "quick_run_id": run_id,
            "preset": rendered["preset"],
            "format": rendered["format"],
        },
    )
    artifact = artifacts.create_artifact(
        db,
        payload=payload,
        owner_user_id=current.id,
        source_task_id=task.id,
        source_snapshot_id=source_snapshot_id,
        trace_id=task.trace_id,
        initial_level=ClassificationLevel.UNCLASSIFIED,
    )
    policy.apply_classification(
        db,
        resource_type="artifact",
        resource_id=str(artifact.id),
        new_level=level.to_storage(),
        actor_type="user",
        actor_id=str(current.id),
        reason="source_selected",
        task_id=task.id,
        source="quick_document",
    )
    db.refresh(artifact)
    version = artifacts.create_version(
        db,
        artifact=artifact,
        storage_ref=url,
        content_hash=rendered["content_hash"],
        file_refs=[url],
        citation_map=None,
        generated_by_model_id=model_id,
        generated_by_agent_id=agent_id,
        generated_by_studio_job_id=rendered["job_id"],
        level=_level(artifact.classification_level),
    )
    exported = export_artifact(
        artifact.id,
        ArtifactExportIn(
            artifact_version_id=version.id,
            export_format=rendered["format"],
            target_space="quick_builder",
        ),
        _ExportCaller(
            actor_type="user",
            actor_id=str(current.id),
            exporter_user_id=current.id,
            exporter_employee_id=current.username,
            is_admin=is_admin_tier(current),
        ),
        db,
    )
    return {
        "artifact_id": artifact.id,
        "artifact_version_id": version.id,
        "export_id": exported.export_id,
        "task_id": task.id,
        "classification_level": artifact.classification_level,
        "download_url": url,
    }
