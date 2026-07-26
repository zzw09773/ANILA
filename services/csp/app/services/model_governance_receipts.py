"""CSP-owned Gate 5 receipt sink and request-scoped admission seam.

The signed authority is process-wide, but usage/audit receipts are request
data.  This module binds one SQLAlchemy session and one invocation subject to
the runtime for the lifetime of a model call.  The same sink instance is
passed as both the usage and audit sink, so the normal pre/post pair is one
database transaction rather than two best-effort writes.

The seam intentionally does not perform network I/O.  Callers must obtain an
immutable authorization first, perform their existing CSP-gateway request,
then close it with a post receipt (or a durable failure receipt).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import Integer, cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.model_governance_receipt import ModelGovernanceReceipt
from app.models.token_usage import TokenUsage
from app.services.model_governance_runtime import (
    DurableReceiptSink,
    ModelGovernanceRuntime,
    ModelInvocationAuthorization,
    ModelInvocationCompletion,
    governance_required_for_settings,
)


_REQUEST_TYPE = "model_gov"
_runtime_provider: Callable[[], ModelGovernanceRuntime | None] | None = None
_AUTHORIZATION_SNAPSHOT_FIELDS = (
    "schema_version",
    "invocation_id",
    "callsite_id",
    "classification",
    "gateway_id",
    "endpoint",
    "artifact_id",
    "deployment_id",
    "provider_binding_id",
    "provider_locality",
    "transport_target_sha256",
    "model_registry_revision",
    "upstream_provider_locality",
    "upstream_transport_target_sha256",
    "egress_policy_id",
    "upstream_egress_policy_id",
    "profile_content_sha256",
    "inventory_sha256",
    "receipt_context",
)


def set_model_governance_runtime_provider(
    provider: Callable[[], ModelGovernanceRuntime | None] | None,
) -> None:
    """Install the CSP lifespan-owned runtime resolver.

    Services do not import ``app.main``.  The application lifecycle injects a
    provider once the runtime is constructed; focused tests can inject a
    synthetic runtime directly through the same seam.
    """

    global _runtime_provider
    _runtime_provider = provider


def resolve_model_governance_runtime() -> ModelGovernanceRuntime | None:
    provider = _runtime_provider
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        return None


def admit_registry_provider(model: Any):
    """Apply provider authority when configured, preserving disabled dev mode."""

    runtime = resolve_model_governance_runtime()
    if runtime is None:
        from app.config import settings

        if governance_required_for_settings(settings) or bool(
            getattr(settings, "GATE5_MODEL_GOVERNANCE_ENABLED", False)
        ):
            raise RuntimeError("model-governance runtime provider is unavailable")
        return None
    if not runtime.enabled:
        return None
    return runtime.resolve_provider_binding(model)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _metadata(event: Mapping[str, Any]) -> str:
    # Raw provider transport targets are deployment topology and must never be
    # copied into the durable ledger or audit metadata.  Keep only the signed
    # ids/revision/locality/hashes/egress and artifact/profile evidence.
    sanitized = {
        key: value
        for key, value in event.items()
        if key not in {"transport_target", "upstream_transport_target"}
    }
    return json.dumps(sanitized, ensure_ascii=False, sort_keys=True, default=str)


def _non_negative_int(value: Any, field: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{field} 必須是非負整數")
    return value


@dataclass(frozen=True, slots=True)
class ReceiptSubject:
    """Request/session identity used for the non-null usage columns."""

    user_id: int
    model_id: int | None
    department_id: int | None = None
    api_key_id: int | None = None
    conversation_id: str | None = None
    trace_id: str | None = None
    actor_username: str | None = None
    # The integer FK is the durable DB attribution for an Agent-originated
    # model call; ``agent_id`` below is the canonical registry name used by
    # the signed model-governance authority.  Both are request-derived from
    # the verified csk identity, never from a client header.
    caller_agent_id: int | None = None
    agent_id: str | None = None


class SqlAlchemyReceiptSink(DurableReceiptSink):
    """Transactional Gate 5 usage/audit sink backed by existing CSP tables.

    ``record_pre_usage`` and ``record_pre_audit`` deliberately do not commit
    until the audit method runs.  The same applies to the post pair.  A
    caller therefore gets a receipt only after both rows are durably committed
    together.  Compensation methods rollback an uncommitted half and persist
    a failure audit in a fresh transaction before returning.
    """

    def __init__(self, db: Session, *, subject: ReceiptSubject) -> None:
        if not isinstance(db, Session):
            raise TypeError("receipt sink requires a SQLAlchemy Session")
        if isinstance(subject.user_id, bool) or subject.user_id <= 0:
            raise ValueError("receipt subject user_id must be positive")
        self.db = db
        self.subject = subject
        self._usage_ids: dict[str, int] = {}
        # A receipt sink may continue the post half of the *same* admitted
        # invocation.  A fresh sink/process is never allowed to replay its
        # pre-authorize phase, even when the durable row is completed/failed.
        self._active_invocations: set[str] = set()

    @staticmethod
    def _invocation_id(event: Mapping[str, Any]) -> str:
        value = event.get("invocation_id")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("receipt event invocation_id is required")
        return value.strip()

    def _ledger(self, event: Mapping[str, Any]) -> ModelGovernanceReceipt | None:
        invocation_id = self._invocation_id(event)
        row = (
            self.db.query(ModelGovernanceReceipt)
            .filter(ModelGovernanceReceipt.invocation_id == invocation_id)
            .with_for_update().populate_existing()
            .one_or_none()
        )
        if row is not None:
            try:
                if row.user_id != self.subject.user_id or row.model_id != self.subject.model_id:
                    raise RuntimeError("model-governance invocation subject mismatch")
                callsite_id = event.get("callsite_id")
                if isinstance(callsite_id, str) and row.callsite_id != callsite_id:
                    raise RuntimeError("model-governance invocation callsite mismatch")
                immutable = {
                    "provider_binding_id": event.get("provider_binding_id"),
                    "provider_locality": event.get("provider_locality"),
                    "transport_target_sha256": event.get("transport_target_sha256"),
                    "model_registry_revision": event.get("model_registry_revision"),
                    "upstream_provider_locality": event.get("upstream_provider_locality"),
                    "upstream_transport_target_sha256": event.get(
                        "upstream_transport_target_sha256"
                    ),
                    "egress_policy_id": event.get("egress_policy_id"),
                    "upstream_egress_policy_id": event.get("upstream_egress_policy_id"),
                    "profile_content_sha256": event.get("profile_content_sha256"),
                    "inventory_sha256": event.get("inventory_sha256"),
                }
                for field, expected in immutable.items():
                    if getattr(row, field) != expected:
                        raise RuntimeError(
                            f"model-governance invocation {field} replay drift"
                        )
                try:
                    admitted = json.loads(row.metadata_json or "{}")
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "model-governance invocation snapshot is malformed"
                    ) from exc
                if not isinstance(admitted, Mapping):
                    raise RuntimeError(
                        "model-governance invocation snapshot is malformed"
                    )
                current_snapshot = json.loads(_metadata(event))
                for field in _AUTHORIZATION_SNAPSHOT_FIELDS:
                    if admitted.get(field) != current_snapshot.get(field):
                        raise RuntimeError(
                            f"model-governance invocation {field} replay drift"
                        )
                if event.get("phase") == "post":
                    expected_usage_receipt = (
                        f"usage:{row.usage_record_id}:pre"
                        if row.usage_record_id is not None
                        else None
                    )
                    expected_audit_receipt = (
                        f"audit:{row.pre_audit_id}:pre"
                        if row.pre_audit_id is not None
                        else None
                    )
                    if (
                        event.get("pre_usage_receipt") != expected_usage_receipt
                        or event.get("pre_audit_receipt") != expected_audit_receipt
                    ):
                        raise RuntimeError(
                            "model-governance invocation pre-receipt binding mismatch"
                        )
            except Exception:
                # ``with_for_update`` has acquired the row lock.  A rejected
                # replay must release it immediately instead of leaving a
                # session transaction open until the request unwinds.
                self.db.rollback()
                raise
        return row

    def _new_ledger(self, event: Mapping[str, Any]) -> ModelGovernanceReceipt:
        row = ModelGovernanceReceipt(
            invocation_id=self._invocation_id(event),
            user_id=self.subject.user_id,
            model_id=self.subject.model_id,
            callsite_id=str(event.get("callsite_id") or "unknown"),
            provider_binding_id=event.get("provider_binding_id"),
            provider_locality=event.get("provider_locality"),
            transport_target_sha256=event.get("transport_target_sha256"),
            model_registry_revision=event.get("model_registry_revision"),
            upstream_provider_locality=event.get("upstream_provider_locality"),
            upstream_transport_target_sha256=event.get(
                "upstream_transport_target_sha256"
            ),
            egress_policy_id=event.get("egress_policy_id"),
            upstream_egress_policy_id=event.get("upstream_egress_policy_id"),
            profile_content_sha256=event.get("profile_content_sha256"),
            inventory_sha256=event.get("inventory_sha256"),
            status="pre",
            metadata_json=_metadata(event),
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        self.db.add(row)
        self.db.flush()
        self._active_invocations.add(row.invocation_id)
        return row

    def _usage_row(self, event: Mapping[str, Any]) -> TokenUsage:
        invocation_id = self._invocation_id(event)
        ledger = self._ledger(event)
        row_id = ledger.usage_record_id if ledger is not None else None
        if row_id is None:
            row_id = self._usage_ids.get(invocation_id)
        row = self.db.get(TokenUsage, row_id) if row_id is not None else None
        if row is None:
            raise RuntimeError("model-governance usage receipt is missing")
        self._usage_ids[invocation_id] = int(row.id)
        return row

    def _audit(
        self,
        event: Mapping[str, Any],
        *,
        action: str,
        status: str,
        detail: str,
    ) -> AuditLog:
        # The deployed ``audit_logs.resource_id`` column is the original
        # integer resource key (the ORM annotation is stale and says String).
        # Gate 5's invocation UUID therefore stays in metadata/detail while
        # the durable ledger id is written with an explicit INTEGER cast.
        ledger = self._ledger(event)
        resource_id = cast(int(ledger.id), Integer) if ledger is not None else None
        row = AuditLog(
            actor_user_id=self.subject.user_id,
            actor_username=self.subject.actor_username,
            action=action,
            resource_type="model_invocation",
            resource_id=resource_id,
            status=status,
            detail=detail,
            metadata_json=_metadata(event),
            created_at=_utc_now(),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _commit(self) -> None:
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _failure_audit(self, event: Mapping[str, Any], detail: str) -> str:
        invocation_id = self._invocation_id(event)
        self.db.rollback()
        ledger = self._ledger(event)
        if ledger is None:
            ledger = self._new_ledger(event)
        elif invocation_id not in self._active_invocations:
            if ledger.status in {"completed", "failed"} and ledger.post_audit_id is not None:
                # A post-closed row is safe to read back, but never to replay
                # as a new network authorization.
                return f"audit:{ledger.post_audit_id}:post"
            raise RuntimeError(
                "model-governance invocation already exists; retry requires a new invocation_id"
            )
        row = self._audit(
            event,
            action="model.governance.failure",
            status="failure",
            detail=detail,
        )
        ledger.status = "failed"
        ledger.post_audit_id = int(row.id)
        ledger.metadata_json = _metadata(event)
        self._commit()
        self._active_invocations.discard(invocation_id)
        return f"audit:{row.id}"

    def record_pre_usage(self, event: Mapping[str, Any]) -> str:
        invocation_id = self._invocation_id(event)
        ledger = self._ledger(event)
        if ledger is not None:
            # A durable row in any state means this invocation id has already
            # entered admission.  Reusing it could authorize a second network
            # request; fail closed and release the SELECT FOR UPDATE lock.
            self.db.rollback()
            raise RuntimeError(
                "model-governance invocation already exists; retry requires a new invocation_id"
            )
        if ledger is None:
            try:
                ledger = self._new_ledger(event)
            except IntegrityError:
                # A concurrent request won the unique invocation key.  The
                # losing request must not reuse its pre-receipt or go on to
                # network I/O with the same invocation id.
                self.db.rollback()
                raise RuntimeError(
                    "model-governance invocation already exists; retry requires a new invocation_id"
                )
        row = TokenUsage(
            api_key_id=self.subject.api_key_id,
            user_id=self.subject.user_id,
            department_id=self.subject.department_id,
            model_id=self.subject.model_id,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            request_timestamp=_utc_now(),
            conversation_id=self.subject.conversation_id,
            trace_id=invocation_id,
            request_type=_REQUEST_TYPE,
            caller_agent_id=self.subject.caller_agent_id,
            legacy_runtime_call=False,
        )
        self.db.add(row)
        self.db.flush()
        self._usage_ids[invocation_id] = int(row.id)
        ledger.usage_record_id = int(row.id)
        ledger.metadata_json = _metadata(event)
        self.db.flush()
        return f"usage:{row.id}:pre"

    def record_pre_audit(self, event: Mapping[str, Any]) -> str:
        invocation_id = self._invocation_id(event)
        ledger = self._ledger(event)
        if ledger is None or ledger.usage_record_id is None:
            raise RuntimeError("model-governance usage receipt must precede audit")
        if invocation_id not in self._active_invocations:
            self.db.rollback()
            raise RuntimeError(
                "model-governance invocation already exists; retry requires a new invocation_id"
            )
        if ledger.status == "failed":
            raise RuntimeError("model-governance invocation is already failed")
        if ledger.pre_audit_id is not None:
            return f"audit:{ledger.pre_audit_id}:pre"
        row = self._audit(
            event,
            action="model.governance.pre",
            status="started",
            detail="Gate 5 model invocation admitted; pre-network receipts committed",
        )
        ledger.pre_audit_id = int(row.id)
        ledger.status = "authorized"
        ledger.metadata_json = _metadata(event)
        self._commit()
        return f"audit:{row.id}:pre"

    def compensate_pre_usage(self, event: Mapping[str, Any]) -> str:
        return self._failure_audit(event, "Gate 5 pre-receipt transaction failed; no model request authorized")

    def record_post_usage(self, event: Mapping[str, Any]) -> str:
        invocation_id = self._invocation_id(event)
        ledger = self._ledger(event)
        if ledger is None or ledger.usage_record_id is None:
            raise RuntimeError("model-governance pre receipt is missing")
        if invocation_id not in self._active_invocations:
            self.db.rollback()
            raise RuntimeError(
                "model-governance invocation already exists; retry requires a new invocation_id"
            )
        if ledger.status == "failed":
            if ledger.post_audit_id is not None and event.get("outcome") == "failure":
                return f"usage:{ledger.usage_record_id}:post"
            raise RuntimeError("model-governance invocation is already failed")
        if ledger.status == "completed" and ledger.post_audit_id is not None:
            return f"usage:{ledger.usage_record_id}:post"
        row = self._usage_row(event)
        usage = event.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, Mapping):
            raise RuntimeError("model-governance usage payload must be an object")
        prompt_tokens = _non_negative_int(usage.get("prompt_tokens"), "prompt_tokens")
        completion_tokens = _non_negative_int(
            usage.get("completion_tokens"), "completion_tokens"
        )
        total_tokens = _non_negative_int(
            usage.get("total_tokens"), "total_tokens"
        )
        if total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        row.prompt_tokens = prompt_tokens
        row.completion_tokens = completion_tokens
        row.total_tokens = total_tokens
        duration = usage.get("request_duration_ms")
        if duration is not None:
            row.request_duration_ms = _non_negative_int(duration, "request_duration_ms")
        ledger.metadata_json = _metadata(event)
        self.db.flush()
        return f"usage:{row.id}:post"

    def record_post_audit(self, event: Mapping[str, Any]) -> str:
        invocation_id = self._invocation_id(event)
        ledger = self._ledger(event)
        if ledger is None or ledger.usage_record_id is None:
            raise RuntimeError("model-governance pre receipt is missing")
        if invocation_id not in self._active_invocations:
            self.db.rollback()
            raise RuntimeError(
                "model-governance invocation already exists; retry requires a new invocation_id"
            )
        if ledger.post_audit_id is not None:
            return f"audit:{ledger.post_audit_id}:post"
        failed = event.get("outcome") == "failure"
        row = self._audit(
            event,
            action="model.governance.post",
            status="failure" if failed else "success",
            detail=(
                "Gate 5 model invocation failed after admission"
                if failed
                else "Gate 5 model invocation usage reconciled"
            ),
        )
        ledger.post_audit_id = int(row.id)
        ledger.status = "failed" if failed else "completed"
        ledger.metadata_json = _metadata(event)
        self._commit()
        self._active_invocations.discard(invocation_id)
        return f"audit:{row.id}:post"

    def compensate_post_usage(self, event: Mapping[str, Any]) -> str:
        return self._failure_audit(event, "Gate 5 post-receipt transaction failed; failure audit committed")

    def record_failure_audit(self, event: Mapping[str, Any]) -> str:
        return self._failure_audit(event, "Gate 5 post usage receipt failed; outbound result rejected")


class GovernedModelInvocation:
    """Request/session-scoped seam around the process-wide runtime."""

    def __init__(
        self,
        *,
        runtime: ModelGovernanceRuntime | None,
        db: Session,
        subject: ReceiptSubject,
        registry_model: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.db = db
        self.subject = subject
        self.registry_model = registry_model
        self.sink = SqlAlchemyReceiptSink(db, subject=subject) if runtime and runtime.enabled else None
        self._closed_invocations: set[str] = set()

    @classmethod
    def from_runtime(
        cls,
        db: Session,
        *,
        subject: ReceiptSubject,
        runtime: ModelGovernanceRuntime | None,
        registry_model: Any | None = None,
    ) -> "GovernedModelInvocation":
        return cls(
            runtime=runtime,
            db=db,
            subject=subject,
            registry_model=registry_model,
        )

    @classmethod
    def from_provider(
        cls,
        db: Session,
        *,
        subject: ReceiptSubject,
    ) -> "GovernedModelInvocation":
        return cls(
            runtime=resolve_model_governance_runtime(),
            db=db,
            subject=subject,
        )

    @property
    def enabled(self) -> bool:
        return self.runtime is not None and self.runtime.enabled

    def authorize(
        self,
        *,
        callsite_id: str,
        classification: Any,
        endpoint: str | None = None,
        agent_id: str | None = None,
        invocation_id: str | None = None,
        now: datetime | None = None,
    ) -> ModelInvocationAuthorization | None:
        # ``agent_id`` is retained for source compatibility with earlier
        # callsites, but it is intentionally ignored.  The only authority is
        # the request subject populated from verified CSP identity context.
        del agent_id
        if not self.enabled:
            return None
        assert self.runtime is not None
        assert self.sink is not None
        if self.registry_model is None:
            raise RuntimeError("model-governance registry row is required")
        safe_endpoint = endpoint or self.runtime.gateway_endpoint
        return self.runtime.authorize_model_invocation(
            callsite_id,
            classification,
            self.subject.agent_id,
            safe_endpoint,
            registry_model=self.registry_model,
            invocation_id=invocation_id,
            now=now,
            usage_sink=self.sink,
            audit_sink=self.sink,
            receipt_context={
                "user_id": self.subject.user_id,
                "model_id": self.subject.model_id,
                "conversation_id": self.subject.conversation_id,
                "trace_id": self.subject.trace_id,
                "caller_agent_id": self.subject.caller_agent_id,
                "agent_id": self.subject.agent_id,
            },
        )

    def complete(
        self,
        authorization: ModelInvocationAuthorization | None,
        usage: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> ModelInvocationCompletion | None:
        if authorization is None:
            return None
        if authorization.invocation_id in self._closed_invocations:
            return None
        assert self.runtime is not None
        assert self.sink is not None
        try:
            result = self.runtime.record_post_usage(
                authorization,
                usage or {},
                now=now,
                usage_sink=self.sink,
                audit_sink=self.sink,
                receipt_context={
                    "user_id": self.subject.user_id,
                    "model_id": self.subject.model_id,
                    "conversation_id": self.subject.conversation_id,
                    "trace_id": self.subject.trace_id,
                    "caller_agent_id": self.subject.caller_agent_id,
                    "agent_id": self.subject.agent_id,
                },
            )
        except Exception:
            # Runtime may already have committed a durable failure audit (for
            # example when post usage itself fails).  Do not let the caller's
            # outer exception path submit a second failure closure.
            self._closed_invocations.add(authorization.invocation_id)
            raise
        self._closed_invocations.add(authorization.invocation_id)
        return result

    def fail(
        self,
        authorization: ModelInvocationAuthorization | None,
        error: BaseException | str,
        *,
        now: datetime | None = None,
    ) -> ModelInvocationCompletion | None:
        if authorization is None:
            return None
        if authorization.invocation_id in self._closed_invocations:
            return None
        assert self.runtime is not None
        assert self.sink is not None
        try:
            result = self.runtime.record_post_failure(
                authorization,
                error,
                now=now,
                usage_sink=self.sink,
                audit_sink=self.sink,
                receipt_context={
                    "user_id": self.subject.user_id,
                    "model_id": self.subject.model_id,
                    "conversation_id": self.subject.conversation_id,
                    "trace_id": self.subject.trace_id,
                    "caller_agent_id": self.subject.caller_agent_id,
                    "agent_id": self.subject.agent_id,
                },
            )
        finally:
            self._closed_invocations.add(authorization.invocation_id)
        return result


__all__ = [
    "GovernedModelInvocation",
    "ReceiptSubject",
    "SqlAlchemyReceiptSink",
    "admit_registry_provider",
    "resolve_model_governance_runtime",
    "set_model_governance_runtime_provider",
]
