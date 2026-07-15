#!/usr/bin/env python3
"""Disposable CSP-side seed for ``gate5-silver-e2e.sh``.

The script is mounted only in the dev Compose stack's Gate 5 profile.  It uses
the CSP SQLAlchemy session to create disposable authority inputs (user,
session family, named Router client, model, Agent, credential, task, source
snapshot and policy), but it never creates an ExecutionGrant.  The harness
obtains that grant through CSP's formal HTTP mint endpoint afterwards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import urllib.request
from datetime import datetime, timedelta, timezone

from anila_contracts import Classification


def _db():
    from app.database import SessionLocal

    return SessionLocal()


def seed_model() -> None:
    from app.models import ModelRegistry

    name = os.environ["E2E_MODEL_TEMP_NAME"]
    db = _db()
    try:
        model = db.query(ModelRegistry).filter(ModelRegistry.name == name).one_or_none()
        if model is None:
            model = ModelRegistry(
                name=name,
                display_name="Gate 5 disposable model",
                model_type="llm",
                endpoint_url="http://gate5-e2e-model:8105",
                api_version="v1",
                protocol="openai_compatible",
                is_active=True,
                health_status="healthy",
                health_checked_at=datetime.now(timezone.utc),
                classification_ceiling=Classification.UNCLASSIFIED.to_storage(),
                supports_streaming=True,
                supports_json_schema=False,
                supports_tools=True,
                is_internal=True,
            )
            db.add(model)
            db.flush()
        # Agent admission requires a numeric ModelBinding, while CSP resolves
        # model calls by name.  Numeric names make both contracts agree.
        model.name = str(model.id)
        model.endpoint_url = "http://gate5-e2e-model:8105"
        model.display_name = "Gate 5 disposable model"
        model.model_type = "llm"
        model.is_active = True
        model.health_status = "healthy"
        model.health_checked_at = datetime.now(timezone.utc)
        model.classification_ceiling = Classification.UNCLASSIFIED.to_storage()
        model.supports_streaming = True
        model.supports_tools = True
        model.is_internal = True
        db.commit()
        print(model.id)
    finally:
        db.close()


def seed_authority() -> None:
    from app.models import (
        Agent,
        AgentCredential,
        AuthRefreshToken,
        AuthSession,
        ModelRegistry,
        PolicyDecision,
        ServiceClient,
        SourceSnapshot,
        Task,
        TaskRun,
        User,
        UserAgentPermission,
        UserModelPermission,
    )
    from app.services.agent_readiness import (
        governance_fingerprint,
        manifest_revision,
        manifest_sha256,
    )
    from app.services.agent_registry import build_registry_snapshot
    from app.services.service_token_envelope import (
        compute_lookup_hash,
        encode_service_token_envelope,
    )
    from app.config import settings
    from app.utils.security import create_access_token

    agent_name = os.environ["E2E_AGENT_NAME"]
    agent_token = os.environ["E2E_AGENT_TOKEN"]
    router_token = os.environ["E2E_ROUTER_TOKEN"]
    model_id = int(os.environ["E2E_MODEL_ID"])
    with urllib.request.urlopen(
        "http://anila-agent:8200/.well-known/anila-agent.json", timeout=10
    ) as response:
        manifest = json.loads(response.read().decode("utf-8"))
    if manifest.get("agent_id") != agent_name:
        raise RuntimeError("Agent manifest identity mismatch")
    binding = manifest.get("model_binding") or {}
    if binding.get("gateway") != "csp" or int(binding.get("model_id", -1)) != model_id:
        raise RuntimeError("Agent manifest model binding did not resolve to seeded CSP model")

    # AuthAssurance is serialized as an integer NumericDate by CSP; keep the
    # durable session timestamp at second precision so the mint request's
    # canonical value compares exactly after the round trip.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    nonce = secrets.token_hex(12)
    username = f"gate5-e2e-{nonce}"
    sid = f"gate5-sid-{secrets.token_urlsafe(32)}"
    family = f"gate5-family-{secrets.token_urlsafe(32)}"
    refresh_jti_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    trace_id = f"gate5-trace-{nonce}"
    session_id = f"gate5-session-{nonce}"
    invocation_id = f"gate5-invocation-{nonce}"
    route_id = f"gate5-route-{nonce}"
    policy_id = f"pg-{route_id}"
    unclassified = Classification.UNCLASSIFIED.to_storage()

    db = _db()
    try:
        model = db.get(ModelRegistry, model_id)
        if model is None:
            raise RuntimeError("seeded model disappeared")
        user = User(
            username=username,
            email=f"{username}@e2e.invalid",
            hashed_password="gate5-e2e-password-not-used",
            role="user",
            is_active=True,
            is_approved=True,
        )
        db.add(user)
        db.flush()
        db.add(UserModelPermission(user_id=user.id, model_id=model.id))
        # Flush the parent explicitly: these two models do not declare an
        # ORM relationship, so SQLAlchemy cannot otherwise guarantee that
        # the AuthSession INSERT precedes the refresh-token FK INSERT.
        auth_session = AuthSession(
            sid=sid,
            user_id=user.id,
            refresh_family_id=family,
            amr_json=json.dumps(["pwd"], separators=(",", ":")),
            acr="urn:anila:acr:password",
            auth_time=now,
            break_glass=False,
        )
        db.add(auth_session)
        db.flush()
        db.add(
            AuthRefreshToken(
                jti_hash=refresh_jti_hash,
                sid=sid,
                generation=0,
                issued_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
        client = ServiceClient(
            client_name=f"gate5-router-{nonce}",
            client_type="router",
            description="disposable Gate 5 Silver E2E Router",
            service_token_envelope=encode_service_token_envelope(router_token),
            service_token_lookup_hash=compute_lookup_hash(router_token),
            is_legacy=False,
            is_active=True,
        )
        db.add(client)
        db.flush()

        agent = Agent(
            name=agent_name,
            owner_user_id=user.id,
            base_model_id=model.id,
            endpoint_url="http://anila-agent:8200",
            manifest_url="http://anila-agent:8200/.well-known/anila-agent.json",
            healthcheck_url="http://anila-agent:8200/ready",
            api_version=manifest["api_version"],
            description_for_router=manifest["description_for_router"],
            runtime_type=manifest["runtime_type"],
            agent_version=manifest["version"],
            supported_task_types=manifest["supported_task_types"],
            input_schema=manifest["input_schema"],
            output_schema=manifest["output_schema"],
            capabilities=manifest["capabilities"],
            manifest_json=manifest,
            trace_callback_mode="sse_and_post",
            health_status="healthy",
            health_checked_at=now,
            approval_status="approved",
            audit_level="full_trace",
            classification_ceiling=manifest["classification"]["ceiling"],
            default_classification_level=manifest["classification"]["default"],
            requires_encryption=False,
            approved_by=user.id,
            approved_at=now,
        )
        db.add(agent)
        db.flush()
        digest = manifest_sha256(manifest)
        agent.manifest_sha256 = digest
        agent.manifest_revision = manifest_revision(manifest)
        fingerprint = governance_fingerprint(agent, base_model=model)
        agent.trace_test_governance_fingerprint = fingerprint
        agent.trace_test_passed_at = now
        agent.trace_test_report = {
            "passed": True,
            "checked_at": now.isoformat(),
            "governance_fingerprint": fingerprint,
            "harness": "gate5-silver-e2e",
        }
        db.add(
            AgentCredential(
                agent_id=agent.id,
                label="gate5-e2e",
                service_token_envelope=encode_service_token_envelope(agent_token),
                service_token_lookup_hash=compute_lookup_hash(agent_token),
                is_legacy=False,
                is_active=True,
            )
        )
        db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))

        public_access_token = create_access_token(
            {
                "sub": str(user.id),
                "username": user.username,
                "role": user.role,
                "tv": user.token_version or 0,
                "sid": sid,
                "iat": int(now.timestamp()),
                "iss": settings.JWT_ISSUER,
                "aud": settings.JWT_AUDIENCE,
                "amr": ["pwd"],
                "acr": "urn:anila:acr:password",
                "auth_time": int(now.timestamp()),
                "break_glass": False,
                "jti": secrets.token_urlsafe(32),
            }
        )

        task = Task(
            title="Gate 5 disposable durable resume",
            task_type="query",
            requester_user_id=user.id,
            status="running",
            source_scope="none",
            selected_collection_ids=[],
            requested_output_type="answer",
            legacy_runtime_call=False,
            classification_level=unclassified,
            trace_id=trace_id,
        )
        db.add(task)
        db.flush()
        source = SourceSnapshot(
            task_id=task.id,
            origin="none",
            source_scope="none",
            collection_ids=[],
            document_ids=[],
            chunk_ids=[],
            retrieval_queries=[],
            classification_level=unclassified,
        )
        db.add(source)
        db.flush()
        policy = PolicyDecision(
            task_id=task.id,
            actor_type="user",
            actor_id=user.id,
            action="agent.invoke",
            resource_type="agent",
            resource_id=agent_name,
            decision="allow",
            reason="Gate 5 disposable E2E allow",
            matched_policy_ids=["gate5-e2e"],
            policy_version="gate5-e2e/v1",
            metadata_json={"harness": "gate5-silver-e2e"},
            classification_level=unclassified,
        )
        db.add(policy)
        db.flush()
        task.source_snapshot_id = source.id
        task.policy_decision_id = policy.id
        run = TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target="agent",
            status="running",
            started_at=now,
            classification_level=unclassified,
        )
        db.add(run)
        db.commit()
        db.refresh(task)
        db.refresh(run)

        snapshot = build_registry_snapshot(db, user_id=user.id, now=now)
        entry = next(item for item in snapshot.agents if item.agent_id == agent_name)
        if not entry.ready_for_dispatch:
            raise RuntimeError(f"seeded Agent not ready: {entry.reason_codes}")
        context = {
            "user_id": user.id,
            "task_id": task.id,
            "run_id": run.id,
            "source_snapshot_id": source.id,
            "trace_id": trace_id,
            "invocation_id": invocation_id,
            "session_id": session_id,
            "agent_id": agent_name,
            "registry_snapshot_id": snapshot.registry_snapshot_id,
            "registry_snapshot_revision": snapshot.snapshot_revision,
            "registry_snapshot_hash": snapshot.snapshot_hash,
            "manifest_revision": agent.manifest_revision,
            "manifest_sha256": agent.manifest_sha256,
            "route_decision_id": route_id,
            "policy_decision_id": policy_id,
            "model_binding": manifest["model_binding"],
            "auth_assurance": {
                "sid": sid,
                "amr": ["pwd"],
                "acr": "urn:anila:acr:password",
                "auth_time": now.isoformat(),
                "break_glass": False,
            },
        }
        if os.environ.get("E2E_INCLUDE_ACCESS_TOKEN") == "1":
            # The harness captures this field in a shell variable and never
            # echoes/logs it; default seed invocations omit bearer material.
            context["public_access_token"] = public_access_token
        # This JSON crosses the Git Bash/Windows host boundary in a command
        # substitution before the next CSP request. Keep it ASCII-only so
        # the Traditional-Chinese classification enum cannot be mojibaked;
        # json.loads in the harness restores the Unicode value in-container.
        print(json.dumps(context, ensure_ascii=True, separators=(",", ":")))
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("model", "authority"))
    args = parser.parse_args()
    seed_model() if args.mode == "model" else seed_authority()


if __name__ == "__main__":
    main()
