"""Shared helpers for the ``app.api.agents`` package.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor). Only helpers used by 2+ submodules
live here.
"""
from fastapi import Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from anila_contracts import AgentManifest as CanonicalAgentManifest
from app.models.agent import Agent
from app.models.user import User
# The CSP-local manifest remains only as an input compatibility adapter for
# pre-Gate-5 registrations.  Readiness never parses it as canonical authority.
from app.schemas.contracts.agents import AgentManifest as LegacyAgentManifest
from app.services.auth_service import get_current_user


def validate_agent_manifest(payload: dict) -> dict:
    """Validate a submitted Agent manifest against the doc 05 §4 contract.

    Fail-closed: unknown fields / wrong types / invalid enum values (e.g. a
    non-五級 classification) raise ``422`` with a zh-TW detail. Returns the
    normalized manifest dict (JSON-mode) suitable for ``Agent.manifest_json``.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Agent manifest 驗證失敗:必須是 JSON object")
    # Canonical Gate-5 payloads are never downgraded to the legacy adapter if
    # they contain any v2-only key.  This prevents a malformed new manifest
    # from accidentally becoming a permissive legacy row.
    canonical_hint = {
        "schema_version",
        "event_protocols",
        "required_scopes",
        "full_trace_required",
        "supports_streaming",
    }
    errors: list[ValidationError] = []
    try:
        manifest = CanonicalAgentManifest.model_validate(payload)
    except ValidationError as canonical_exc:
        errors.append(canonical_exc)
        if canonical_hint.intersection(payload):
            manifest = None
        else:
            try:
                manifest = LegacyAgentManifest.model_validate(payload)
            except ValidationError as legacy_exc:
                errors.append(legacy_exc)
                manifest = None
    if manifest is None:
        exc = errors[-1] if errors else ValidationError.from_exception_data(
            "AgentManifest", []
        )
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "(root)"
        msg = first.get("msg", "格式不符")
        raise HTTPException(
            status_code=422,
            detail=f"Agent manifest 驗證失敗:欄位「{loc}」{msg}",
        ) from exc
    return manifest.model_dump(mode="json")


def _require_developer_or_admin(current_user: User = Depends(get_current_user)) -> User:
    # Sprint owner-tier: owner 是 admin 之上的最高權限,凡 admin 能做的事 owner 也能做。
    # 漏掉 owner 會讓系統最高權限者反而拿不到開發者頁面 (download template、register
    # agent 等),這是 RBAC 新增層級時典型的回歸坑。和 auth_service._ADMIN_TIER_ROLES
    # 保持一致。
    if current_user.role not in ("admin", "developer", "owner"):
        raise HTTPException(status_code=403, detail="需要開發者或管理員權限")
    return current_user


def _client_ip(request: Request | None) -> str | None:
    """Extract caller IP from the Request — tolerant of reverse-proxy
    setups (reads X-Forwarded-For first hop) and of None so endpoints
    that don't inject a Request stay safe."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _resolve_agent(db: Session, agent_id: int) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent
