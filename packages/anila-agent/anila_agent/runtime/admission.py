"""Official Agent manifest and startup admission.

The starter template historically accepted an arbitrary OpenAI-compatible URL
as ``ANILA_BASE_URL``.  That is useful for a local experiment, but it is not a
governed Silver deployment: the model client must cross the CSP gateway.  This
module keeps the policy at the official-agent boundary and produces the
canonical :class:`anila_contracts.AgentManifest` used by registration and
health tooling.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlsplit

from anila_contracts import AgentManifest, Classification
from anila_contracts.agents import (
    AGENT_MANIFEST_SCHEMA_VERSION,
    ManifestClassification,
    ModelBinding,
    RuntimeType,
)

from anila_agent.config import AppConfig


class AgentAdmissionError(ValueError):
    """Raised when an official Agent cannot be admitted by CSP policy."""


@dataclass(frozen=True)
class AgentAdmission:
    """The immutable result of startup admission."""

    manifest: AgentManifest
    csp_base_url: str
    model_base_url: str

    @property
    def canonical_manifest(self) -> str:
        return canonical_manifest_json(self.manifest)


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_PATHS = frozenset({"", "/", "/v1", "/v1/"})


def _require_url(value: str, *, field_name: str) -> tuple[str, str, int | None, str]:
    """Parse an origin-like URL and return normalized comparison parts."""

    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AgentAdmissionError(f"{field_name} 必須是絕對 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise AgentAdmissionError(f"{field_name} 不得含 userinfo")
    if parsed.query or parsed.fragment:
        raise AgentAdmissionError(f"{field_name} 不得含 query 或 fragment")
    path = parsed.path or ""
    if path not in _ALLOWED_PATHS:
        raise AgentAdmissionError(f"{field_name} 只允許 CSP origin 或 /v1 路徑")
    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    return parsed.scheme.lower(), host, port, path


def assert_csp_model_endpoint(model_base_url: str, csp_base_url: str) -> None:
    """Reject a raw model endpoint and require same-origin CSP routing.

    The model URL may be the CSP origin itself or its OpenAI-compatible
    ``/v1`` path.  Host aliases, arbitrary model-service names, userinfo and
    path traversal are not accepted.  HTTP remains valid for an explicitly
    configured local/dev CSP; production TLS policy belongs to deployment
    profiles, not this package-level URL identity check.
    """

    model = _require_url(model_base_url, field_name="ANILA_BASE_URL")
    csp = _require_url(csp_base_url, field_name="CSP_BASE_URL")
    if model[:3] != csp[:3]:
        raise AgentAdmissionError(
            "ANILA_BASE_URL 必須指向 CSP gateway（scheme/host/port 必須與 CSP_BASE_URL 相同）；"
            "禁止直接連線裸模型 endpoint"
        )


def _runtime_version() -> str:
    try:
        return version("anila-agent")
    except PackageNotFoundError:
        return "1.0.0"


def _classification(raw: str | None, *, field_name: str) -> Classification:
    value = raw or Classification.UNCLASSIFIED.value
    try:
        return Classification.from_storage(value)
    except ValueError as exc:
        raise AgentAdmissionError(f"{field_name} 分類等級無效: {value!r}") from exc


def _agent_id(cfg: AppConfig) -> str:
    candidate = os.getenv("ANILA_AGENT_ID") or cfg.agent.name.strip().lower().replace(" ", "-")
    if not _IDENTIFIER.fullmatch(candidate):
        raise AgentAdmissionError("ANILA_AGENT_ID 必須是 canonical identifier")
    return candidate


def build_agent_manifest(
    cfg: AppConfig,
    *,
    supports_resume: bool = False,
) -> AgentManifest:
    """Build and validate the canonical ``agent-manifest/v1`` declaration."""

    ceiling = _classification(
        os.getenv("ANILA_CLASSIFICATION_CEILING") or os.getenv("ANILA_CLASSIFICATION_LEVEL"),
        field_name="classification.ceiling",
    )
    default = _classification(
        os.getenv("ANILA_CLASSIFICATION_DEFAULT"),
        field_name="classification.default",
    )
    if default > ceiling:
        raise AgentAdmissionError("classification.default 不得高於 classification.ceiling")

    model_id = cfg.model.model
    if not _IDENTIFIER.fullmatch(model_id):
        raise AgentAdmissionError("ANILA_MODEL 必須是 canonical model identifier")

    gold_requested = os.getenv("ANILA_GOLD_CONFORMANCE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if gold_requested:
        raise AgentAdmissionError(
            "Gold durable resume 不接受環境變數自我宣告；"
            "必須由 CSP service-authenticated startup admission 啟用"
        )
    capabilities: tuple[str, ...] = ("tools",)
    # A capability is declared only when the service wiring can actually
    # provide it.  A missing collection is a valid non-RAG deployment.
    collection_id = os.getenv("ANILA_COLLECTION_ID", "").strip()
    if collection_id.isdigit() and int(collection_id) > 0:
        capabilities = ("retrieval", "tools")

    manifest = AgentManifest.model_validate(
        {
            "schema_version": AGENT_MANIFEST_SCHEMA_VERSION,
            "agent_id": _agent_id(cfg),
            "name": cfg.agent.name,
            "version": os.getenv("ANILA_AGENT_VERSION") or _runtime_version(),
            "runtime_type": RuntimeType.ANILA_AGENT,
            "runtime_version": _runtime_version(),
            "api_version": "v1",
            "supported_task_types": ("chat",),
            "description_for_router": (
                "官方 ANILA 單 Agent runtime；由 CSP gateway 路由模型並產生完整 StepEvent。"
            ),
            "input_schema": {
                "type": "object",
                "properties": {"messages": {"type": "array"}},
                "required": ["messages"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"choices": {"type": "array"}},
            },
            "capabilities": capabilities,
            "event_protocols": ("step-event/v1",),
            "required_scopes": ("agent:invoke",),
            "classification": ManifestClassification(ceiling=ceiling, default=default),
            "full_trace_required": True,
            "supports_streaming": True,
            # This flag is supplied solely by the service startup boundary
            # after CSP token + durable-store admission succeeds.  A package
            # config/env value alone can never self-declare resume authority.
            "supports_resume": supports_resume,
            "supports_cancel": True,
            "supports_idempotency": True,
            "model_binding": ModelBinding(
                model_id=model_id,
                model_revision=os.getenv("ANILA_MODEL_REVISION"),
                gateway="csp",
                classification_ceiling=ceiling,
            ),
        }
    )
    return manifest


def canonical_manifest_json(manifest: AgentManifest) -> str:
    """Return deterministic UTF-8 JSON for registry hashing and snapshots."""

    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def admit_startup(
    cfg: AppConfig,
    *,
    csp_base_url: str | None = None,
    supports_resume: bool = False,
) -> AgentAdmission:
    """Perform official-agent startup checks and return the canonical admission."""

    csp_url = csp_base_url or os.getenv("CSP_BASE_URL") or os.getenv("ANILA_CSP_BASE_URL")
    if not csp_url:
        raise AgentAdmissionError("官方 Agent 啟動必須設定 CSP_BASE_URL")
    assert_csp_model_endpoint(cfg.model.base_url, csp_url)
    manifest = build_agent_manifest(cfg, supports_resume=supports_resume)
    return AgentAdmission(
        manifest=manifest,
        csp_base_url=csp_url.rstrip("/"),
        model_base_url=cfg.model.base_url.rstrip("/"),
    )


__all__ = [
    "AgentAdmission",
    "AgentAdmissionError",
    "admit_startup",
    "assert_csp_model_endpoint",
    "build_agent_manifest",
    "canonical_manifest_json",
]
