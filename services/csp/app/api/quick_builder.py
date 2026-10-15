"""沿用 ANILA 身分的助手快製 API。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import (
    QuickAssistant,
    QuickRun,
    QuickTemplate,
    QuickVersion,
    QuickCollectionPublication,
)
from app.models.user import User
from app.schemas.quick_builder import (
    QuickBuilderRoute,
    AssistantWrite,
    Restore,
    Review,
    TemplateWrite,
    TestInput,
    VersionRef,
    VersionWrite,
)
from app.services import quick_builder as service
from app.services.auth_service import get_current_user, is_admin_tier, require_admin
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

router = APIRouter(
    prefix="/api/quick-builder", tags=["助手快製"], route_class=QuickBuilderRoute
)


@router.get("")
def index(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {
        "assistants": [
            service.to_out(db, user, row, summary=True)
            for row in db.query(QuickAssistant).order_by(QuickAssistant.id.desc()).all()
            if service.visible(db, user, row)
        ]
    }


@router.post("", status_code=201)
def create(
    body: AssistantWrite,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(db, user, service.create(db, user, body))


@router.get("/catalog")
def catalog(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.api_key_service import check_model_permission
    from app.services.quick_mcp import list_tools

    return {
        "actor_user_id": user.id,
        "models": [
            {"id": m.id, "name": m.name, "display_name": m.display_name}
            for m in db.query(ModelRegistry)
            .filter(ModelRegistry.is_active.is_(True))
            .all()
            if m.name != "anila-router"
            and check_model_permission(db, user=user, api_key_id=None, model_id=m.id)
        ],
        "collections": [
            {
                "id": c.id,
                "name": c.name,
                "department_id": c.department_id,
                "publication_scope": p.scope
                if (p := db.get(QuickCollectionPublication, c.id))
                else "unit",
                "target_department_ids": p.target_department_ids if p else [],
            }
            for c in db.query(IngestionCollection).filter_by(status="active").all()
            if service.collection_usable(db, user, c)
        ],
        "mcp_tools": list_tools(db, user),
        "document_presets": [
            {"id": "deep_tech_review", "name": "深度技術綜述"},
            {"id": "key_summary", "name": "重點摘要"},
            {"id": "teaching_handout", "name": "教學講義"},
            {"id": "external_comms", "name": "對外溝通"},
        ],
        "document_formats": ["html", "pdf", "docx"],
        "can_manage_templates": is_admin_tier(user),
        "can_manage_readiness": is_admin_tier(user),
    }


class CollectionPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["unit", "public", "departments"]
    target_department_ids: list[int] = Field(default_factory=list, max_length=100)


@router.put("/collections/{collection_id}/publication")
def collection_publication(
    collection_id: int,
    body: CollectionPublication,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.models.department import Department
    from app.schemas.contracts.classification import (
        ClassificationLevel,
        outbound_action_allowed,
    )

    coll = db.get(IngestionCollection, collection_id)
    if coll is None:
        service.fail(404, "NOT_FOUND", "找不到這個文件集")
    if coll.department_id is None:
        service.fail(
            422,
            "SOURCE_DEPARTMENT_REQUIRED",
            "個人文件集不能直接公開，請先建立有單位歸屬的文件集",
        )
    if body.scope != "unit" and not outbound_action_allowed(
        ClassificationLevel.from_storage(coll.classification_level or "無機密")
    ):
        service.fail(
            403, "PUBLICATION_CLASSIFICATION", "這個文件集的密等不能開放跨單位使用"
        )
    ids = sorted(set(body.target_department_ids)) if body.scope == "departments" else []
    if (
        any(type(i) is not int or i <= 0 for i in ids)
        or (body.scope == "departments" and not ids)
        or (
            ids
            and db.query(Department)
            .filter(Department.id.in_(ids), Department.is_active.is_(True))
            .count()
            != len(ids)
        )
    ):
        service.fail(422, "INVALID_DEPARTMENT", "請選擇有效的開放單位")
    row = db.get(QuickCollectionPublication, collection_id)
    if row:
        row.scope, row.target_department_ids, row.approved_by = body.scope, ids, user.id
    else:
        db.add(
            QuickCollectionPublication(
                collection_id=collection_id,
                scope=body.scope,
                target_department_ids=ids,
                approved_by=user.id,
            )
        )
    service.audit(
        db,
        user,
        "collection_publication",
        None,
        collection_id=collection_id,
        scope=body.scope,
        target_department_ids=ids,
    )
    db.commit()
    return {
        "collection_id": collection_id,
        "scope": body.scope,
        "target_department_ids": ids,
    }


@router.get("/templates")
def templates(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.quick_templates import list_templates

    return {
        "templates": list_templates()
        + [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "graph": t.graph,
                "configuration_notes": [],
            }
            for t in db.query(QuickTemplate).order_by(QuickTemplate.id).all()
            if is_admin_tier(user) or _template_sources_public(db, t.graph)
        ]
    }


def _validate_template_sources(db, graph):
    from app.models.quick_mcp import QuickMCPService, QuickMCPTool

    collections, tools = service._graph_sources(graph)
    for collection_id in collections:
        collection = db.get(IngestionCollection, collection_id)
        publication = db.get(QuickCollectionPublication, collection_id)
        if (
            collection is None
            or collection.status != "active"
            or publication is None
            or publication.scope != "public"
        ):
            service.fail(
                422,
                "TEMPLATE_PRIVATE_SOURCE",
                "全院範本不能包含未公開的文件集，請先清除來源設定",
            )
    for tool_id in tools:
        tool = db.get(QuickMCPTool, tool_id)
        source = db.get(QuickMCPService, tool.service_id) if tool else None
        if (
            not source
            or not source.is_public
            or source.status != "approved"
            or not tool.is_approved
            or not tool.is_read_only
        ):
            service.fail(
                422,
                "TEMPLATE_PRIVATE_SOURCE",
                "全院範本不能包含未公開或未核准的查詢工具，請先清除工具設定",
            )


def _template_sources_public(db, graph):
    try:
        _validate_template_sources(db, graph)
    except HTTPException:
        return False
    return True


@router.post("/templates", status_code=201)
def create_template(
    body: TemplateWrite,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.services.quick_flow import validate_graph

    try:
        graph = validate_graph(body.graph)
    except ValueError as exc:
        service.fail(422, "INVALID_FLOW", str(exc))
    _validate_template_sources(db, graph)
    row = QuickTemplate(
        name=body.name, description=body.description, graph=graph, updated_by=user.id
    )
    db.add(row)
    db.flush()
    service.audit(db, user, "template_create", None, template_id=row.id)
    db.commit()
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "graph": row.graph,
    }


@router.put("/templates/{template_id}")
def update_template(
    template_id: int,
    body: TemplateWrite,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.services.quick_flow import validate_graph

    row = db.get(QuickTemplate, template_id)
    if row is None:
        service.fail(404, "NOT_FOUND", "找不到這個可維護範本")
    try:
        graph = validate_graph(body.graph)
    except ValueError as exc:
        service.fail(422, "INVALID_FLOW", str(exc))
    _validate_template_sources(db, graph)
    row.name, row.description, row.graph, row.updated_by = (
        body.name,
        body.description,
        graph,
        user.id,
    )
    service.audit(db, user, "template_update", None, template_id=row.id)
    db.commit()
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "graph": row.graph,
    }


def _run_access(db, user, run_id):
    run = db.get(QuickRun, run_id)
    if run is None or (run.user_id != user.id and not is_admin_tier(user)):
        service.fail(404, "NOT_FOUND", "找不到這筆執行紀錄")
    version = db.get(QuickVersion, run.version_id)
    assistant = service.load(db, user, version.assistant_id)
    service.validate_sources(db, user, version.payload, check_model_access=False)
    return run, version, assistant


@router.get("/runs/{run_id}")
def read_run(
    run_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    from app.services.quick_execution import public_result

    run, _, _ = _run_access(db, user, run_id)
    result = {k: v for k, v in run.result.items() if k != "artifacts"}
    return public_result(run.id, result, run.result.get("artifacts", []))


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
def download_artifact(
    run_id: int,
    artifact_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.schemas.contracts.classification import outbound_action_allowed
    from app.services.quick_execution import artifact_execution_level

    run, version, assistant = _run_access(db, user, run_id)
    artifacts = run.result.get("artifacts", [])
    if artifact_id < 0 or artifact_id >= len(artifacts):
        service.fail(404, "NOT_FOUND", "找不到這份產出文件")
    artifact = artifacts[artifact_id]
    if not outbound_action_allowed(
        artifact_execution_level(db, run, version, artifact)
    ):
        service.fail(403, "EXPORT_CLASSIFICATION", "這個密等的內容不能匯出文件")
    output_format = artifact.get("format", "rtf")
    if output_format not in {"rtf", "html", "pdf", "docx"}:
        service.fail(502, "ARTIFACT_FORMAT", "文件格式紀錄不完整，請重新執行助手")
    if artifact.get("artifact_id"):
        from app.api.artifacts import _ExportCaller, export_artifact
        from app.schemas.contracts.artifacts import ArtifactExportIn

        export_artifact(
            artifact["artifact_id"],
            ArtifactExportIn(
                artifact_version_id=artifact["artifact_version_id"],
                export_format=output_format,
                target_space="quick_builder",
            ),
            _ExportCaller(
                actor_type="user",
                actor_id=str(user.id),
                exporter_user_id=user.id,
                exporter_employee_id=user.username,
                is_admin=is_admin_tier(user),
            ),
            db,
        )
    if artifact.get("encoding") == "base64":
        import base64
        import binascii
        import hashlib

        try:
            content = base64.b64decode(artifact["content"], validate=True)
        except (ValueError, binascii.Error):
            service.fail(502, "ARTIFACT_CONTENT", "文件內容紀錄不完整，請重新執行助手")
        if hashlib.sha256(content).hexdigest() != artifact.get("content_hash"):
            service.fail(502, "ARTIFACT_CONTENT", "文件內容驗證失敗，請重新執行助手")
    else:
        content = artifact["content"].encode("ascii")
    service.audit(
        db,
        user,
        "artifact_download",
        assistant.id,
        run_id=run.id,
        artifact_id=artifact_id,
    )
    db.commit()
    return Response(
        content,
        media_type=artifact["media_type"],
        headers={
            "Content-Disposition": f"attachment; filename=assistant-draft.{output_format}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        },
    )


@router.post("/runtime/{version_id}/v1/chat/completions")
async def runtime_adapter(
    version_id: int, request: Request, db: Session = Depends(get_db)
):
    from app.services.quick_execution import agent_response
    from app.services.proxy.dispatch_token import (
        extract_bearer_token,
        verify_dispatch_token,
    )
    from app.services.api_key_service import check_agent_permission

    claims = verify_dispatch_token(
        extract_bearer_token(request.headers.get("authorization")), db
    )
    if not claims:
        service.fail(401, "INVALID_DISPATCH", "身分憑證無效或已過期")
    version = db.get(QuickVersion, version_id)
    if not version or version.agent_id != claims["agent_id"]:
        service.fail(403, "DISPATCH_TARGET", "身分憑證不適用這個助手")
    user = db.get(User, int(claims["user_id"]))
    agent = db.get(Agent, version.agent_id)
    if (
        not user
        or not user.is_active
        or not user.is_approved
        or not agent
        or not check_agent_permission(
            db, user=user, api_key_id=claims.get("api_key_id"), agent_id=agent.id
        )
    ):
        service.fail(403, "AGENT_DENIED", "目前沒有使用這個助手的權限")
    if claims.get("department") != user.department_id:
        service.fail(403, "IDENTITY_CHANGED", "所屬單位已變更，請重新登入")
    try:
        body = await request.json()
    except ValueError:
        service.fail(422, "INVALID_REQUEST", "對話內容不是有效的 JSON 格式")
    if not isinstance(body, dict):
        service.fail(422, "INVALID_REQUEST", "對話內容格式不正確")
    return await agent_response(
        db,
        user,
        agent,
        body,
        api_key_id=claims.get("api_key_id"),
        dispatch_claims=claims,
    )


@router.get("/{assistant_id}")
def detail(
    assistant_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(db, user, service.load(db, user, assistant_id))


@router.post("/{assistant_id}/versions", status_code=201)
def save(
    assistant_id: int,
    body: VersionWrite,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(db, user, service.save(db, user, assistant_id, body))


@router.post("/{assistant_id}/restore")
def restore(
    assistant_id: int,
    body: Restore,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(db, user, service.restore(db, user, assistant_id, body))


@router.get("/{assistant_id}/compare")
def compare(
    assistant_id: int,
    left: int = Query(gt=0),
    right: int = Query(gt=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = service.load(db, user, assistant_id)
    if row.department_id != user.department_id and not service.can_review(
        db, user, row
    ):
        service.fail(404, "NOT_FOUND", "找不到可比較的流程版本")
    a, b = service.version_for(db, row, left), service.version_for(db, row, right)
    return {
        "left": service.version_out(a),
        "right": service.version_out(b),
        "changes": [
            {"field": key, "before": a.payload.get(key), "after": b.payload.get(key)}
            for key in sorted(set(a.payload) | set(b.payload))
            if a.payload.get(key) != b.payload.get(key)
        ],
    }


@router.post("/{assistant_id}/submit")
def submit(
    assistant_id: int,
    body: VersionRef,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(
        db, user, service.submit(db, user, assistant_id, body.version_id)
    )


@router.post("/{assistant_id}/review")
def review(
    assistant_id: int,
    body: Review,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(db, user, service.review(db, user, assistant_id, body))


@router.post("/{assistant_id}/publish")
def publish(
    assistant_id: int,
    body: VersionRef,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(
        db, user, service.publish(db, user, assistant_id, body.version_id)
    )


@router.post("/{assistant_id}/disable")
def disable(
    assistant_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return service.to_out(db, user, service.disable(db, user, assistant_id))


@router.post("/{assistant_id}/test")
async def test(
    assistant_id: int,
    body: TestInput,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.quick_execution import run

    return await run(
        db, user, assistant_id, body.version_id, body.message, testing=True
    )
