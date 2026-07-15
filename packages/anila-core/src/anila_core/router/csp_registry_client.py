"""CSP-owned registry and Agent dispatch adapters for Router R3.

The Router is deliberately a consumer of a caller-scoped CSP projection.  It
does not read the CSP database, trust the legacy ``/v1/agents`` endpoint, or
use an Agent's advertised endpoint URL.  This module keeps the transport
boundary small and testable; all authority checks happen again in the local
``CapabilityFilter``/``DecisionEngine``/``PolicyGate`` pipeline.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from anila_contracts import (
    AgentManifest,
    ExecutionGrant,
    PolicyGateResult,
    RouteDecision,
)
from anila_contracts.agents import ModelBinding
from anila_contracts.classification import ClassificationLevel

from .candidate_filter import RegistryEntry, RegistrySnapshot
from .policy_gate import ExecutionGrantInput


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]*$")
_PLACEHOLDER_TOKENS = frozenset(
    {"", "not-set", "changeme", "dev-service-token", "dev-secret-key-change-in-prod"}
)


def _validate_resume_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentClientError("resume 缺少 idempotency key")
    normalized = value.strip()
    if len(normalized) > 255 or any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in normalized
    ):
        raise AgentClientError("resume idempotency key 格式無效")
    return normalized


class RegistryClientError(RuntimeError):
    """Raised when the CSP authority snapshot cannot be obtained safely."""


class AgentClientError(RuntimeError):
    """Raised when a CSP Agent proxy call cannot be completed safely."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GrantMintUnavailable(RuntimeError):
    """Raised when CSP has not issued a grant for a formal Agent call."""


@dataclass(frozen=True, slots=True)
class ExecutionGrantEnvelope:
    """The CSP transport envelope around an unsigned inner grant.

    ``anila-contracts.ExecutionGrant`` intentionally has no cryptographic
    material.  The token is kept beside it (never injected into the inner
    model) so the Router can carry the exact RS256 envelope to the CSP final
    dispatch sink.
    """

    token: str
    grant: ExecutionGrant
    schema_version: str = "execution-grant-envelope/v1"
    token_type: str = "Bearer"

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("CSP ExecutionGrant envelope 缺少 signed token")
        if not isinstance(self.grant, ExecutionGrant):
            raise TypeError("ExecutionGrant envelope grant 必須是 ExecutionGrant")
        if self.schema_version != "execution-grant-envelope/v1":
            raise ValueError("ExecutionGrant envelope schema_version 無效")
        if self.token_type != "Bearer":
            raise ValueError("ExecutionGrant envelope token_type 無效")


class ExecutionGrantMinter(Protocol):
    """CSP mint seam; the Router never signs or fabricates a grant."""

    async def mint(
        self,
        *,
        grant_input: ExecutionGrantInput,
        decision: RouteDecision,
        policy_result: PolicyGateResult,
        snapshot: RegistrySnapshot,
        entry: RegistryEntry,
        caller_user_id: int,
    ) -> ExecutionGrantEnvelope | None: ...


class NoopExecutionGrantMinter:
    """Explicit R2 blocker until a CSP grant-mint endpoint is available."""

    async def mint(self, **_: Any) -> ExecutionGrantEnvelope | None:
        return None


class AgentClient(Protocol):
    """Only CSP-backed Agent transport accepted by the formal Router."""

    async def complete(
        self,
        *,
        query: str,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
        stream: bool = False,
    ) -> dict[str, Any]: ...

    async def stream(
        self,
        *,
        query: str,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def resume(
        self,
        *,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
        idempotency_key: str,
        approval_mode: str = "approve_all",
    ) -> dict[str, Any]: ...

    async def resume_by_session(
        self,
        *,
        session_id: str,
        caller_user_id: int,
        idempotency_key: str,
        approval_mode: str = "approve_all",
    ) -> dict[str, Any]: ...


class InferenceClient(Protocol):
    """CSP-owned primary-model inference transport for formal Router calls."""

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context: Any,
    ) -> dict[str, Any]: ...

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context: Any,
    ) -> AsyncIterator[dict[str, Any]]: ...


class _CspBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _RegistryBaseModel(_CspBaseModel):
    id: StrictInt = Field(gt=0)
    name: StrictStr
    display_name: StrictStr
    is_active: StrictBool
    health_status: StrictStr
    health_checked_at: datetime | None = None
    classification_ceiling: StrictStr


class _RegistryEntryModel(_CspBaseModel):
    """Strict mirror of CSP's internal AgentRegistryEntry response."""

    agent_id: StrictStr
    registry_id: StrictInt = Field(gt=0)
    name: StrictStr
    snapshot_id: StrictStr
    is_active: StrictBool
    manifest: AgentManifest | None = None
    manifest_sha256: StrictStr
    manifest_revision: StrictStr
    approval_status: StrictStr
    health_status: StrictStr
    health_checked_at: datetime | None = None
    audit_level: StrictStr
    trace_callback_mode: StrictStr
    trace_test_passed_at: datetime | None = None
    trace_test_governance_fingerprint: StrictStr
    classification_ceiling: StrictStr
    default_classification_level: StrictStr
    base_model: _RegistryBaseModel | None = None
    ready_for_dispatch: StrictBool
    approved: StrictBool
    health_ready: StrictBool
    trace_test_passed: StrictBool
    manifest_valid: StrictBool
    endpoint_via_csp: StrictBool
    approval_required: StrictBool
    required_obligations: tuple[StrictStr, ...] = ()
    model_gateway: StrictStr
    reason_codes: tuple[StrictStr, ...] = ()


class _RegistrySnapshotModel(_CspBaseModel):
    """Strict mirror of CSP's internal AgentRegistrySnapshot response."""

    schema_version: str
    snapshot_id: StrictStr
    snapshot_revision: StrictStr
    snapshot_hash: StrictStr
    registry_snapshot_id: StrictStr
    fetched_at: datetime
    expires_at: datetime
    caller_user_id: StrictInt = Field(gt=0)
    agents: tuple[_RegistryEntryModel, ...]


def _hash(value: str, *, field_name: str) -> str:
    if not _HEX64.fullmatch(value):
        raise RegistryClientError(f"{field_name} 格式無效")
    return value


def _revision(value: str, *, field_name: str) -> str:
    # R2's current authority deliberately uses the same SHA-256 generation for
    # id/revision/hash.  Keep that equality check in the adapter rather than
    # silently accepting an older or mixed-generation payload.
    return _hash(value, field_name=field_name)


def _aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RegistryClientError(f"{field_name} 必須是 timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _classification(value: str, *, field_name: str) -> ClassificationLevel:
    try:
        return ClassificationLevel.from_storage(value)
    except (TypeError, ValueError) as exc:
        raise RegistryClientError(f"{field_name} 分類無效") from exc


def _manifest_hash(value: str, *, required: bool = False) -> str | None:
    if not value:
        if required:
            raise RegistryClientError("ready Agent 缺少 manifest_sha256")
        return None
    return _hash(value, field_name="manifest_sha256")


def _manifest_revision(value: str, *, required: bool = False) -> str | None:
    if not value:
        if required:
            raise RegistryClientError("ready Agent 缺少 manifest_revision")
        return None
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise RegistryClientError("manifest_revision 格式無效")
    return value


def _strict_positive_user_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RegistryClientError("caller_user_id 必須是 positive integer")
    return value


def _model_binding(manifest: AgentManifest | None) -> ModelBinding | None:
    return manifest.model_binding if manifest is not None else None


def _entry_to_router(entry: _RegistryEntryModel, *, snapshot_id: str) -> RegistryEntry:
    if entry.snapshot_id != snapshot_id:
        raise RegistryClientError("Agent entry snapshot_id 與 authority generation 不一致")
    if entry.manifest is not None and entry.manifest.agent_id != entry.agent_id:
        raise RegistryClientError("Agent manifest agent_id 不一致")
    manifest_hash = _manifest_hash(entry.manifest_sha256, required=entry.ready_for_dispatch)
    manifest_revision = _manifest_revision(
        entry.manifest_revision,
        required=entry.ready_for_dispatch,
    )
    ceiling: ClassificationLevel | None = None
    if entry.classification_ceiling:
        ceiling = _classification(
            entry.classification_ceiling,
            field_name="classification_ceiling",
        )
    if entry.ready_for_dispatch:
        if entry.manifest is None or not entry.manifest_valid:
            raise RegistryClientError("ready Agent 缺少有效 canonical manifest")
        if not entry.endpoint_via_csp or entry.model_gateway != "csp":
            raise RegistryClientError("ready Agent 必須透過 CSP model gateway")
        if ceiling is None:
            raise RegistryClientError("ready Agent 缺少 classification ceiling")
        if _model_binding(entry.manifest) is None:
            raise RegistryClientError("ready Agent 缺少 CSP model binding")

    return RegistryEntry(
        agent_id=entry.agent_id,
        manifest=entry.manifest,
        snapshot_id=snapshot_id,
        manifest_revision=manifest_revision,
        manifest_sha256=manifest_hash,
        approved=entry.approved,
        health_ready=entry.health_ready,
        trace_test_passed=entry.trace_test_passed,
        ready_for_dispatch=entry.ready_for_dispatch,
        manifest_valid=entry.manifest_valid,
        endpoint_via_csp=entry.endpoint_via_csp,
        approval_required=entry.approval_required,
        required_obligations=tuple(entry.required_obligations),
        required_scopes=tuple(entry.manifest.required_scopes) if entry.manifest else (),
        classification_ceiling=ceiling,
        model_binding=_model_binding(entry.manifest),
        model_gateway=entry.model_gateway,
    )


def parse_registry_snapshot(payload: Mapping[str, Any]) -> RegistrySnapshot:
    """Strictly parse an R2 payload into the R3 immutable consumer view."""

    try:
        wire = _RegistrySnapshotModel.model_validate(dict(payload))
    except Exception as exc:
        raise RegistryClientError("CSP AgentRegistrySnapshot schema 無效") from exc
    if wire.schema_version != "agent-registry/v1":
        raise RegistryClientError("CSP registry schema_version 不支援")
    snapshot_id = _hash(wire.snapshot_id, field_name="snapshot_id")
    revision = _revision(wire.snapshot_revision, field_name="snapshot_revision")
    snapshot_hash = _hash(wire.snapshot_hash, field_name="snapshot_hash")
    registry_id = _hash(wire.registry_snapshot_id, field_name="registry_snapshot_id")
    if not (snapshot_id == revision == snapshot_hash == registry_id):
        raise RegistryClientError("CSP registry snapshot generation identity 不一致")
    fetched = _aware(wire.fetched_at, field_name="fetched_at")
    expires = _aware(wire.expires_at, field_name="expires_at")
    if expires <= fetched:
        raise RegistryClientError("CSP registry snapshot expires_at 無效")
    caller_user_id = _strict_positive_user_id(wire.caller_user_id)
    entries = tuple(_entry_to_router(item, snapshot_id=snapshot_id) for item in wire.agents)
    return RegistrySnapshot(
        snapshot_id,
        entries,
        fresh=True,
        stale=False,
        authority="csp",
        captured_at=fetched,
        expires_at=expires,
        snapshot_revision=revision,
        snapshot_hash=snapshot_hash,
        caller_user_id=caller_user_id,
    )


class CspRegistryClient:
    """Named service-client adapter for the caller-scoped CSP registry."""

    def __init__(
        self,
        csp_base_url: str,
        *,
        service_token: str | None,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = csp_base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CSP base URL 必須是絕對 HTTP(S) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("CSP base URL 不得包含 userinfo/path/query/fragment")
        self.service_token = (service_token or "").strip()
        self.timeout = timeout
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        """Whether this transport carries a deployable named service token."""

        token = self.service_token.strip()
        return bool(
            token.startswith("csk-")
            and token.lower()
            not in _PLACEHOLDER_TOKENS
        )

    def _headers(self, caller_user_id: int) -> dict[str, str]:
        _strict_positive_user_id(caller_user_id)
        if self.service_token.lower() in _PLACEHOLDER_TOKENS or not self.service_token:
            raise RegistryClientError("缺少具名 CSP service-client token")
        return {
            # CSP's internal dependency deliberately reads the dedicated
            # service-token header, not a user API-key Bearer header.
            "X-CSP-Service-Token": self.service_token,
            "Content-Type": "application/json",
            "X-ANILA-Caller-User-Id": str(caller_user_id),
        }

    async def fetch_snapshot(self, caller_user_id: int) -> RegistrySnapshot:
        headers = self._headers(caller_user_id)
        url = f"{self.base_url}/internal/v1/agents/registry"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except RegistryClientError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise RegistryClientError("CSP registry snapshot 取得失敗") from exc
        if not isinstance(payload, Mapping):
            raise RegistryClientError("CSP registry response 必須是 JSON object")
        snapshot = parse_registry_snapshot(payload)
        if snapshot.caller_user_id != caller_user_id:
            raise RegistryClientError("CSP registry caller_user_id mismatch")
        return snapshot


@dataclass(frozen=True)
class CspAgentRequest:
    url: str
    payload: dict[str, Any]
    headers: dict[str, str]


@dataclass(frozen=True)
class CspInferenceRequest:
    url: str
    payload: dict[str, Any]
    headers: dict[str, str]


class CspInferenceClient:
    """Call CSP's internal Router model-inference seam.

    The inbound user bearer is deliberately not accepted as a downstream
    credential.  CSP authenticates this request with a named ``router``
    service-client token and rebinds the positive caller id/task context to
    durable authority before selecting the ordinary model gateway.
    """

    def __init__(
        self,
        csp_base_url: str,
        *,
        service_token: str | None,
        inference_path: str = "/internal/v1/router/chat/completions",
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = csp_base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CSP base URL 必須是絕對 HTTP(S) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("CSP base URL 不得包含 userinfo/path/query/fragment")
        if not inference_path.startswith("/") or "?" in inference_path or "#" in inference_path:
            raise ValueError("CSP inference path 必須是 origin-relative path")
        self.service_token = (service_token or "").strip()
        self.inference_path = inference_path
        self.timeout = timeout
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        """Whether this transport carries a deployable named service token."""

        token = self.service_token.strip()
        return bool(
            token.startswith("csk-")
            and token.lower()
            not in _PLACEHOLDER_TOKENS
        )

    @staticmethod
    def _text(value: object, *, name: str) -> str:
        if not isinstance(value, str):
            raise AgentClientError(f"{name} 必須是非空單行字串")
        text = value.strip()
        if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise AgentClientError(f"{name} 必須是非空單行字串")
        return text

    @staticmethod
    def _positive(value: object, *, name: str) -> str:
        text = CspAgentClient._positive_wire(value, name=name)
        return text

    def build_request(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context: Any,
        stream: bool,
    ) -> CspInferenceRequest:
        if self.service_token.lower() in _PLACEHOLDER_TOKENS or not self.service_token:
            raise AgentClientError("缺少具名 Router inference service-client token")
        if not isinstance(model, str) or not model.strip():
            raise AgentClientError("inference model 不得為空")
        if not isinstance(messages, list):
            raise AgentClientError("inference messages 必須是 list")
        if context is None:
            raise AgentClientError("formal inference 缺少 RequestContext")
        identity = self._positive(getattr(context, "identity", None), name="caller_user_id")
        owner = self._positive(getattr(context, "owner_id", None), name="owner_id")
        task_id = self._positive(getattr(context, "task_id", None), name="task_id")
        run_id = self._positive(getattr(context, "run_id", None), name="run_id")
        source = self._positive(
            getattr(context, "source_snapshot_id", None), name="source_snapshot_id"
        )
        trace_id = self._text(getattr(context, "trace_id", None), name="trace_id")
        invocation_id = self._text(
            getattr(context, "invocation_id", None), name="invocation_id"
        )
        session_id = self._text(getattr(context, "session_id", None), name="session_id")
        task_type = self._text(getattr(context, "task_type", None), name="task_type")
        classification_obj = getattr(context, "classification", None)
        classification = (
            classification_obj.to_storage()
            if isinstance(classification_obj, ClassificationLevel)
            else self._text(classification_obj, name="classification")
        )
        scopes = tuple(getattr(context, "scopes", ()) or ())
        capabilities = tuple(getattr(context, "required_capabilities", ()) or ())
        if not scopes:
            raise AgentClientError("formal inference 缺少 scopes")
        auth = getattr(context, "auth_assurance", None)
        if auth is None:
            raise AgentClientError("formal inference 缺少 auth assurance")
        if hasattr(auth, "model_dump"):
            auth_payload = auth.model_dump(mode="json")
        elif isinstance(auth, Mapping):
            auth_payload = dict(auth)
        else:
            raise AgentClientError("auth assurance 格式無效")
        auth_json = json.dumps(auth_payload, ensure_ascii=False, separators=(",", ":"))
        headers = {
            "X-CSP-Service-Token": self.service_token,
            "Content-Type": "application/json",
            "X-ANILA-Caller-User-Id": identity,
            "X-ANILA-Owner-Id": owner,
            "X-ANILA-Task-Id": task_id,
            "X-ANILA-Run-Id": run_id,
            "X-ANILA-Source-Snapshot-Id": source,
            "X-ANILA-Trace-Id": trace_id,
            "X-ANILA-Invocation-Id": invocation_id,
            "X-ANILA-Session-Id": session_id,
            "X-ANILA-Task-Type": task_type,
            "X-ANILA-Classification-Level": quote(classification, safe=""),
            "X-ANILA-Scopes": ",".join(self._text(item, name="scope") for item in scopes),
            "X-ANILA-Required-Capabilities": ",".join(
                self._text(item, name="required_capability") for item in capabilities
            ),
            "X-ANILA-Auth-Assurance": quote(auth_json, safe=""),
        }
        payload = {
            "model": model,
            "messages": messages,
            "stream": bool(stream),
            "anila_session_id": session_id,
        }
        return CspInferenceRequest(
            url=f"{self.base_url}{self.inference_path}",
            payload=payload,
            headers=headers,
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context: Any,
    ) -> dict[str, Any]:
        request = self.build_request(
            model=model, messages=messages, context=context, stream=False
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    request.url, json=request.payload, headers=request.headers
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AgentClientError("CSP internal inference call 失敗") from exc
        if not isinstance(data, Mapping):
            raise AgentClientError("CSP internal inference response 必須是 JSON object")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentClientError("CSP inference response shape 無效") from exc
        if not isinstance(message, Mapping):
            raise AgentClientError("CSP inference message 格式無效")
        content = message.get("content") or ""
        if not isinstance(content, str):
            raise AgentClientError("CSP inference content 必須是字串")
        reasoning_raw = message.get("reasoning_content") or message.get("reasoning") or ""
        reasoning = reasoning_raw.strip() if isinstance(reasoning_raw, str) else ""
        return {
            "content": content,
            "reasoning": reasoning or None,
            "anila_meta": data.get("anila_meta"),
            "raw": dict(data),
            "error": None,
        }

    async def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        context: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        request = self.build_request(
            model=model, messages=messages, context=context, stream=True
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST", request.url, json=request.payload, headers=request.headers
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        yield {"type": "error", "error": f"CSP inference HTTP {response.status_code}"}
                        return
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].lstrip()
                        if raw == "[DONE]":
                            yield {"type": "done"}
                            return
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, Mapping):
                            continue
                        choices = chunk.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        first = choices[0]
                        delta = first.get("delta", {}) if isinstance(first, Mapping) else {}
                        if not isinstance(delta, Mapping):
                            continue
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            yield {"type": "reasoning", "content": reasoning}
                        content = delta.get("content")
                        if isinstance(content, str) and content:
                            yield {"type": "delta", "content": content}
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            yield {"type": "error", "error": f"CSP inference stream 失敗: {type(exc).__name__}"}


class CspExecutionGrantMinter:
    """Call CSP's signed ExecutionGrant mint seam.

    The Router submits unsigned evidence and receives a transport envelope.
    It never creates a local ``ExecutionGrant`` as an authority substitute
    and never places the signed token inside that inner contract.
    """

    def __init__(
        self,
        csp_base_url: str,
        *,
        service_token: str | None,
        mint_path: str = "/internal/v1/execution-grants/mint",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = csp_base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CSP base URL 必須是絕對 HTTP(S) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("CSP base URL 不得包含 userinfo/path/query/fragment")
        if not mint_path.startswith("/") or "?" in mint_path or "#" in mint_path:
            raise ValueError("CSP ExecutionGrant mint path 必須是 origin-relative path")
        self.service_token = (service_token or "").strip()
        self.mint_path = mint_path
        self.timeout = timeout
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        token = self.service_token.strip()
        return bool(token.startswith("csk-") and token.lower() not in _PLACEHOLDER_TOKENS)

    @staticmethod
    def _payload(
        *,
        grant_input: ExecutionGrantInput,
        decision: RouteDecision,
        policy_result: PolicyGateResult,
        snapshot: RegistrySnapshot,
        entry: RegistryEntry,
        caller_user_id: int,
    ) -> dict[str, Any]:
        if isinstance(caller_user_id, bool) or not isinstance(caller_user_id, int) or caller_user_id <= 0:
            raise GrantMintUnavailable("caller_user_id 必須是 positive integer")
        if snapshot.snapshot_id != decision.registry_snapshot_id:
            raise GrantMintUnavailable("grant mint snapshot/decision mismatch")
        if entry.agent_id != grant_input.target_agent_id:
            raise GrantMintUnavailable("grant mint target mismatch")
        if not entry.manifest_sha256 or not entry.manifest_revision:
            raise GrantMintUnavailable("grant mint 缺少 manifest identity")
        return {
            "task_id": grant_input.task_id,
            "run_id": grant_input.run_id,
            "source_snapshot_id": grant_input.source_snapshot_id,
            "trace_id": grant_input.trace_id,
            "invocation_id": grant_input.invocation_id,
            "session_id": grant_input.session_id,
            "registry_snapshot_id": snapshot.snapshot_id,
            "registry_snapshot_revision": snapshot.snapshot_revision or snapshot.snapshot_id,
            "registry_snapshot_hash": snapshot.snapshot_hash or snapshot.snapshot_id,
            "target_agent_id": entry.agent_id,
            "manifest_revision": entry.manifest_revision,
            "manifest_sha256": entry.manifest_sha256,
            "classification": grant_input.classification.to_storage(),
            "auth_assurance": grant_input.auth_assurance.model_dump(mode="json"),
            "model_binding": grant_input.model_binding.model_dump(mode="json"),
            "allowed_capabilities": list(grant_input.allowed_capabilities),
            "allowed_scopes": list(grant_input.allowed_scopes),
            "route_decision": decision.model_dump(mode="json"),
            "policy_result": policy_result.model_dump(mode="json"),
            "ttl_seconds": 60,
        }

    def build_request(
        self,
        *,
        grant_input: ExecutionGrantInput,
        decision: RouteDecision,
        policy_result: PolicyGateResult,
        snapshot: RegistrySnapshot,
        entry: RegistryEntry,
        caller_user_id: int,
    ) -> CspAgentRequest:
        if not self.is_configured:
            raise GrantMintUnavailable("缺少具名 Router service-client token")
        payload = self._payload(
            grant_input=grant_input,
            decision=decision,
            policy_result=policy_result,
            snapshot=snapshot,
            entry=entry,
            caller_user_id=caller_user_id,
        )
        return CspAgentRequest(
            url=f"{self.base_url}{self.mint_path}",
            payload=payload,
            headers={
                "X-CSP-Service-Token": self.service_token,
                "X-ANILA-Caller-User-Id": str(caller_user_id),
                "Content-Type": "application/json",
            },
        )

    async def mint(
        self,
        *,
        grant_input: ExecutionGrantInput,
        decision: RouteDecision,
        policy_result: PolicyGateResult,
        snapshot: RegistrySnapshot,
        entry: RegistryEntry,
        caller_user_id: int,
    ) -> ExecutionGrantEnvelope | None:
        request = self.build_request(
            grant_input=grant_input,
            decision=decision,
            policy_result=policy_result,
            snapshot=snapshot,
            entry=entry,
            caller_user_id=caller_user_id,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    request.url, json=request.payload, headers=request.headers
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise GrantMintUnavailable("CSP ExecutionGrant mint call 失敗") from exc
        if not isinstance(data, Mapping):
            raise GrantMintUnavailable("CSP ExecutionGrant mint response 必須是 JSON object")
        token = data.get("token")
        raw_grant = data.get("grant")
        if not isinstance(token, str) or not token.strip() or not isinstance(raw_grant, Mapping):
            raise GrantMintUnavailable("CSP mint response 缺少 signed envelope")
        try:
            grant = ExecutionGrant.model_validate(raw_grant)
            envelope = ExecutionGrantEnvelope(
                token=token,
                grant=grant,
                schema_version=str(data.get("schema_version") or ""),
                token_type=str(data.get("token_type") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise GrantMintUnavailable("CSP mint response envelope schema 無效") from exc
        # The response is not authority until the final CSP sink verifies the
        # JWT.  Still fail closed if the inner response is not the evidence we
        # just submitted; this prevents accidental cross-request reuse.
        if (
            grant.task_id != grant_input.task_id
            or grant.run_id != grant_input.run_id
            or grant.trace_id != grant_input.trace_id
            or grant.invocation_id != grant_input.invocation_id
            or grant.source_snapshot_id != grant_input.source_snapshot_id
            or grant.registry_snapshot_id != snapshot.snapshot_id
            or grant.target.id != entry.agent_id
            or grant.manifest_revision != entry.manifest_revision
            or grant.route_decision_id != decision.decision_id
            or grant.policy_decision_id != policy_result.decision_id
            or grant.session_id != grant_input.session_id
        ):
            raise GrantMintUnavailable("CSP mint response binding mismatch")
        return envelope


class CspAgentClient:
    """CSP-only Agent transport carrying the complete R2/R3 binding.

    The public user bearer is deliberately *not* a downstream credential.
    Final Agent dispatch waits for a named Router service-client token and a
    dedicated CSP internal seam; until that seam is deployed this adapter
    fails closed before network I/O.
    """

    def __init__(
        self,
        csp_base_url: str,
        *,
        service_token: str | None = None,
        dispatch_path: str = "/internal/v1/agents/dispatch",
        resume_path: str | None = None,
        resume_by_session_path: str | None = None,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = csp_base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CSP base URL 必須是絕對 HTTP(S) origin")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("CSP base URL 不得包含 userinfo/path/query/fragment")
        if not dispatch_path.startswith("/") or "?" in dispatch_path or "#" in dispatch_path:
            raise ValueError("CSP Agent dispatch path 必須是 origin-relative path")
        resolved_resume_path = resume_path or f"{dispatch_path.rstrip('/')}/resume"
        if not resolved_resume_path.startswith("/") or "?" in resolved_resume_path or "#" in resolved_resume_path:
            raise ValueError("CSP Agent resume path 必須是 origin-relative path")
        resolved_resume_by_session_path = resume_by_session_path or (
            f"{dispatch_path.rstrip('/').rsplit('/', 1)[0]}/resume-by-session"
        )
        if (
            not resolved_resume_by_session_path.startswith("/")
            or "?" in resolved_resume_by_session_path
            or "#" in resolved_resume_by_session_path
        ):
            raise ValueError("CSP Agent resume-by-session path 必須是 origin-relative path")
        self.service_token = (service_token or "").strip()
        self.dispatch_path = dispatch_path
        self.resume_path = resolved_resume_path
        self.resume_by_session_path = resolved_resume_by_session_path
        self.timeout = timeout
        self.transport = transport

    @property
    def is_configured(self) -> bool:
        """Whether this transport carries a deployable named service token."""

        token = self.service_token.strip()
        return bool(
            token.startswith("csk-")
            and token.lower()
            not in _PLACEHOLDER_TOKENS
        )

    @staticmethod
    def _positive_wire(value: object, *, name: str) -> str:
        if isinstance(value, bool):
            raise AgentClientError(f"{name} 必須是 positive numeric id")
        text = str(value).strip()
        if not _POSITIVE_DECIMAL.fullmatch(text):
            raise AgentClientError(f"{name} 必須是 positive numeric id")
        return text

    def build_request(
        self,
        *,
        query: str,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
        stream: bool,
    ) -> CspAgentRequest:
        # ``caller_api_key`` is retained in the protocol for call-site
        # compatibility, but must never be copied into this service-to-
        # service request.  The named Router service-client token is the only
        # credential accepted by the pending internal CSP seam.
        del caller_api_key
        if self.service_token.lower() in _PLACEHOLDER_TOKENS or not self.service_token:
            raise AgentClientError(
                "缺少具名 Router service-client token；CSP internal Agent seam 未啟用"
            )
        if not isinstance(query, str) or not query.strip():
            raise AgentClientError("Agent query 不得為空")
        if entry.snapshot_id != snapshot.snapshot_id:
            raise AgentClientError("Agent entry/snapshot mismatch")
        if not isinstance(execution_grant, ExecutionGrantEnvelope):
            raise AgentClientError(
                "formal Agent call 缺少 CSP-issued signed ExecutionGrant envelope"
            )
        signed_token = execution_grant.token
        inner_grant = execution_grant.grant
        try:
            inner_grant.assert_active_at(datetime.now(timezone.utc))
        except ValueError as exc:
            raise AgentClientError("ExecutionGrant 不在有效期限") from exc
        if inner_grant.target.kind.value != "agent" or inner_grant.target.id != entry.agent_id:
            raise AgentClientError("ExecutionGrant target 與 Agent entry 不一致")
        if inner_grant.task_id != getattr(context, "task_id", None) or inner_grant.run_id != getattr(context, "run_id", None):
            raise AgentClientError("ExecutionGrant task/run binding 不一致")
        if inner_grant.trace_id != getattr(context, "trace_id", None):
            raise AgentClientError("ExecutionGrant trace binding 不一致")
        if inner_grant.registry_snapshot_id != snapshot.snapshot_id:
            raise AgentClientError("ExecutionGrant snapshot binding 不一致")
        if inner_grant.manifest_revision != entry.manifest_revision:
            raise AgentClientError("ExecutionGrant manifest binding 不一致")
        if inner_grant.route_decision_id != route_decision.decision_id:
            raise AgentClientError("ExecutionGrant route decision binding 不一致")
        if inner_grant.policy_decision_id != policy_result.decision_id:
            raise AgentClientError("ExecutionGrant policy binding 不一致")
        if grant_input.route_decision_id != route_decision.decision_id or grant_input.target_agent_id != entry.agent_id:
            raise AgentClientError("unsigned grant input binding 不一致")
        if not entry.ready_for_dispatch or not entry.endpoint_via_csp or entry.model_gateway != "csp":
            raise AgentClientError("Agent entry 未通過 CSP readiness gate")
        if not entry.manifest_revision or not entry.manifest_sha256:
            raise AgentClientError("Agent entry 缺少 manifest revision/hash")
        snapshot_revision = snapshot.snapshot_revision or snapshot.snapshot_id
        snapshot_hash = snapshot.snapshot_hash or snapshot.snapshot_id
        if snapshot_revision != snapshot.snapshot_id or snapshot_hash != snapshot.snapshot_id:
            raise AgentClientError("snapshot generation facts 不一致")
        task_id = self._positive_wire(getattr(context, "task_id", None), name="task_id")
        run_id = self._positive_wire(getattr(context, "run_id", None), name="run_id")
        owner_id = self._positive_wire(getattr(context, "owner_id", None), name="owner_id")
        source_snapshot_id = self._positive_wire(
            getattr(context, "source_snapshot_id", None), name="source_snapshot_id"
        )
        trace_id = str(getattr(context, "trace_id", "")).strip()
        invocation_id = str(getattr(context, "invocation_id", "")).strip()
        if not trace_id or not invocation_id or not session_id.strip():
            raise AgentClientError("formal Agent call 缺少 trace/session/invocation binding")
        payload = {
            "model": entry.agent_id,
            "messages": [{"role": "user", "content": query}],
            "stream": stream,
            "anila_session_id": session_id,
            "anila_binding": {
                "owner_id": owner_id,
                "task_id": task_id,
                "run_id": run_id,
                "source_snapshot_id": source_snapshot_id,
                "trace_id": trace_id,
                "invocation_id": invocation_id,
                "session_id": session_id,
                "agent_id": entry.agent_id,
                "registry_snapshot_id": snapshot.snapshot_id,
                "registry_snapshot_revision": snapshot_revision,
                "registry_snapshot_hash": snapshot_hash,
                "manifest_revision": entry.manifest_revision,
                "manifest_sha256": entry.manifest_sha256,
                "grant_id": inner_grant.grant_id,
                "route_decision_id": route_decision.decision_id,
                "policy_decision_id": policy_result.decision_id,
            },
        }
        headers = {
            "X-CSP-Service-Token": self.service_token,
            "Content-Type": "application/json",
            "X-ANILA-Caller-User-Id": str(getattr(context, "identity", "")),
            "X-ANILA-Owner-Id": owner_id,
            "X-ANILA-Task-Id": task_id,
            "X-ANILA-Run-Id": run_id,
            "X-ANILA-Source-Snapshot-Id": source_snapshot_id,
            "X-ANILA-Session-Id": session_id,
            "X-ANILA-Trace-Id": trace_id,
            "X-ANILA-Invocation-Id": invocation_id,
            "X-ANILA-Agent-Id": entry.agent_id,
            "X-ANILA-Registry-Snapshot-Id": snapshot.snapshot_id,
            "X-ANILA-Registry-Snapshot-Revision": snapshot_revision,
            "X-ANILA-Registry-Snapshot-Hash": snapshot_hash,
            "X-ANILA-Agent-Manifest-Revision": entry.manifest_revision,
            "X-ANILA-Agent-Manifest-SHA256": entry.manifest_sha256,
            "X-ANILA-Execution-Grant-Id": inner_grant.grant_id,
            "X-ANILA-Execution-Grant": signed_token,
            "X-ANILA-Idempotency-Key": invocation_id,
            "X-ANILA-Route-Decision-Id": route_decision.decision_id,
            "X-ANILA-Policy-Decision-Id": policy_result.decision_id,
            # HTTP field values are ASCII on the wire; the CSP sink unquotes
            # this canonical authority field before Classification parsing.
            "X-ANILA-Classification-Level": quote(
                inner_grant.classification.to_storage(), safe=""
            ),
        }
        if not _POSITIVE_DECIMAL.fullmatch(headers["X-ANILA-Caller-User-Id"]):
            raise AgentClientError("CSP caller user id 必須是 positive numeric id")
        return CspAgentRequest(
            url=f"{self.base_url}{self.dispatch_path}",
            payload=payload,
            headers=headers,
        )

    def build_resume_request(
        self,
        *,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
        idempotency_key: str,
        approval_mode: str = "approve_all",
    ) -> CspAgentRequest:
        """Build the body-less formal approve request through the same gate.

        Calling ``build_request`` first deliberately reuses all grant,
        registry, manifest, owner and correlation validation.  Only after that
        validation does this method replace the normal chat payload with the
        explicit R5 binary ``approval_mode`` body and resume path.
        """

        idempotency_key = _validate_resume_idempotency_key(idempotency_key)
        if approval_mode != "approve_all":
            raise AgentClientError("resume approval_mode 必須是 approve_all")
        base = self.build_request(
            query="resume",
            entry=entry,
            snapshot=snapshot,
            caller_api_key=caller_api_key,
            session_id=session_id,
            context=context,
            route_decision=route_decision,
            policy_result=policy_result,
            grant_input=grant_input,
            execution_grant=execution_grant,
            stream=False,
        )
        headers = dict(base.headers)
        headers["X-ANILA-Idempotency-Key"] = idempotency_key
        return CspAgentRequest(
            url=f"{self.base_url}{self.resume_path}",
            payload={"approval_mode": approval_mode},
            headers=headers,
        )

    def build_resume_by_session_request(
        self,
        *,
        session_id: str,
        caller_user_id: int,
        idempotency_key: str,
        approval_mode: str = "approve_all",
    ) -> CspAgentRequest:
        """Build the restart-safe opaque-session CSP resume request.

        No grant, binding, interrupt id, answer, or bearer is accepted from
        the caller. CSP resolves the durable authority by session id.
        """

        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 255:
            raise AgentClientError("resume session_id 格式無效")
        if not isinstance(caller_user_id, int) or isinstance(caller_user_id, bool) or caller_user_id <= 0:
            raise AgentClientError("resume caller_user_id 必須是 positive numeric id")
        idempotency_key = _validate_resume_idempotency_key(idempotency_key)
        if approval_mode != "approve_all":
            raise AgentClientError("resume approval_mode 必須是 approve_all")
        if not self.is_configured:
            raise AgentClientError("缺少具名 Router service-client token；CSP internal Agent seam 未啟用")
        return CspAgentRequest(
            url=f"{self.base_url}{self.resume_by_session_path}",
            payload={"session_id": session_id.strip(), "approval_mode": approval_mode},
            headers={
                "X-CSP-Service-Token": self.service_token,
                "X-ANILA-Caller-User-Id": str(caller_user_id),
                "X-ANILA-Idempotency-Key": idempotency_key,
                "Content-Type": "application/json",
            },
        )

    async def complete(
        self,
        *,
        query: str,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
        stream: bool = False,
    ) -> dict[str, Any]:
        request = self.build_request(
            query=query,
            entry=entry,
            snapshot=snapshot,
            caller_api_key=caller_api_key,
            session_id=session_id,
            context=context,
            route_decision=route_decision,
            policy_result=policy_result,
            grant_input=grant_input,
            execution_grant=execution_grant,
            stream=stream,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    request.url,
                    json=request.payload,
                    headers=request.headers,
                )
                if response.status_code == 202:
                    data = response.json()
                    if not isinstance(data, Mapping):
                        raise AgentClientError("CSP Agent pause response 必須是 JSON object")
                    return {
                        "content": "",
                        "status": "paused",
                        "anila_meta": data.get("anila_meta"),
                        "anila_events": data.get("anila_events", []),
                        "raw": dict(data),
                    }
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AgentClientError("CSP Agent proxy call 失敗") from exc
        if not isinstance(data, Mapping):
            raise AgentClientError("CSP Agent response 必須是 JSON object")
        try:
            message = data["choices"][0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentClientError("CSP Agent response shape 無效") from exc
        if not isinstance(content, str):
            raise AgentClientError("CSP Agent content 必須是字串")
        return {"content": content, "anila_meta": data.get("anila_meta"), "raw": dict(data)}

    async def stream(
        self,
        *,
        query: str,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
    ) -> AsyncIterator[dict[str, Any]]:
        request = self.build_request(
            query=query,
            entry=entry,
            snapshot=snapshot,
            caller_api_key=caller_api_key,
            session_id=session_id,
            context=context,
            route_decision=route_decision,
            policy_result=policy_result,
            grant_input=grant_input,
            execution_grant=execution_grant,
            stream=True,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST", request.url, json=request.payload, headers=request.headers
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        yield {"type": "error", "error": f"CSP Agent HTTP {response.status_code}"}
                        return
                    content = ""
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].lstrip()
                        if raw == "[DONE]":
                            yield {"type": "done"}
                            return
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, Mapping):
                            continue
                        choices = chunk.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        delta = choices[0].get("delta", {}) if isinstance(choices[0], Mapping) else {}
                        piece = delta.get("content") if isinstance(delta, Mapping) else None
                        if isinstance(piece, str) and piece:
                            content += piece
                            yield {"type": "content", "content": piece}
                        if isinstance(chunk.get("anila_meta"), Mapping):
                            yield {"type": "meta", "anila_meta": dict(chunk["anila_meta"])}
                    yield {"type": "done", "content": content}
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            yield {"type": "error", "error": f"CSP Agent stream 失敗: {type(exc).__name__}"}

    async def resume(
        self,
        *,
        entry: RegistryEntry,
        snapshot: RegistrySnapshot,
        caller_api_key: str,
        session_id: str,
        context: Any,
        route_decision: RouteDecision,
        policy_result: PolicyGateResult,
        grant_input: ExecutionGrantInput,
        execution_grant: ExecutionGrantEnvelope,
        idempotency_key: str,
        approval_mode: str = "approve_all",
    ) -> dict[str, Any]:
        request = self.build_resume_request(
            entry=entry,
            snapshot=snapshot,
            caller_api_key=caller_api_key,
            session_id=session_id,
            context=context,
            route_decision=route_decision,
            policy_result=policy_result,
            grant_input=grant_input,
            execution_grant=execution_grant,
            idempotency_key=idempotency_key,
            approval_mode=approval_mode,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    request.url,
                    json=request.payload,
                    headers=request.headers,
                )
                if response.status_code == 409:
                    raise AgentClientError("CSP Agent resume idempotency/lifecycle conflict")
                response.raise_for_status()
                data = response.json()
        except AgentClientError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AgentClientError("CSP Agent resume call 失敗") from exc
        if not isinstance(data, Mapping):
            raise AgentClientError("CSP Agent resume response 必須是 JSON object")
        result = {"raw": dict(data), **dict(data)}
        if data.get("status") == "paused" or response.status_code == 202:
            result.setdefault("content", "")
            result.setdefault("anila_meta", data.get("anila_meta"))
            return result
        try:
            message = data["choices"][0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentClientError("CSP Agent resume response shape 無效") from exc
        if not isinstance(content, str):
            raise AgentClientError("CSP Agent resume content 必須是字串")
        result.update({"content": content, "anila_meta": data.get("anila_meta")})
        return result

    async def resume_by_session(
        self,
        *,
        session_id: str,
        caller_user_id: int,
        idempotency_key: str,
        approval_mode: str = "approve_all",
    ) -> dict[str, Any]:
        request = self.build_resume_by_session_request(
            session_id=session_id,
            caller_user_id=caller_user_id,
            idempotency_key=idempotency_key,
            approval_mode=approval_mode,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(
                    request.url,
                    json=request.payload,
                    headers=request.headers,
                )
                if response.status_code in {401, 403}:
                    raise AgentClientError(
                        "CSP resume-by-session caller authorization denied",
                        status_code=response.status_code,
                    )
                if response.status_code == 409:
                    raise AgentClientError(
                        "CSP resume-by-session idempotency/lifecycle conflict",
                        status_code=409,
                    )
                if response.status_code >= 500:
                    raise AgentClientError(
                        "CSP resume-by-session upstream failure",
                        status_code=502,
                    )
                response.raise_for_status()
                data = response.json()
        except AgentClientError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise AgentClientError("CSP resume-by-session call 失敗") from exc
        if not isinstance(data, Mapping):
            raise AgentClientError("CSP resume-by-session response 必須是 JSON object")
        result = {"raw": dict(data), **dict(data)}
        if data.get("status") == "paused" or response.status_code == 202:
            result.setdefault("content", "")
            result.setdefault("anila_meta", data.get("anila_meta"))
            return result
        try:
            message = data["choices"][0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentClientError("CSP resume-by-session response shape 無效") from exc
        if not isinstance(content, str):
            raise AgentClientError("CSP resume-by-session content 必須是字串")
        result.update({"content": content, "anila_meta": data.get("anila_meta")})
        return result

__all__ = [
    "AgentClient",
    "AgentClientError",
    "CspAgentClient",
    "CspAgentRequest",
    "CspInferenceClient",
    "CspInferenceRequest",
    "CspRegistryClient",
    "CspExecutionGrantMinter",
    "ExecutionGrantEnvelope",
    "ExecutionGrantMinter",
    "GrantMintUnavailable",
    "InferenceClient",
    "NoopExecutionGrantMinter",
    "RegistryClientError",
    "parse_registry_snapshot",
]
