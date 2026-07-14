"""Deterministic registry snapshot and capability filtering.

The Router consumes a CSP-produced registry *view*.  It never treats an
Agent's self-reported readiness as authority.  Every candidate must therefore
be present in a fresh CSP snapshot and satisfy the server-derived request
context before the routing model is allowed to choose one.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from anila_contracts import AgentManifest
from anila_contracts.agents import ManifestCapabilities, ModelBinding
from anila_contracts.classification import ClassificationLevel

from .request_context import RequestContext


def _tokens(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _capabilities_from_manifest(manifest: AgentManifest) -> tuple[str, ...]:
    capabilities = manifest.capabilities
    if isinstance(capabilities, tuple):
        return _tokens(capabilities)
    if isinstance(capabilities, ManifestCapabilities):
        values: list[str] = []
        if capabilities.retrieval:
            values.append("retrieval")
        if capabilities.tools:
            values.extend(capabilities.tools)
            values.append("tools")
        if capabilities.streaming:
            values.append("streaming")
        return _tokens(values)
    return ()


@dataclass(frozen=True)
class RegistryEntry:
    """Immutable CSP registry entry consumed by the Router.

    ``manifest`` may be a validated :class:`AgentManifest` or its JSON object
    projection.  The readiness fields are CSP-owned view data; an Agent cannot
    make itself dispatchable by changing its manifest.
    """

    agent_id: str
    manifest: AgentManifest | dict[str, Any] | None = None
    snapshot_id: str | None = None
    manifest_revision: str | None = None
    approved: bool = False
    health_ready: bool = False
    trace_test_passed: bool = False
    ready_for_dispatch: bool = False
    manifest_valid: bool = False
    endpoint_via_csp: bool = True
    approval_required: bool = False
    required_obligations: tuple[str, ...] = ()
    # Direct fields keep this a useful local consumer protocol while CSP
    # evolves its internal view shape.
    task_types: tuple[str, ...] = ()
    supported_task_types: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    # Governance fields are CSP-supplied only.  AgentManifest fields never
    # fill these defaults.
    classification_ceiling: ClassificationLevel | None = None
    model_binding: ModelBinding | dict[str, Any] | None = None
    model_gateway: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.manifest, dict):
            try:
                object.__setattr__(self, "manifest", AgentManifest.model_validate(self.manifest))
            except Exception:
                # Keep the entry in the immutable snapshot so the filter can
                # return MANIFEST_INVALID instead of throwing at the trust
                # boundary.
                object.__setattr__(self, "manifest_valid", False)

        manifest = self.manifest if isinstance(self.manifest, AgentManifest) else None
        if manifest is not None:
            if manifest.agent_id != self.agent_id:
                object.__setattr__(self, "manifest_valid", False)
            if not self.task_types and not self.supported_task_types:
                object.__setattr__(self, "task_types", tuple(manifest.supported_task_types))
            if not self.supported_task_types:
                object.__setattr__(
                    self, "supported_task_types", tuple(manifest.supported_task_types)
                )
            if not self.capabilities:
                object.__setattr__(self, "capabilities", _capabilities_from_manifest(manifest))
            if not self.required_scopes:
                # Keep the empty CSP declaration empty.  Required scopes are a
                # governance field and must not be inherited from the Agent.
                object.__setattr__(self, "required_scopes", ())

        object.__setattr__(
            self, "task_types", _tokens(self.task_types or self.supported_task_types)
        )
        object.__setattr__(
            self, "supported_task_types", _tokens(self.supported_task_types or self.task_types)
        )
        object.__setattr__(self, "capabilities", _tokens(self.capabilities))
        object.__setattr__(self, "required_scopes", _tokens(self.required_scopes))
        object.__setattr__(self, "required_obligations", _tokens(self.required_obligations))

        binding = self.model_binding
        if isinstance(binding, dict):
            try:
                binding = ModelBinding.model_validate(binding)
            except Exception:
                binding = None
            object.__setattr__(self, "model_binding", binding)

        ceiling = self.classification_ceiling
        if isinstance(ceiling, str):
            try:
                object.__setattr__(
                    self, "classification_ceiling", ClassificationLevel.from_storage(ceiling)
                )
            except ValueError:
                object.__setattr__(self, "classification_ceiling", None)
        elif ceiling is not None and not isinstance(ceiling, ClassificationLevel):
            object.__setattr__(self, "classification_ceiling", None)

        if self.manifest_revision is not None and not isinstance(self.manifest_revision, str):
            object.__setattr__(self, "manifest_revision", None)
        if isinstance(self.manifest_revision, str) and not self.manifest_revision.strip():
            object.__setattr__(self, "manifest_revision", None)
        for field_name in (
            "approved",
            "health_ready",
            "trace_test_passed",
            "ready_for_dispatch",
            "manifest_valid",
            "endpoint_via_csp",
            "approval_required",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                object.__setattr__(self, field_name, False)
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id 不得為空白")
        if self.snapshot_id is not None and (
            not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip()
        ):
            raise ValueError("snapshot_id 不得為空白")

    @property
    def effective_task_types(self) -> tuple[str, ...]:
        return self.task_types or self.supported_task_types

    @property
    def effective_classification_ceiling(self) -> ClassificationLevel:
        if self.classification_ceiling is None:
            # The filter treats this as missing authority; this fallback only
            # keeps the property total for logging/type consumers.
            return ClassificationLevel.UNCLASSIFIED
        return self.classification_ceiling

    @property
    def has_csp_model_binding(self) -> bool:
        binding = self.model_binding
        if isinstance(binding, ModelBinding):
            return bool(binding.gateway == "csp")
        return False


@dataclass(frozen=True)
class RegistrySnapshot:
    """An immutable CSP registry generation consumed for one router decision."""

    snapshot_id: str
    entries: tuple[RegistryEntry, ...] | Sequence[RegistryEntry] = ()
    fresh: bool = True
    stale: bool = False
    authority: str = "csp"
    captured_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_id, str) or not self.snapshot_id.strip():
            raise ValueError("registry snapshot_id 不得為空白")
        normalised_entries: list[RegistryEntry] = []
        for raw_entry in self.entries:
            if isinstance(raw_entry, RegistryEntry):
                normalised_entries.append(raw_entry)
            elif isinstance(raw_entry, AgentManifest):
                normalised_entries.append(
                    RegistryEntry(
                        agent_id=raw_entry.agent_id,
                        manifest=raw_entry,
                        snapshot_id=self.snapshot_id,
                    )
                )
            elif isinstance(raw_entry, dict):
                normalised_entries.append(RegistryEntry(**raw_entry))
            else:
                raise TypeError("registry entries 必須是 RegistryEntry/AgentManifest/object")
        materialized = tuple(normalised_entries)
        if len({entry.agent_id for entry in materialized}) != len(materialized):
            raise ValueError("registry snapshot 不得有重複 agent_id")
        object.__setattr__(self, "entries", materialized)
        if self.captured_at is not None and not isinstance(self.captured_at, datetime):
            raise TypeError("captured_at 必須是 datetime")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise TypeError("expires_at 必須是 datetime")
        if self.captured_at is not None and self.captured_at.tzinfo is None:
            raise ValueError("captured_at 必須帶時區")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at 必須帶時區")

    def is_fresh(self, at: datetime | None = None, *, now: datetime | None = None) -> bool:
        if (
            not isinstance(self.fresh, bool)
            or not isinstance(self.stale, bool)
            or not self.fresh
            or self.stale
            or self.authority != "csp"
        ):
            return False
        if self.captured_at is None or self.expires_at is None:
            return False
        instant = now or at or datetime.now(timezone.utc)
        if not isinstance(instant, datetime):
            raise TypeError("freshness check 的時間必須是 datetime")
        if instant.tzinfo is None:
            raise ValueError("freshness check 的時間必須帶時區")
        if self.expires_at <= self.captured_at:
            return False
        return self.captured_at <= instant < self.expires_at

    @property
    def entry_by_id(self) -> dict[str, RegistryEntry]:
        return {entry.agent_id: entry for entry in self.entries}

    @property
    def generation(self) -> str:
        return self.snapshot_id


@dataclass(frozen=True)
class CandidateFilterResult:
    """Deterministic candidate set and safe audit reason codes."""

    snapshot_id: str | None
    candidates: tuple[RegistryEntry, ...]
    reason_codes: tuple[str, ...]

    @property
    def entries(self) -> tuple[RegistryEntry, ...]:
        return self.candidates

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.reason_codes

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self.candidates)

    def __len__(self) -> int:
        return len(self.candidates)

    def __bool__(self) -> bool:
        return bool(self.candidates)


class CapabilityFilter:
    """Apply deterministic task, capability, scope and readiness checks."""

    def filter(
        self,
        context: RequestContext,
        snapshot: RegistrySnapshot | None,
        *,
        now: datetime | None = None,
    ) -> CandidateFilterResult:
        if snapshot is None:
            return CandidateFilterResult(None, (), ("SNAPSHOT_MISSING",))
        if not snapshot.is_fresh(now=now):
            return CandidateFilterResult(snapshot.snapshot_id, (), ("SNAPSHOT_STALE",))

        accepted: list[RegistryEntry] = []
        reasons: list[str] = []
        required_caps = set(context.required_capabilities)
        user_scopes = set(context.scopes)

        for entry in snapshot.entries:
            if entry.snapshot_id != snapshot.snapshot_id:
                reasons.append("ENTRY_SNAPSHOT_MISMATCH")
                continue
            if entry.manifest is None:
                reasons.append("MANIFEST_MISSING")
                continue
            if not entry.manifest_valid:
                reasons.append("MANIFEST_INVALID")
                continue
            if entry.manifest_revision is None or not entry.manifest_revision.strip():
                reasons.append("MANIFEST_REVISION_MISSING")
                continue
            if not entry.ready_for_dispatch:
                if not entry.approved:
                    reasons.append("AGENT_NOT_APPROVED")
                if not entry.health_ready:
                    reasons.append("AGENT_UNHEALTHY")
                if not entry.trace_test_passed:
                    reasons.append("TRACE_TEST_NOT_READY")
                reasons.append("AGENT_NOT_READY")
                continue
            if not entry.approved or not entry.health_ready or not entry.trace_test_passed:
                reasons.append("AGENT_NOT_READY")
                continue
            if not entry.has_csp_model_binding:
                reasons.append("MODEL_BINDING_MISSING")
                continue
            if entry.classification_ceiling is None:
                reasons.append("CLASSIFICATION_CEILING_MISSING")
                continue
            task_types = set(entry.effective_task_types)
            if context.task_type not in task_types:
                reasons.append("TASK_TYPE_UNSUPPORTED")
                continue
            if not required_caps.issubset(set(entry.capabilities)):
                reasons.append("CAPABILITY_MISMATCH")
                continue
            if not set(entry.required_scopes).issubset(user_scopes):
                reasons.append("INSUFFICIENT_SCOPE")
                continue
            if context.classification > entry.effective_classification_ceiling:
                reasons.append("CLASSIFICATION_EXCEEDS_CEILING")
                continue
            if not entry.endpoint_via_csp:
                reasons.append("UNTRUSTED_ENDPOINT")
                continue
            accepted.append(entry)

        if not accepted:
            reasons.append("NO_ELIGIBLE_CANDIDATES")
        elif not reasons:
            reasons.append("CANDIDATES_READY")
        unique_reasons = tuple(dict.fromkeys(reasons))
        return CandidateFilterResult(snapshot.snapshot_id, tuple(accepted), unique_reasons)

    select = filter


# Names used in design docs and adapters; aliases keep one implementation and
# avoid introducing a second registry authority.
RouterRegistryEntry = RegistryEntry
RouterRegistrySnapshot = RegistrySnapshot
AgentRegistryEntry = RegistryEntry
AgentRegistrySnapshot = RegistrySnapshot


__all__ = [
    "AgentRegistryEntry",
    "AgentRegistrySnapshot",
    "CapabilityFilter",
    "CandidateFilterResult",
    "RegistryEntry",
    "RegistrySnapshot",
    "RouterRegistryEntry",
    "RouterRegistrySnapshot",
]
