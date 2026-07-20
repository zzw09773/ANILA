"""Build the CSP authority projection consumed by Router services."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from anila_contracts import Classification
from sqlalchemy.orm import Session

from app.config import settings
from app.models.agent import Agent, UserAgentPermission
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.schemas.agent_registry import AgentRegistrySnapshot
from app.services.agent_readiness import evaluate_agent_readiness
from app.services.auth_service import is_admin_tier


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _caller(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active or not user.is_approved:
        raise ValueError("registry caller user is unavailable")
    return user


def visible_agents(db: Session, *, user_id: int) -> list[Agent]:
    """Apply the same caller-scoped ACL as legacy ``/v1/agents``."""

    user = _caller(db, user_id)
    query = db.query(Agent)
    if is_admin_tier(user):
        return cast(list[Agent], query.order_by(Agent.id.asc()).all())
    return cast(
        list[Agent],
        query.join(UserAgentPermission, UserAgentPermission.agent_id == Agent.id)
        .filter(UserAgentPermission.user_id == user.id)
        .order_by(Agent.id.asc())
        .all(),
    )


def _model_projection(model: ModelRegistry | None) -> dict[str, Any] | None:
    if model is None:
        return None
    # Do not add endpoint_url here.  Router receives only a CSP-owned opaque
    # target and must call the CSP proxy, never a raw Agent endpoint.
    return {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "is_active": bool(model.is_active),
        "health_status": model.health_status or "unknown",
        "health_checked_at": _iso(model.health_checked_at),
        "classification_ceiling": model.classification_ceiling or "",
    }


def _current_base_model(agent: Agent, db: Session) -> ModelRegistry | None:
    model_id = getattr(agent, "base_model_id", None)
    if model_id is None:
        return None
    return (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == model_id)
        .populate_existing()
        .one_or_none()
    )


def _effective_classification_ceiling(
    agent: Agent, *, manifest: Any, model: ModelRegistry | None
) -> str:
    """Project the lowest CSP/manifest/model ceiling, never the loosest one."""

    levels = []
    candidates = [getattr(agent, "classification_ceiling", None)]
    if manifest is not None:
        candidates.append(getattr(getattr(manifest, "classification", None), "ceiling", None))
    if model is not None:
        candidates.append(getattr(model, "classification_ceiling", None))
    for raw in candidates:
        value = getattr(raw, "value", raw)
        try:
            levels.append(Classification.from_storage(str(value)))
        except (TypeError, ValueError):
            continue
    if not levels:
        return ""
    return cast(str, min(levels, key=lambda level: level.rank).to_storage())


def _entry(agent: Agent, *, db: Session, now: datetime) -> dict[str, Any]:
    readiness = evaluate_agent_readiness(agent, db=db, now=now)
    base_model = _current_base_model(agent, db)
    manifest = readiness.manifest
    readiness_reasons = set(readiness.reason_codes)
    trace_passed = (
        isinstance(getattr(agent, "trace_test_report", None), dict)
        and getattr(agent, "trace_test_report", {}).get("passed") is True
        and "TRACE_TEST_MISSING" not in readiness_reasons
        and "TRACE_TEST_STALE" not in readiness_reasons
    )
    return {
        "agent_id": agent.name,
        "registry_id": agent.id,
        "name": agent.name,
        "is_active": bool(getattr(agent, "is_active", True)),
        "manifest": readiness.manifest_json if readiness.manifest is not None else None,
        "manifest_sha256": getattr(agent, "manifest_sha256", None) or "",
        "manifest_revision": getattr(agent, "manifest_revision", None) or "",
        "approval_status": agent.approval_status or "unknown",
        "health_status": agent.health_status or "unknown",
        "health_checked_at": _iso(getattr(agent, "health_checked_at", None)),
        "audit_level": getattr(agent, "audit_level", None) or "",
        "trace_callback_mode": getattr(agent, "trace_callback_mode", None) or "",
        "trace_test_passed_at": _iso(getattr(agent, "trace_test_passed_at", None)),
        "trace_test_governance_fingerprint": getattr(
            agent, "trace_test_governance_fingerprint", None
        ) or "",
        "classification_ceiling": _effective_classification_ceiling(
            agent, manifest=manifest, model=base_model
        ),
        "default_classification_level": getattr(
            agent, "default_classification_level", None
        ) or "",
        "base_model": _model_projection(base_model),
        "ready_for_dispatch": readiness.ready_for_dispatch,
        "approved": agent.approval_status == "approved",
        "health_ready": (
            agent.health_status == "healthy"
            and "HEALTH_STALE" not in readiness_reasons
        ),
        "trace_test_passed": trace_passed,
        "manifest_valid": manifest is not None and not any(
            code.startswith("MANIFEST_") for code in readiness_reasons
        ),
        "endpoint_via_csp": (
            manifest is not None
            and manifest.model_binding is not None
            and manifest.model_binding.gateway == "csp"
        ),
        # Only the explicit human security-review gate is waiting for an
        # approval action.  Unhealthy/stale/invalid/rejected rows need a
        # technical remediation, not a misleading "approve" badge.
        "approval_required": agent.approval_status == "pending_security_review",
        "required_obligations": (
            ["FULL_TRACE"] if manifest is not None and manifest.full_trace_required else []
        ),
        "model_gateway": (
            manifest.model_binding.gateway
            if manifest is not None and manifest.model_binding is not None
            else ""
        ),
        "reason_codes": readiness.reasons,
    }


def build_registry_snapshot(
    db: Session,
    *,
    user_id: int,
    now: datetime | None = None,
) -> AgentRegistrySnapshot:
    """Return a deterministic, caller-scoped snapshot with an explicit TTL."""

    fetched = now or datetime.now(timezone.utc)
    if fetched.tzinfo is None or fetched.utcoffset() is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    agents = [_entry(agent, db=db, now=fetched) for agent in visible_agents(db, user_id=user_id)]
    # Hash stable governance material only.  Probe timestamps and freshness
    # derived reason codes are delivery-time evidence; including them would
    # churn the generation on every health poll and create avoidable Router
    # snapshot 409s.  Health state, manifest/evidence identity and approval
    # state remain in the hash, so real governance changes still replan.
    stable_agents = []
    for entry in agents:
        stable_base_model = entry["base_model"]
        if stable_base_model is not None:
            stable_base_model = dict(stable_base_model)
            stable_base_model.pop("health_checked_at", None)
        stable_agents.append(
            {
                "agent_id": entry["agent_id"],
                "registry_id": entry["registry_id"],
                "name": entry["name"],
                "is_active": entry["is_active"],
                "manifest": entry["manifest"],
                "manifest_sha256": entry["manifest_sha256"],
                "manifest_revision": entry["manifest_revision"],
                "approval_status": entry["approval_status"],
                "health_status": entry["health_status"],
                "audit_level": entry["audit_level"],
                "trace_callback_mode": entry["trace_callback_mode"],
                "trace_test_passed": entry["trace_test_passed"],
                "trace_test_governance_fingerprint": entry[
                    "trace_test_governance_fingerprint"
                ],
                "classification_ceiling": entry["classification_ceiling"],
                "default_classification_level": entry[
                    "default_classification_level"
                ],
                "base_model": stable_base_model,
                "manifest_valid": entry["manifest_valid"],
                "endpoint_via_csp": entry["endpoint_via_csp"],
                "model_gateway": entry["model_gateway"],
            }
        )
    material = {"schema_version": "agent-registry/v1", "agents": stable_agents}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    # The snapshot id is a stable generation identifier; entry IDs are added
    # only after hashing so they do not make the hash recursively depend on
    # itself.
    for entry in agents:
        entry["snapshot_id"] = revision
    return AgentRegistrySnapshot(
        schema_version="agent-registry/v1",
        snapshot_id=revision,
        snapshot_revision=revision,
        snapshot_hash=revision,
        registry_snapshot_id=revision,
        fetched_at=fetched,
        expires_at=fetched + timedelta(seconds=settings.AGENT_REGISTRY_SNAPSHOT_TTL_SECONDS),
        caller_user_id=user_id,
        agents=agents,
    )


def registry_snapshot_revision(
    db: Session, *, user_id: int, now: datetime | None = None
) -> str:
    return cast(str, build_registry_snapshot(db, user_id=user_id, now=now).snapshot_revision)


__all__ = ["build_registry_snapshot", "registry_snapshot_revision", "visible_agents"]
