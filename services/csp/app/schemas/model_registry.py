from datetime import datetime
import ipaddress
from collections.abc import Mapping
from typing import Annotated, Any

from anila_contracts import Classification as ClassificationLevel
from anila_security import (
    ModelGovernanceError,
    ProviderBinding,
    ProviderLocality,
    TransportTarget,
    require_identifier,
)
from anila_security.model_governance import require_string
from pydantic import BaseModel, ConfigDict, Field, model_validator


LOCALITY_VALUES = tuple(item.value for item in ProviderLocality)

# ``context_window`` 從「只給 UI 顯示的註記」升級成 Router token 預算的輸入
# (見 anila_core.router.token_budget)。既然它現在會決定實際送出的 max_tokens,
# 就不能再接受 0 / 負值 —— 一個 0 會讓預算算出「輸入上限為負」,把每一次呼叫都
# 擋掉。在寫入邊界擋下來,比讓壞值流到推論路徑再去猜要清楚得多。``None``
# (未登記)仍然合法:Router 對未登記的降級行為是刻意設計的 fail-safe。
ContextWindowTokens = Annotated[int, Field(gt=0)]


def _canonicalize_provider_target(
    target: TransportTarget | None,
    *,
    locality: str | None,
) -> TransportTarget | None:
    """Use the explicit no-DNS policy for exact external IP targets.

    ``TransportTarget.parse`` applies the production DNS policy to external
    hostnames.  An IP literal has no DNS to resolve, so the provider contract
    canonicalizes it to ``dns_policy=none`` before hashing/validation.  Keep
    this normalization at the CSP schema boundary for both string and dict
    request forms; otherwise an equivalent dict from an older client is
    rejected while the string form succeeds.
    """

    if target is None or locality != ProviderLocality.EXTERNAL_GOVERNED.value:
        return target
    try:
        ipaddress.ip_address(target.host)
    except ValueError:
        return target
    if target.dns_policy == "none":
        return target
    return TransportTarget(
        target.scheme,
        target.host,
        target.port,
        target.port_mode,
        target.path,
        "none",
    )


def _target_from_input(
    value: Any,
    *,
    locality: str | None,
    field: str,
) -> TransportTarget | None:
    """Parse a request target through the shared canonical transport contract."""

    if value is None:
        return None
    if isinstance(value, Mapping):
        target = TransportTarget.from_dict(value, field=field)
    elif isinstance(value, str):
        dns_policy = (
            "production_fail_closed"
            if locality == ProviderLocality.EXTERNAL_GOVERNED.value
            else "none"
        )
        target = TransportTarget.parse(value, dns_policy=dns_policy, field=field)
    else:
        raise ModelGovernanceError(f"{field} must be a transport target string or object")
    return _canonicalize_provider_target(target, locality=locality)


def _validate_hash(target: TransportTarget | None, raw_hash: str | None, field: str) -> None:
    if raw_hash is None:
        return
    if target is None:
        raise ModelGovernanceError(f"{field} requires a transport target")
    if not isinstance(raw_hash, str) or len(raw_hash) != 64 or any(
        char not in "0123456789abcdef" for char in raw_hash
    ):
        raise ModelGovernanceError(f"{field} must be 64 lowercase hexadecimal characters")
    if raw_hash != target.sha256:
        raise ModelGovernanceError(f"{field} does not match its transport target")


def _validate_policy_id(value: str | None, field: str) -> None:
    if value is not None:
        require_identifier(value, field)


def validate_provider_snapshot_fields(
    *,
    provider_locality: str,
    transport_target: Any,
    transport_target_sha256: str | None,
    upstream_provider_locality: str | None,
    upstream_transport_target: Any,
    upstream_transport_target_sha256: str | None,
    egress_policy_id: str | None,
    upstream_egress_policy_id: str | None,
    allow_incomplete_unclassified: bool = True,
) -> tuple[TransportTarget | None, TransportTarget | None]:
    """Validate the CSP write shape using ``anila-security`` primitives.

    This helper intentionally does not infer locality from ``is_internal``.
    The API layer supplies the merged row state for PATCH requests, then uses
    the returned targets to build one complete snapshot atomically.
    """

    if provider_locality not in LOCALITY_VALUES:
        raise ModelGovernanceError("provider_locality is invalid")
    target = _target_from_input(
        transport_target, locality=provider_locality, field="transport_target"
    )
    upstream_locality = upstream_provider_locality
    if upstream_locality is not None and upstream_locality not in LOCALITY_VALUES:
        raise ModelGovernanceError("upstream_provider_locality is invalid")
    upstream_target = _target_from_input(
        upstream_transport_target,
        locality=upstream_locality,
        field="upstream_transport_target",
    )
    _validate_hash(target, transport_target_sha256, "transport_target_sha256")
    _validate_hash(
        upstream_target,
        upstream_transport_target_sha256,
        "upstream_transport_target_sha256",
    )
    _validate_policy_id(egress_policy_id, "egress_policy_id")
    _validate_policy_id(upstream_egress_policy_id, "upstream_egress_policy_id")

    # Delegate locality-specific admission rules to the shared security
    # contract.  CSP must not fork the external transport policy here: the
    # contract intentionally owns whether an external target is an HTTPS FQDN
    # or an exact host:port transport (including HTTP/gRPC/IP forms).
    if target is None:
        if provider_locality != ProviderLocality.UNCLASSIFIED.value:
            raise ModelGovernanceError(
                f"{provider_locality} requires transport_target"
            )
        if any(
            value is not None
            for value in (
                upstream_locality,
                upstream_target,
                upstream_transport_target_sha256,
                egress_policy_id,
                upstream_egress_policy_id,
            )
        ):
            raise ModelGovernanceError(
                "unclassified provider cannot carry upstream target or egress policy"
            )
    else:
        ProviderBinding.from_dict(
            {
                "provider_binding_id": "provider.schema",
                "model_registry_id": "model.schema",
                "model_registry_name": "model-schema",
                "model_registry_revision": "schema-validation",
                "provider_locality": provider_locality,
                "transport_target": target.to_dict(),
                "transport_target_sha256": target.sha256,
                "upstream_provider_locality": upstream_locality,
                "upstream_transport_target": (
                    None if upstream_target is None else upstream_target.to_dict()
                ),
                "upstream_transport_target_sha256": (
                    None if upstream_target is None else upstream_target.sha256
                ),
                "egress_policy_id": egress_policy_id,
                "upstream_egress_policy_id": upstream_egress_policy_id,
                "model_artifact_id": "artifact.schema",
                "deployment_id": "deployment.schema",
            }
        )
    if (
        provider_locality == ProviderLocality.UNCLASSIFIED.value
        and not allow_incomplete_unclassified
        and target is None
    ):
        raise ModelGovernanceError("classified provider snapshot is incomplete")

    return target, upstream_target


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    model_type: str  # 'llm' / 'vlm' / 'embedding' / 'agent' / 'image'
    endpoint_url: str
    api_version: str = "v1"
    description: str | None = None
    context_window: ContextWindowTokens | None = None
    base_model_id: int | None = None  # For agents: the underlying LLM model ID
    # Default True: new model registrations are expected to live on the
    # anila-models-net internal docker network (decoupled inference stack).
    # Admin can untick for external on-prem LAN endpoints. DB column default
    # (migration 0033) is False so historical rows aren't auto-flipped.
    is_internal: bool = True
    # Locality is authoritative; the legacy is_internal flag is never used to
    # infer it.  A request which omits this field remains unclassified.
    provider_locality: str = ProviderLocality.UNCLASSIFIED.value
    transport_target: dict[str, Any] | str | None = None
    transport_target_sha256: str | None = None
    model_registry_revision: str | None = None
    upstream_provider_locality: str | None = None
    upstream_transport_target: dict[str, Any] | str | None = None
    upstream_transport_target_sha256: str | None = None
    egress_policy_id: str | None = None
    upstream_egress_policy_id: str | None = None
    # Slice 6a (doc 04 §2): ModelEndpoint formalized fields.
    protocol: str = "openai_compatible"  # 'openai_compatible' / 'custom_adapter'
    classification_ceiling: ClassificationLevel = ClassificationLevel.UNCLASSIFIED
    owner_department_id: int | None = None
    supports_streaming: bool = True
    supports_json_schema: bool = False
    supports_tools: bool = False
    # doc 04 §3: write-only per-model gateway key. Encrypted to
    # ``api_key_secret_ref`` on create; NEVER returned. Omit to use the
    # global MODEL_GATEWAY_API_KEY fallback.
    api_key: str | None = None

    @model_validator(mode="after")
    def _validate_provider_snapshot(self) -> "ModelCreate":
        try:
            validate_provider_snapshot_fields(
                provider_locality=self.provider_locality,
                transport_target=self.transport_target,
                transport_target_sha256=self.transport_target_sha256,
                upstream_provider_locality=self.upstream_provider_locality,
                upstream_transport_target=self.upstream_transport_target,
                upstream_transport_target_sha256=self.upstream_transport_target_sha256,
                egress_policy_id=self.egress_policy_id,
                upstream_egress_policy_id=self.upstream_egress_policy_id,
            )
            if self.model_registry_revision is not None:
                require_string(
                    self.model_registry_revision,
                    "model_registry_revision",
                    max_length=256,
                )
        except ModelGovernanceError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    model_type: str | None = None
    endpoint_url: str | None = None
    api_version: str | None = None
    is_active: bool | None = None
    description: str | None = None
    context_window: ContextWindowTokens | None = None
    base_model_id: int | None = None
    is_internal: bool | None = None
    provider_locality: str | None = None
    transport_target: dict[str, Any] | str | None = None
    transport_target_sha256: str | None = None
    model_registry_revision: str | None = None
    upstream_provider_locality: str | None = None
    upstream_transport_target: dict[str, Any] | str | None = None
    upstream_transport_target_sha256: str | None = None
    egress_policy_id: str | None = None
    upstream_egress_policy_id: str | None = None
    # Slice 6a (doc 04 §2/§3).
    protocol: str | None = None
    # A default still preserves PATCH semantics because the API uses
    # ``exclude_unset=True``. Explicit JSON null is rejected by Pydantic.
    classification_ceiling: ClassificationLevel = ClassificationLevel.UNCLASSIFIED
    owner_department_id: int | None = None
    supports_streaming: bool | None = None
    supports_json_schema: bool | None = None
    supports_tools: bool | None = None
    # Write-only: re-encrypt the per-model gateway key. Never returned.
    api_key: str | None = None

    @model_validator(mode="after")
    def _validate_explicit_provider_fields(self) -> "ModelUpdate":
        # PATCH may omit locality and rely on the existing row. Validate all
        # supplied target/hash syntax now; the API repeats full merged-row
        # validation before mutating the ORM object.
        try:
            if (
                "provider_locality" in self.model_fields_set
                and self.provider_locality is None
            ):
                raise ModelGovernanceError(
                    "provider_locality must be one of the four locality strings; "
                    "omit it to retain the existing value"
                )
            if self.provider_locality is None:
                target = _target_from_input(
                    self.transport_target,
                    locality=None,
                    field="transport_target",
                )
                upstream = _target_from_input(
                    self.upstream_transport_target,
                    locality=self.upstream_provider_locality,
                    field="upstream_transport_target",
                )
                _validate_hash(
                    target,
                    self.transport_target_sha256,
                    "transport_target_sha256",
                )
                _validate_hash(
                    upstream,
                    self.upstream_transport_target_sha256,
                    "upstream_transport_target_sha256",
                )
                _validate_policy_id(self.egress_policy_id, "egress_policy_id")
                _validate_policy_id(
                    self.upstream_egress_policy_id,
                    "upstream_egress_policy_id",
                )
            else:
                validate_provider_snapshot_fields(
                    provider_locality=self.provider_locality,
                    transport_target=self.transport_target,
                    transport_target_sha256=self.transport_target_sha256,
                    upstream_provider_locality=self.upstream_provider_locality,
                    upstream_transport_target=self.upstream_transport_target,
                    upstream_transport_target_sha256=self.upstream_transport_target_sha256,
                    egress_policy_id=self.egress_policy_id,
                    upstream_egress_policy_id=self.upstream_egress_policy_id,
                )
            if self.model_registry_revision is not None:
                require_string(
                    self.model_registry_revision,
                    "model_registry_revision",
                    max_length=256,
                )
        except ModelGovernanceError as exc:
            raise ValueError(str(exc)) from exc
        return self


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    display_name: str
    model_type: str
    endpoint_url: str
    api_version: str
    is_active: bool
    is_router_primary: bool = False
    is_image_primary: bool = False
    health_status: str
    health_checked_at: datetime | None
    description: str | None
    context_window: int | None
    base_model_id: int | None = None
    base_model_name: str | None = None
    is_internal: bool = False
    provider_locality: str = ProviderLocality.UNCLASSIFIED.value
    transport_target: dict[str, Any] | None = None
    transport_target_sha256: str | None = None
    model_registry_revision: str | None = None
    upstream_provider_locality: str | None = None
    upstream_transport_target: dict[str, Any] | None = None
    upstream_transport_target_sha256: str | None = None
    egress_policy_id: str | None = None
    upstream_egress_policy_id: str | None = None
    # Slice 6a (doc 04 §2): ModelEndpoint formalized fields.
    protocol: str = "openai_compatible"
    classification_ceiling: ClassificationLevel
    owner_department_id: int | None = None
    supports_streaming: bool = True
    supports_json_schema: bool = False
    supports_tools: bool = False
    # doc 04 §3: only the presence of a per-model key is exposed — never the
    # ciphertext / secret ref, and never the plaintext.
    has_api_key: bool = False
    created_at: datetime
    updated_at: datetime
