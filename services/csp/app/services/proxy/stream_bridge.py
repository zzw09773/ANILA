"""Gate 4 synchronous trust boundary for agent-originated timeline events.

The downstream agent is not an authority for governance identifiers.  This
module accepts only the frozen ``anila.step`` wire event, validates it against
``anila-contracts``, rebinds every identity/classification field from the CSP
dispatch context, and applies bounded per-event/per-run rate limits.  It is
deliberately stateless beyond one live stream; replay and idempotency belong to
Gate 5's durable StreamBridge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from anila_contracts import Classification, ExecutionGrant, StepEvent
from anila_contracts.events import STEP_EVENT_SSE_NAME, StepKind, StepStatus
from anila_contracts._types import InvocationTargetKind

logger = logging.getLogger("anila.stream_bridge.audit")

TIMELINE_EVENT_NAME = STEP_EVENT_SSE_NAME
MAX_EVENT_BYTES = 32 * 1024
MAX_EVENTS_PER_SECOND = 40
MAX_EVENTS_PER_RUN = 500
MAX_SAFE_SUMMARY_CHARS = 500

_SECRET_PATTERN = re.compile(
    r"(?i)(?:"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:sk|csk|bsk)-[A-Za-z0-9_-]{12,}|"
    r"\b(?:api[_-]?key|access[_-]?token|authorization|password)\s*[:=]\s*\S+"
    r")"
)


@dataclass(frozen=True, slots=True)
class BridgeContext:
    task_id: str
    trace_id: str
    agent_id: str
    session_id: str
    run_id: str
    classification: Classification
    # CSP-authored invocation identity.  Gate 5 R4 uses this to prevent a
    # duplicate Router invocation from creating a second downstream side
    # effect; older Gate 4 callers may omit it.
    invocation_id: str | None = None
    # These optional fields are not read from the Agent event.  They are
    # CSP-owned dispatch binding facts used by ``AgentClient`` when a caller
    # wants to carry the same context across the proxy boundary.
    registry_snapshot_id: str | None = None
    manifest_revision: str | None = None
    manifest_sha256: str | None = None
    grant_id: str | None = None
    source_snapshot_id: int | None = None
    route_decision_id: str | None = None
    policy_decision_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "trace_id",
            "agent_id",
            "session_id",
            "run_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必須是非空字串")
        if self.invocation_id is not None:
            if not isinstance(self.invocation_id, str) or not self.invocation_id.strip():
                raise ValueError("invocation_id 不得為空白")
        if not isinstance(self.classification, Classification):
            raise TypeError("classification 必須是 Classification")
        for field_name in (
            "registry_snapshot_id",
            "manifest_revision",
            "manifest_sha256",
            "grant_id",
            "route_decision_id",
            "policy_decision_id",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} 不得為空白")
        if self.source_snapshot_id is not None:
            if (
                isinstance(self.source_snapshot_id, bool)
                or not isinstance(self.source_snapshot_id, int)
                or self.source_snapshot_id <= 0
            ):
                raise ValueError("source_snapshot_id 必須是 positive int")


class StreamBridgeError(Exception):
    """Base error for fail-closed StreamBridge and store operations."""

    status_code = 409
    code = "STREAM_BRIDGE_ERROR"

    def __init__(self, message: str, *, run_id: str | None = None, event_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.event_id = event_id


class EventBindingError(StreamBridgeError):
    """A stored event or replay request is bound to the wrong trusted run."""

    status_code = 403
    code = "EVENT_BINDING_MISMATCH"


class EventConflictError(StreamBridgeError):
    """One ``(run_id, event_id)`` was reused with a different payload."""

    status_code = 409
    code = "EVENT_ID_CONFLICT"


class EventOrderError(StreamBridgeError):
    """A new event would move the source order backwards or repeat it."""

    status_code = 409
    code = "EVENT_OUT_OF_ORDER"


class TerminalEventError(StreamBridgeError):
    """A run already has its exactly-once terminal event."""

    status_code = 409
    code = "TERMINAL_ALREADY_WRITTEN"


class TerminalConflictError(StreamBridgeError):
    """A terminal retry changed status or safe summary."""

    status_code = 409
    code = "TERMINAL_PAYLOAD_CONFLICT"


class StoreNotConfiguredError(StreamBridgeError):
    """A production bridge was created without an injected event store."""

    code = "EVENT_STORE_NOT_CONFIGURED"


class CspProxyRequiredError(StreamBridgeError):
    """A caller attempted to bypass the CSP proxy with a raw Agent URL."""

    status_code = 403
    code = "CSP_PROXY_REQUIRED"


@dataclass(frozen=True, slots=True)
class AgentCallBinding:
    """Immutable CSP-owned binding attached to one downstream Agent call.

    The bridge accepts a complete, short-lived :class:`ExecutionGrant`, not
    an opaque ``grant_id``.  This lets the pure component validate grant
    expiry and every authority binding before a transport is touched.
    ``task_id``/``run_id`` remain strings on the Gate 4 event wire; the
    positive integer properties below are the lossless R1/R3 authority view.
    """

    task_id: str
    run_id: str
    session_id: str
    trace_id: str
    agent_id: str
    grant: ExecutionGrant | Mapping[str, object] | None = None
    registry_snapshot_id: str | None = None
    snapshot_id: str | None = None
    registry_snapshot_revision: str | None = None
    snapshot_revision: str | None = None
    registry_snapshot_hash: str | None = None
    snapshot_hash: str | None = None
    manifest_revision: str | None = None
    revision: str | None = None
    manifest_sha256: str | None = None
    manifest_hash: str | None = None
    source_snapshot_id: int | None = None
    classification: Classification | None = None
    grant_id: str | None = None
    execution_grant_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "run_id", "session_id", "trace_id", "agent_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必須是非空字串")

        def positive_wire(value: str, field_name: str) -> int:
            # This is intentionally strict: no bool/float/coercive whitespace
            # or arbitrary legacy identifier is admitted to a governed call.
            if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
                raise ValueError(f"{field_name} wire value 必須是 positive-int decimal string")
            parsed = int(value)
            if parsed <= 0:
                raise ValueError(f"{field_name} 必須大於 0")
            return parsed

        task_id_int = positive_wire(self.task_id, "task_id")
        run_id_int = positive_wire(self.run_id, "run_id")

        def coalesce(primary: str | None, alias: str | None, field_name: str) -> str:
            if primary is not None and alias is not None and primary != alias:
                raise ValueError(f"{field_name} alias 不一致")
            value = primary or alias
            if value is None or not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必須是非空字串")
            return value.strip()

        def coalesce_optional(
            primary: str | None, alias: str | None, field_name: str
        ) -> str | None:
            if primary is not None and alias is not None and primary != alias:
                raise ValueError(f"{field_name} alias 不一致")
            value = primary or alias
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} 不得為空白")
            return value.strip() if isinstance(value, str) else value

        snapshot = coalesce(
            self.registry_snapshot_id, self.snapshot_id, "registry_snapshot_id"
        )
        snapshot_revision = coalesce(
            self.registry_snapshot_revision,
            self.snapshot_revision,
            "registry_snapshot_revision",
        )
        snapshot_hash = coalesce(
            self.registry_snapshot_hash,
            self.snapshot_hash,
            "registry_snapshot_hash",
        )
        revision = coalesce(self.manifest_revision, self.revision, "manifest_revision")
        manifest_hash = coalesce(
            self.manifest_sha256, self.manifest_hash, "manifest_sha256"
        )
        grant_alias = coalesce_optional(self.grant_id, self.execution_grant_id, "grant_id")
        raw_grant = self.grant
        if raw_grant is None:
            raise ValueError("Agent call 必須攜帶完整 ExecutionGrant")
        if isinstance(raw_grant, Mapping):
            try:
                parsed_grant = ExecutionGrant.model_validate(raw_grant)
            except Exception as exc:
                raise ValueError("ExecutionGrant schema 無效") from exc
        elif isinstance(raw_grant, ExecutionGrant):
            parsed_grant = raw_grant
        else:
            raise TypeError("grant 必須是 ExecutionGrant 或其 mapping")
        parsed_grant.assert_active_at(datetime.now(timezone.utc))
        if parsed_grant.target.kind is not InvocationTargetKind.AGENT:
            raise ValueError("ExecutionGrant target 必須是 agent")
        if str(parsed_grant.target.id) != self.agent_id:
            raise ValueError("ExecutionGrant target agent 不一致")
        if parsed_grant.task_id != task_id_int or parsed_grant.run_id != run_id_int:
            raise ValueError("ExecutionGrant task/run binding 不一致")
        source_snapshot_id = self.source_snapshot_id or parsed_grant.source_snapshot_id
        if isinstance(source_snapshot_id, bool) or not isinstance(source_snapshot_id, int) or source_snapshot_id <= 0:
            raise ValueError("Agent call 必須攜帶 positive source_snapshot_id")
        if parsed_grant.source_snapshot_id != source_snapshot_id:
            raise ValueError("ExecutionGrant source_snapshot_id 不一致")
        if str(parsed_grant.registry_snapshot_id) != snapshot:
            raise ValueError("ExecutionGrant registry_snapshot_id 不一致")
        if snapshot_revision != snapshot or snapshot_hash != snapshot:
            raise ValueError("Registry snapshot id/revision/hash 必須是同一 authority generation")
        if parsed_grant.manifest_revision != revision:
            raise ValueError("ExecutionGrant manifest_revision 不一致")
        grant = parsed_grant.grant_id
        if grant_alias is not None and parsed_grant.grant_id != grant_alias:
            raise ValueError("ExecutionGrant grant_id 不一致")
        classification = self.classification or parsed_grant.classification
        if parsed_grant.classification is not classification:
            raise ValueError("ExecutionGrant classification 不一致")
        model_binding = parsed_grant.target.model_binding
        if model_binding is None or model_binding.gateway != "csp":
            raise ValueError("ExecutionGrant 必須綁定 CSP model gateway")
        if self.manifest_sha256 is None and self.manifest_hash is None:
            raise ValueError("Agent call 必須攜帶 manifest hash")
        # ExecutionGrant v1 deliberately carries revision but not a manifest
        # hash.  The hash is still required as a separate CSP snapshot fact;
        # an adapter must compare it to its DB registry row before issuing the
        # grant.  We therefore enforce presence and alias consistency here.
        object.__setattr__(self, "grant", parsed_grant)
        object.__setattr__(self, "source_snapshot_id", source_snapshot_id)
        object.__setattr__(self, "classification", classification)
        object.__setattr__(self, "registry_snapshot_id", snapshot)
        object.__setattr__(self, "snapshot_id", snapshot)
        object.__setattr__(self, "registry_snapshot_revision", snapshot_revision)
        object.__setattr__(self, "snapshot_revision", snapshot_revision)
        object.__setattr__(self, "registry_snapshot_hash", snapshot_hash)
        object.__setattr__(self, "snapshot_hash", snapshot_hash)
        object.__setattr__(self, "manifest_revision", revision)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "manifest_sha256", manifest_hash)
        object.__setattr__(self, "manifest_hash", manifest_hash)
        object.__setattr__(self, "grant_id", grant)
        object.__setattr__(self, "execution_grant_id", grant)

    @property
    def task_id_int(self) -> int:
        return int(self.task_id)

    @property
    def run_id_int(self) -> int:
        return int(self.run_id)

    def assert_active_at(self, now: datetime) -> None:
        """Re-check the complete grant at the actual transport boundary."""

        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("grant validation time 必須帶時區")
        grant = self.grant
        if not isinstance(grant, ExecutionGrant):
            raise ValueError("Agent call grant 尚未 canonicalize")
        grant.assert_active_at(now)

    @classmethod
    def from_context(
        cls,
        context: BridgeContext,
        *,
        registry_snapshot_id: str | None = None,
        registry_snapshot_revision: str | None = None,
        registry_snapshot_hash: str | None = None,
        manifest_revision: str | None = None,
        manifest_sha256: str | None = None,
        grant_id: str | None = None,
        grant: ExecutionGrant | Mapping[str, object] | None = None,
        source_snapshot_id: int | None = None,
        classification: Classification | None = None,
    ) -> "AgentCallBinding":
        """Build a call binding from trusted stream context and CSP facts."""

        return cls(
            task_id=context.task_id,
            run_id=context.run_id,
            session_id=context.session_id,
            trace_id=context.trace_id,
            agent_id=context.agent_id,
            grant=grant,
            registry_snapshot_id=registry_snapshot_id or context.registry_snapshot_id,
            registry_snapshot_revision=registry_snapshot_revision or context.registry_snapshot_id,
            registry_snapshot_hash=registry_snapshot_hash or context.registry_snapshot_id,
            manifest_revision=manifest_revision or context.manifest_revision,
            manifest_sha256=manifest_sha256 or context.manifest_sha256,
            source_snapshot_id=source_snapshot_id or context.source_snapshot_id,
            classification=classification or context.classification,
            grant_id=grant_id or context.grant_id,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "AgentCallBinding":
        """Parse a strict mapping without accepting an endpoint override."""

        allowed = {
            "task_id",
            "run_id",
            "session_id",
            "trace_id",
            "agent_id",
            "grant",
            "registry_snapshot_id",
            "snapshot_id",
            "registry_snapshot_revision",
            "snapshot_revision",
            "registry_snapshot_hash",
            "snapshot_hash",
            "registry_snapshot_revision",
            "snapshot_revision",
            "registry_snapshot_hash",
            "snapshot_hash",
            "manifest_revision",
            "revision",
            "manifest_sha256",
            "manifest_hash",
            "source_snapshot_id",
            "classification",
            "grant_id",
            "execution_grant_id",
        }
        if set(value) - allowed:
            raise CspProxyRequiredError("Agent binding 不得包含 endpoint 或未定義欄位")
        return cls(**dict(value))  # type: ignore[arg-type]

    def headers(self) -> dict[str, str]:
        """Return only non-secret correlation/governance headers."""

        return {
            "X-ANILA-Task-Id": self.task_id,
            "X-ANILA-Run-Id": self.run_id,
            "X-ANILA-Session-Id": self.session_id,
            "X-ANILA-Trace-Id": self.trace_id,
            "X-ANILA-Agent-Id": self.agent_id,
            "X-ANILA-Registry-Snapshot-Id": self.registry_snapshot_id or "",
            "X-ANILA-Registry-Snapshot-Revision": self.registry_snapshot_revision or "",
            "X-ANILA-Registry-Snapshot-Hash": self.registry_snapshot_hash or "",
            "X-ANILA-Manifest-Revision": self.manifest_revision or "",
            "X-ANILA-Manifest-SHA256": self.manifest_sha256 or "",
            "X-ANILA-Execution-Grant-Id": self.grant_id or "",
        }


@dataclass(frozen=True, slots=True)
class CspProxyRequest:
    """Prepared request passed to an injected CSP transport in tests."""

    method: str
    url: str
    headers: Mapping[str, str]
    json: Mapping[str, object]


@runtime_checkable
class AgentClient(Protocol):
    """Downstream seam: implementations must call the CSP proxy only."""

    def dispatch(
        self,
        payload: Mapping[str, object],
        *,
        binding: AgentCallBinding,
        now: datetime | None = None,
    ) -> object: ...


class CspProxyAgentClient:
    """Prepare the existing CSP ``/v1/chat/completions`` request only.

    The default transport is intentionally absent: callers inject a CSP-owned
    HTTP adapter.  Returning :class:`CspProxyRequest` without a transport keeps
    this component side-effect free and makes its header contract testable.
    """

    def __init__(
        self,
        csp_base_url: str,
        *,
        trusted_csp_origin: str,
        trusted_csp_audience: str = "/v1/chat/completions",
        trusted_csp_path: str | None = None,
        transport: Callable[[CspProxyRequest], object] | None = None,
    ) -> None:
        self._origin = self._validate_csp_origin(trusted_csp_origin, "trusted_csp_origin")
        self._base_url = self._validate_csp_base_url(csp_base_url, expected_origin=self._origin)
        if trusted_csp_path is not None:
            if trusted_csp_audience != "/v1/chat/completions" and trusted_csp_audience != trusted_csp_path:
                raise CspProxyRequiredError("trusted CSP audience/path alias 不一致")
            trusted_csp_audience = trusted_csp_path
        self._audience = self._validate_csp_audience(
            trusted_csp_audience, expected_origin=self._origin
        )
        self._transport = transport

    @staticmethod
    def _canonical_origin(value: str, *, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CspProxyRequiredError(f"{field_name} 必須是 CSP origin")
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CspProxyRequiredError(f"{field_name} 必須是 http(s) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CspProxyRequiredError(f"{field_name} 不得帶 userinfo/query/fragment")
        if parsed.path not in {"", "/"}:
            raise CspProxyRequiredError(f"{field_name} 必須是 exact origin，不得帶 path")
        try:
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise CspProxyRequiredError(f"{field_name} port 無效") from exc
        if hostname is None:
            raise CspProxyRequiredError(f"{field_name} 缺少 host")
        default_port = (parsed.scheme == "http" and port == 80) or (
            parsed.scheme == "https" and port == 443
        )
        netloc = hostname.lower()
        if port is not None and not default_port:
            netloc = f"{netloc}:{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))

    @classmethod
    def _validate_csp_origin(cls, value: str, field_name: str) -> str:
        return cls._canonical_origin(value, field_name=field_name)

    @classmethod
    def _validate_csp_base_url(cls, value: str, *, expected_origin: str) -> str:
        actual = cls._canonical_origin(value, field_name="csp_base_url")
        if actual != expected_origin:
            raise CspProxyRequiredError("csp_base_url host/origin 不符合 trusted CSP origin")
        return actual

    @classmethod
    def _validate_csp_audience(cls, value: str, *, expected_origin: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise CspProxyRequiredError("trusted_csp_audience 不得為空白")
        parsed = urlsplit(value.strip())
        if parsed.scheme or parsed.netloc:
            actual = cls._canonical_origin(
                urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
                field_name="trusted_csp_audience",
            )
            if actual != expected_origin:
                raise CspProxyRequiredError("trusted_csp_audience origin 不符合 CSP origin")
        if parsed.query or parsed.fragment or not parsed.path.startswith("/"):
            raise CspProxyRequiredError("trusted_csp_audience 必須是 exact path")
        if parsed.path != "/v1/chat/completions":
            raise CspProxyRequiredError("Agent downstream audience 必須是 /v1/chat/completions")
        return parsed.path

    @property
    def csp_base_url(self) -> str:
        return self._base_url

    @property
    def trusted_csp_origin(self) -> str:
        return self._origin

    @property
    def trusted_csp_audience(self) -> str:
        return self._audience

    def build_request(
        self,
        payload: Mapping[str, object],
        *,
        binding: AgentCallBinding,
        raw_agent_endpoint: str | None = None,
        now: datetime | None = None,
    ) -> CspProxyRequest:
        binding.assert_active_at(now or datetime.now(timezone.utc))
        grant = binding.grant
        if not isinstance(grant, ExecutionGrant):
            raise CspProxyRequiredError("Agent call grant 尚未 canonicalize")
        if raw_agent_endpoint is not None:
            raise CspProxyRequiredError("不得從 Router 直連 raw Agent endpoint")
        if not isinstance(payload, Mapping):
            raise TypeError("Agent payload 必須是 mapping")
        if any(key in payload for key in ("endpoint_url", "agent_url", "base_url")):
            raise CspProxyRequiredError("Agent payload 不得指定 downstream endpoint")
        target = f"{self._origin}{self._audience}"
        body = dict(payload)
        requested_model = body.get("model")
        if requested_model is not None and requested_model != binding.agent_id:
            raise CspProxyRequiredError("CSP proxy model 必須綁定 granted agent")
        body["model"] = binding.agent_id
        body["stream"] = True
        if "messages" not in body:
            query = body.pop("query", body.pop("input", ""))
            body["messages"] = [{"role": "user", "content": query}] if isinstance(query, str) else []
        body["anila_binding"] = {
            "task_id": binding.task_id,
            "run_id": binding.run_id,
            "session_id": binding.session_id,
            "trace_id": binding.trace_id,
            "source_snapshot_id": binding.source_snapshot_id,
            "route_decision_id": grant.route_decision_id,
            "policy_decision_id": grant.policy_decision_id,
            "registry_snapshot_id": binding.registry_snapshot_id,
            "registry_snapshot_revision": binding.registry_snapshot_revision,
            "registry_snapshot_hash": binding.registry_snapshot_hash,
            "manifest_revision": binding.manifest_revision,
            "manifest_sha256": binding.manifest_sha256,
            "grant_id": binding.grant_id,
            "classification": grant.classification.to_storage(),
            "allowed_capabilities": grant.allowed_capabilities,
            "allowed_scopes": grant.allowed_scopes,
            "model_binding": grant.target.model_binding.model_dump(mode="json")
            if grant.target.model_binding is not None
            else None,
        }
        return CspProxyRequest(
            method="POST",
            url=target,
            headers=binding.headers(),
            json=body,
        )

    def dispatch(
        self,
        payload: Mapping[str, object],
        *,
        binding: AgentCallBinding,
        raw_agent_endpoint: str | None = None,
        now: datetime | None = None,
    ) -> object:
        request = self.build_request(
            payload, binding=binding, raw_agent_endpoint=raw_agent_endpoint, now=now
        )
        if self._transport is None:
            return request
        return self._transport(request)

    invoke = dispatch
    call = dispatch


@dataclass(frozen=True, slots=True)
class SessionAppendResult:
    """Result of an append, including whether it was an idempotent replay."""

    event: StepEvent
    duplicate: bool = False

    @property
    def cursor(self) -> int:
        return int(self.event.cursor)


@runtime_checkable
class SessionEventStore(Protocol):
    """Pluggable store contract for authoritative session event persistence.

    Implementations must make append atomic with their idempotency key and
    terminal constraint.  No in-memory implementation is considered durable.
    """

    def append(
        self, event: StepEvent, *, binding: BridgeContext
    ) -> SessionAppendResult: ...

    def read_after(
        self, *, binding: BridgeContext, after_cursor: int | str = 0
    ) -> tuple[StepEvent, ...]: ...

    def terminal(self, *, binding: BridgeContext) -> StepEvent | None: ...


class CspSessionEventStore(SessionEventStore, Protocol):
    """Future CSP DB/transaction adapter contract; intentionally no adapter yet."""


@dataclass(slots=True)
class _RunState:
    binding: BridgeContext
    events: list[StepEvent]
    by_event_id: dict[str, tuple[str, StepEvent]]
    next_cursor: int = 0
    last_source_order: int | None = None
    terminal_event_id: str | None = None


class InMemorySessionEventStore:
    """Test-only conformance store; it is not durable or restart-safe alone.

    Restart simulation is achieved by constructing a new ``StreamBridge``
    against the same store object.  Production must provide a CSP DB/Redis
    adapter implementing :class:`SessionEventStore`.
    """

    durable = False

    def __init__(self) -> None:
        self._runs: dict[str, _RunState] = {}
        self._lock = RLock()

    @staticmethod
    def _binding_key(binding: BridgeContext) -> tuple[str, str, str, str, str]:
        return (
            binding.task_id,
            binding.run_id,
            binding.session_id,
            binding.trace_id,
            binding.agent_id,
        )

    @staticmethod
    def _event_digest(event: StepEvent) -> str:
        # ``cursor`` is assigned by the store and therefore is not part of
        # idempotency payload identity.  Every other StepEvent field remains
        # conflict-sensitive, including source sequence and summaries.
        payload = event.model_dump(mode="json")
        payload.pop("cursor", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _source_order(event: StepEvent) -> int:
        # Agent ``cursor`` is untrusted wire data.  The event sequence is the
        # only source-order hint accepted at this boundary; the persisted
        # cursor is assigned below by the store itself.
        return cast(int, event.sequence)

    @staticmethod
    def _assert_binding(event: StepEvent, binding: BridgeContext) -> None:
        values = {
            "task_id": (event.task_id, binding.task_id),
            "trace_id": (event.trace_id, binding.trace_id),
            "agent_id": (event.agent_id, binding.agent_id),
            "session_id": (event.session_id, binding.session_id),
            "run_id": (event.run_id, binding.run_id),
            "classification": (event.classification, binding.classification),
        }
        mismatches = [name for name, (actual, expected) in values.items() if actual != expected]
        if mismatches:
            raise EventBindingError(
                "StepEvent 治理欄位未綁定 trusted dispatch context: "
                + ",".join(mismatches),
                run_id=binding.run_id,
                event_id=event.event_id,
            )

    @classmethod
    def _assert_state_binding(cls, state: _RunState, binding: BridgeContext) -> None:
        if cls._binding_key(state.binding) != cls._binding_key(binding):
            raise EventBindingError(
                "run_id 已綁定其他 task/session/trace/agent", run_id=binding.run_id
            )

    def append(
        self, event: StepEvent, *, binding: BridgeContext
    ) -> SessionAppendResult:
        if not isinstance(event, StepEvent):
            raise TypeError("event 必須是 StepEvent")
        self._assert_binding(event, binding)
        with self._lock:
            state = self._runs.get(binding.run_id)
            if state is None:
                state = _RunState(binding=binding, events=[], by_event_id={})
                self._runs[binding.run_id] = state
            else:
                self._assert_state_binding(state, binding)

            existing = state.by_event_id.get(event.event_id)
            if existing is not None:
                existing_digest, existing_event = existing
                if existing_digest != self._event_digest(event):
                    raise EventConflictError(
                        "相同 (run_id,event_id) 的 payload 不一致",
                        run_id=binding.run_id,
                        event_id=event.event_id,
                    )
                return SessionAppendResult(existing_event, duplicate=True)

            if state.terminal_event_id is not None:
                raise TerminalEventError(
                    "run 已寫入 terminal event，不接受後續事件",
                    run_id=binding.run_id,
                    event_id=event.event_id,
                )

            source_order = self._source_order(event)
            if state.last_source_order is not None and source_order <= state.last_source_order:
                raise EventOrderError(
                    "新事件 cursor/sequence 必須嚴格遞增",
                    run_id=binding.run_id,
                    event_id=event.event_id,
                )

            state.next_cursor += 1
            stored = event.model_copy(update={"cursor": str(state.next_cursor)})
            digest = self._event_digest(event)
            state.events.append(stored)
            state.by_event_id[event.event_id] = (digest, stored)
            state.last_source_order = source_order
            if event.status in {
                StepStatus.COMPLETED,
                StepStatus.FAILED,
                StepStatus.CANCELLED,
            }:
                state.terminal_event_id = event.event_id
            return SessionAppendResult(stored, duplicate=False)

    def read_after(
        self, *, binding: BridgeContext, after_cursor: int | str = 0
    ) -> tuple[StepEvent, ...]:
        try:
            cursor = int(after_cursor)
        except (TypeError, ValueError) as exc:
            raise EventOrderError("after_cursor 必須是非負整數", run_id=binding.run_id) from exc
        if cursor < 0:
            raise EventOrderError("after_cursor 不得小於 0", run_id=binding.run_id)
        with self._lock:
            state = self._runs.get(binding.run_id)
            if state is None:
                return ()
            self._assert_state_binding(state, binding)
            return tuple(event for event in state.events if int(event.cursor) > cursor)

    def terminal(self, *, binding: BridgeContext) -> StepEvent | None:
        with self._lock:
            state = self._runs.get(binding.run_id)
            if state is None:
                return None
            self._assert_state_binding(state, binding)
            if state.terminal_event_id is None:
                return None
            return state.by_event_id[state.terminal_event_id][1]

    def latest_cursor(self, *, binding: BridgeContext) -> int:
        with self._lock:
            state = self._runs.get(binding.run_id)
            if state is None:
                return 0
            self._assert_state_binding(state, binding)
            return state.next_cursor

    def all_events(self, *, binding: BridgeContext) -> tuple[StepEvent, ...]:
        return self.read_after(binding=binding, after_cursor=0)


class StreamValidator:
    """Validate and bind events for one live agent stream."""

    def __init__(
        self,
        context: BridgeContext,
        *,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_events_per_second: int = MAX_EVENTS_PER_SECOND,
        max_events_per_run: int = MAX_EVENTS_PER_RUN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self.max_event_bytes = max_event_bytes
        self.max_events_per_second = max_events_per_second
        self.max_events_per_run = max_events_per_run
        self._clock = clock
        self._accepted = 0
        self._recent: deque[float] = deque()

    def _drop(self, reason: str, *, event_name: str | None) -> None:
        # Never log the rejected payload: it may be the secret we are blocking.
        logger.warning(
            "stream_event_dropped reason=%s event=%s task=%s agent=%s run=%s",
            reason,
            event_name or "message",
            self.context.task_id,
            self.context.agent_id,
            self.context.run_id,
        )

    def _reserve_budget(self, event_name: str) -> bool:
        if self._accepted >= self.max_events_per_run:
            self._drop("run_budget", event_name=event_name)
            return False
        now = self._clock()
        while self._recent and now - self._recent[0] >= 1.0:
            self._recent.popleft()
        if len(self._recent) >= self.max_events_per_second:
            self._drop("rate_budget", event_name=event_name)
            return False
        self._recent.append(now)
        self._accepted += 1
        return True

    def validate(self, event_name: str | None, data: str | None) -> str | None:
        """Return a canonical SSE block or ``None`` when the event is rejected."""
        if event_name != TIMELINE_EVENT_NAME:
            self._drop("event_name", event_name=event_name)
            return None
        if data is None:
            self._drop("missing_data", event_name=event_name)
            return None
        if len(data.encode("utf-8")) > self.max_event_bytes:
            self._drop("event_size", event_name=event_name)
            return None
        if not self._reserve_budget(TIMELINE_EVENT_NAME):
            return None
        try:
            raw = json.loads(data)
            event = StepEvent.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, TypeError):
            self._drop("schema", event_name=event_name)
            return None

        for summary in (event.safe_input_summary, event.safe_output_summary):
            if summary is None:
                continue
            if len(summary) > MAX_SAFE_SUMMARY_CHARS:
                self._drop("summary_size", event_name=event_name)
                return None
            if _SECRET_PATTERN.search(summary):
                self._drop("summary_secret", event_name=event_name)
                return None

        trusted = self.context
        rebound = event.model_copy(
            update={
                "task_id": trusted.task_id,
                "trace_id": trusted.trace_id,
                "agent_id": trusted.agent_id,
                "session_id": trusted.session_id,
                "run_id": trusted.run_id,
                "invocation_id": trusted.invocation_id or event.invocation_id,
                "classification": trusted.classification,
            }
        )
        return (
            f"event: {TIMELINE_EVENT_NAME}\n"
            "data: "
            + cast(str, rebound.model_dump_json())
            + "\n\n"
        )


class StreamBridge:
    """Durable-store aware core built on the Gate 4 validator.

    ``StreamValidator`` remains the live-wire trust boundary and keeps its
    existing ``str | None`` API.  ``StreamBridge`` adds the durable seam:
    every accepted event is appended atomically to an injected
    :class:`SessionEventStore`, replay reads only after a trusted cursor, and
    duplicate/conflict/terminal errors are surfaced instead of being silently
    retried.  A bridge without a store is intentionally not dispatchable.
    """

    def __init__(
        self,
        context: BridgeContext,
        *,
        store: SessionEventStore | None = None,
        validator: StreamValidator | None = None,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_events_per_second: int = MAX_EVENTS_PER_SECOND,
        max_events_per_run: int = MAX_EVENTS_PER_RUN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self.store = store
        self.validator = validator or StreamValidator(
            context,
            max_event_bytes=max_event_bytes,
            max_events_per_second=max_events_per_second,
            max_events_per_run=max_events_per_run,
            clock=clock,
        )

    def _require_store(self) -> SessionEventStore:
        if self.store is None:
            raise StoreNotConfiguredError("StreamBridge 必須注入 CSP-owned SessionEventStore")
        return self.store

    @staticmethod
    def _frame(event: StepEvent) -> str:
        return (
            f"event: {TIMELINE_EVENT_NAME}\n"
            "data: "
            + cast(str, event.model_dump_json())
            + "\n\n"
        )

    def _validated_event(self, data: str) -> StepEvent | None:
        frame = self.validator.validate(TIMELINE_EVENT_NAME, data)
        if frame is None:
            return None
        data_line = next(
            (line[6:] for line in frame.splitlines() if line.startswith("data: ")),
            None,
        )
        if data_line is None:
            # This should be unreachable because StreamValidator authored the
            # frame.  Keep the bridge fail-closed if that invariant changes.
            logger.error("stream_bridge_internal_error reason=missing_data_line")
            return None
        try:
            return StepEvent.model_validate(json.loads(data_line))
        except (json.JSONDecodeError, ValidationError, TypeError):
            logger.error("stream_bridge_internal_error reason=validator_frame_schema")
            return None

    def append_event(self, event: StepEvent) -> SessionAppendResult | None:
        """Validate, CSP-rebind, and atomically append one event.

        ``None`` means the Gate 4 validator dropped an unknown/malformed,
        secret-bearing, oversized, or over-budget frame.  Store conflicts are
        deliberately raised as typed errors so callers cannot accidentally
        turn a 409 into a duplicate side effect.
        """

        if not isinstance(event, StepEvent):
            raise TypeError("event 必須是 StepEvent")
        validated = self._validated_event(event.model_dump_json())
        if validated is None:
            return None
        return self._require_store().append(validated, binding=self.context)

    def append(
        self,
        event_name: str | StepEvent,
        data: str | None = None,
    ) -> str | SessionAppendResult | None:
        """Accept either a named SSE frame or a prevalidated StepEvent.

        The SSE form returns a canonical frame for existing Gate 4 callers;
        the object form returns a typed append receipt for store users.
        """

        if isinstance(event_name, StepEvent):
            if data is not None:
                raise TypeError("StepEvent append 不得同時提供 data")
            return self.append_event(event_name)
        if data is None:
            return self.validator.validate(event_name, None)
        if event_name != TIMELINE_EVENT_NAME:
            # Keep unknown named events on the existing drop/audit path.
            return self.validator.validate(event_name, data)
        validated = self._validated_event(data)
        if validated is None:
            return None
        receipt = self._require_store().append(validated, binding=self.context)
        return self._frame(receipt.event)

    ingest = append
    process = append
    handle = append

    def replay_events(self, *, after_cursor: int | str = 0) -> tuple[StepEvent, ...]:
        """Return trusted stored events strictly after ``after_cursor``."""

        return self._require_store().read_after(
            binding=self.context, after_cursor=after_cursor
        )

    def replay(self, *, after_cursor: int | str = 0) -> tuple[str, ...]:
        """Return replayed events as canonical named SSE frames."""

        return tuple(self._frame(event) for event in self.replay_events(after_cursor=after_cursor))

    read_after = replay_events

    def terminal_event(self) -> StepEvent | None:
        """Return the one terminal event, if the store has one."""

        return self._require_store().terminal(binding=self.context)

    def append_terminal(
        self,
        status: StepStatus | str,
        *,
        event_id: str | None = None,
        safe_output_summary: str | None = None,
        completed_at: datetime | None = None,
    ) -> SessionAppendResult:
        """Append a CSP-authored terminal exactly once.

        The default event id is deterministic per run/status.  Thus a retry
        after a process restart resolves to the stored event rather than
        creating a second terminal or conflicting on a fresh timestamp.
        """

        terminal_status = status if isinstance(status, StepStatus) else StepStatus(status)
        if terminal_status not in {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
        }:
            raise ValueError("terminal status 必須是 completed/failed/cancelled")
        if safe_output_summary is not None:
            if len(safe_output_summary) > MAX_SAFE_SUMMARY_CHARS:
                raise ValueError("terminal safe summary 超過大小上限")
            if _SECRET_PATTERN.search(safe_output_summary):
                raise ValueError("terminal safe summary 可能包含 secret")
        store = self._require_store()
        deterministic_event_id = event_id or f"terminal:{self.context.run_id}:{terminal_status.value}"
        existing = store.terminal(binding=self.context)
        if existing is not None:
            if event_id is not None and event_id != existing.event_id:
                raise TerminalConflictError(
                    "terminal retry event_id 不一致",
                    run_id=self.context.run_id,
                    event_id=event_id,
                )
            if (
                existing.status is not terminal_status
                or existing.safe_output_summary != safe_output_summary
            ):
                raise TerminalConflictError(
                    "terminal retry status/summary 不一致",
                    run_id=self.context.run_id,
                    event_id=existing.event_id,
                )
            return SessionAppendResult(existing, duplicate=True)
        event = StepEvent(
            event_id=deterministic_event_id,
            # A fixed terminal source sequence keeps retries with the
            # deterministic event id idempotent.  The authoritative cursor is
            # still assigned by the store.
            sequence=2_147_483_647,
            cursor=f"terminal:{terminal_status.value}",
            trace_id=self.context.trace_id,
            task_id=self.context.task_id,
            session_id=self.context.session_id,
            invocation_id=self.context.invocation_id or self.context.run_id,
            run_id=self.context.run_id,
            step_id=f"agent:{self.context.agent_id}",
            kind=StepKind.AGENT,
            status=terminal_status,
            safe_output_summary=safe_output_summary,
            agent_id=self.context.agent_id,
            completed_at=completed_at,
            classification=self.context.classification,
        )
        # Bypass the live budget for a CSP-authored terminal, but still pass
        # through the same trusted store binding and exactly-once constraint.
        return store.append(event, binding=self.context)


class BridgeCore(StreamBridge):
    """Compatibility name for the Gate 4 thin core, now store-aware."""


def cancelled_terminal_frame(context: BridgeContext) -> str:
    """Create CSP-authored single terminal event after downstream cancellation."""
    event = StepEvent(
        event_id=uuid.uuid4().hex,
        sequence=2_147_483_647,
        cursor="cancelled",
        trace_id=context.trace_id,
        task_id=context.task_id,
        session_id=context.session_id,
        invocation_id=context.invocation_id or context.run_id,
        run_id=context.run_id,
        step_id=f"agent:{context.agent_id}",
        kind=StepKind.AGENT,
        status=StepStatus.CANCELLED,
        safe_output_summary="執行已取消",
        agent_id=context.agent_id,
        completed_at=datetime.now(timezone.utc),
        classification=context.classification,
    )
    return f"event: {TIMELINE_EVENT_NAME}\ndata: {event.model_dump_json()}\n\n"


__all__ = [
    "AgentCallBinding",
    "AgentClient",
    "BridgeContext",
    "BridgeCore",
    "CspProxyAgentClient",
    "CspProxyRequest",
    "CspProxyRequiredError",
    "CspSessionEventStore",
    "EventBindingError",
    "EventConflictError",
    "EventOrderError",
    "InMemorySessionEventStore",
    "MAX_EVENT_BYTES",
    "MAX_EVENTS_PER_RUN",
    "MAX_EVENTS_PER_SECOND",
    "SessionAppendResult",
    "SessionEventStore",
    "StoreNotConfiguredError",
    "StreamBridge",
    "StreamBridgeError",
    "StreamValidator",
    "TIMELINE_EVENT_NAME",
    "TerminalEventError",
    "TerminalConflictError",
    "cancelled_terminal_frame",
]
