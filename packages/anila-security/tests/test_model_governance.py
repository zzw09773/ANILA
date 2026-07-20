from __future__ import annotations

from datetime import datetime, timezone

import pytest

from anila_security.model_governance import (
    _AUTHORITY_CONSTRUCTOR_TOKEN,
    CLASSIFICATION_ORDER,
    CallsiteBinding,
    Deployment,
    InferenceCallsite,
    ModelArtifact,
    ModelGovernanceError,
    ProviderBinding,
    ProviderLocality,
    TransportTarget,
    VerifiedModelGovernanceAuthority,
    classification_rank,
    parse_rfc3339,
    parse_classification_level,
    transport_target_sha256,
)


def test_model_artifact_contract_requires_digest_revision_and_license() -> None:
    artifact = ModelArtifact.from_dict(
        {
            "artifact_id": "artifact.synthetic",
            "model_family": "synthetic-llm",
            "digest": "sha256:" + "a" * 64,
            "revision": "rev-1",
            "license_id": "Apache-2.0",
            "license_approved": True,
            "license_approval_artifact_id": "legal.synthetic.v1",
            "legal_approver_ids": ["legal"],
        }
    )
    assert artifact.revision == "rev-1"
    assert artifact.digest.startswith("sha256:")


def test_deployment_contract_requires_gpu_and_fresh_readiness() -> None:
    deployment = Deployment.from_dict(
        {
            "deployment_id": "deployment.synthetic",
            "artifact_id": "artifact.synthetic",
            "image_digest": "sha256:" + "b" * 64,
            "gpu_topology": {
                "vendor": "NVIDIA",
                "count": 1,
                "memory_gib": 80,
                "compute_capability": "sm_90",
            },
            "health_readiness": {
                "health_url": "/health",
                "readiness_url": "/ready",
                "freshness_seconds": 60,
                "last_check": "2026-07-15T12:00:00Z",
                "healthy": True,
                "ready": True,
            },
        }
    )
    assert deployment.gpu_count == 1
    assert deployment.last_health_check == datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_callsite_contract_rejects_raw_unknown_endpoint_field() -> None:
    value = {
        "id": "r7.synthetic",
        "owner": "synthetic",
        "service": "synthetic",
        "source": "services/synthetic.py",
        "symbol": "invoke",
        "kind": "synthetic",
        "enabled": False,
        "gateway_id": None,
        "raw_endpoint": True,
        "classification_ceiling": "營業秘密",
        "usage_sink": None,
        "audit_sink": None,
        "agent_scope": [],
        "sink_kinds": ["chat_completions"],
        "endpoint_url": "http://model.invalid",
    }
    with pytest.raises(ModelGovernanceError, match="unknown or missing fields"):
        InferenceCallsite.from_dict(value)


def test_scalar_contracts_fail_closed() -> None:
    with pytest.raises(ModelGovernanceError, match="sha256"):
        ModelArtifact.from_dict(
            {
                "artifact_id": "artifact.synthetic",
                "model_family": "synthetic",
                "digest": "not-a-digest",
                "revision": "rev-1",
                "license_id": "Apache-2.0",
                "license_approved": True,
                "license_approval_artifact_id": "legal.synthetic.v1",
                "legal_approver_ids": ["legal"],
            }
        )
    with pytest.raises(ModelGovernanceError, match="timezone"):
        parse_rfc3339("2026-07-15", "last_check")


def _provider_binding_value(
    *,
    locality: str = "internal_isolated",
    target: str = "model.internal:8000",
    dns_policy: str = "none",
    upstream_locality: str | None = None,
    upstream_target: str | None = None,
    upstream_dns_policy: str = "none",
    egress_policy_id: str | None = None,
    upstream_egress_policy_id: str | None = None,
) -> dict[str, object]:
    transport = TransportTarget.parse(target, dns_policy=dns_policy).to_dict()
    upstream_transport = (
        None
        if upstream_target is None
        else TransportTarget.parse(
            upstream_target,
            dns_policy=upstream_dns_policy,
        ).to_dict()
    )
    return {
        "provider_binding_id": "provider.synthetic",
        "model_registry_id": "model.synthetic",
        "model_registry_name": "synthetic-model",
        "model_registry_revision": "registry-rev-1",
        "provider_locality": locality,
        "transport_target": transport,
        "transport_target_sha256": transport_target_sha256(transport),
        "upstream_provider_locality": upstream_locality,
        "upstream_transport_target": upstream_transport,
        "upstream_transport_target_sha256": (
            None
            if upstream_transport is None
            else transport_target_sha256(upstream_transport)
        ),
        "egress_policy_id": egress_policy_id,
        "upstream_egress_policy_id": upstream_egress_policy_id,
        "model_artifact_id": "artifact.synthetic",
        "deployment_id": "deployment.synthetic",
    }


@pytest.mark.parametrize(
    ("raw", "canonical", "port_mode"),
    [
        ("MODEL.INTERNAL:8000", "model.internal:8000", "explicit_nondefault"),
        ("HTTPS://Api.Provider.Example", "https://api.provider.example", "default"),
        (
            "https://api.provider.example:8443/v1",
            "https://api.provider.example:8443/v1",
            "explicit_nondefault",
        ),
        ("tcp://provider.internal:9000", "tcp://provider.internal:9000", "explicit_nondefault"),
    ],
)
def test_transport_target_canonicalizes_host_port_fqdn_and_port_modes(
    raw: str,
    canonical: str,
    port_mode: str,
) -> None:
    target = TransportTarget.parse(raw)

    assert target.canonical == canonical
    assert target.port_mode == port_mode
    assert TransportTarget.from_dict(target.to_dict()) == target
    assert transport_target_sha256(target.to_dict()) == target.sha256


def test_transport_target_serialized_form_must_already_be_canonical() -> None:
    value = TransportTarget.parse("https://api.provider.example").to_dict()
    value["host"] = "API.Provider.Example"

    with pytest.raises(ModelGovernanceError, match="not canonical"):
        TransportTarget.from_dict(value)


def test_provider_binding_covers_all_locality_states_and_shim_upstream() -> None:
    isolated = ProviderBinding.from_dict(_provider_binding_value())
    unclassified = ProviderBinding.from_dict(
        _provider_binding_value(locality=ProviderLocality.UNCLASSIFIED.value)
    )
    external = ProviderBinding.from_dict(
        _provider_binding_value(
            locality=ProviderLocality.EXTERNAL_GOVERNED.value,
            target="https://api.provider.example/v1",
            dns_policy="production_fail_closed",
            egress_policy_id="egress.provider",
        )
    )
    shim = ProviderBinding.from_dict(
        _provider_binding_value(
            locality=ProviderLocality.INTERNAL_SHIM.value,
            target="provider-shim.internal:8000",
            upstream_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
            upstream_target="https://api.provider.example/v1",
            upstream_dns_policy="production_fail_closed",
            upstream_egress_policy_id="egress.provider",
        )
    )

    assert isolated.provider_locality == ProviderLocality.INTERNAL_ISOLATED.value
    assert unclassified.provider_locality == ProviderLocality.UNCLASSIFIED.value
    assert external.egress_policy_id == "egress.provider"
    assert shim.upstream_provider_locality == ProviderLocality.EXTERNAL_GOVERNED.value
    assert shim.upstream_transport_target is not None


def test_provider_binding_rejects_external_transport_and_egress_gaps() -> None:
    http_external = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="http://api.provider.example:7000",
        dns_policy="production_fail_closed",
        egress_policy_id="egress.provider",
    )
    assert ProviderBinding.from_dict(http_external).transport_target.scheme == "http"

    ip_external = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="http://172.16.120.35:7000",
        dns_policy="none",
        egress_policy_id="egress.provider",
    )
    assert ProviderBinding.from_dict(ip_external).transport_target.host == "172.16.120.35"

    no_egress_external = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="http://172.16.120.35:7000",
        dns_policy="none",
    )
    with pytest.raises(ModelGovernanceError, match="egress_policy_id"):
        ProviderBinding.from_dict(no_egress_external)

    no_egress_fqdn = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://api.provider.example",
        dns_policy="production_fail_closed",
    )
    with pytest.raises(ModelGovernanceError, match="egress_policy_id"):
        ProviderBinding.from_dict(no_egress_fqdn)


def test_external_provider_context_canonicalizes_bare_fqdn_to_https_default() -> None:
    target = TransportTarget.parse(
        "Api.Provider.Example",
        dns_policy="production_fail_closed",
    )

    assert target.scheme == "https"
    assert target.host == "api.provider.example"
    assert target.port == 443
    assert target.port_mode == "default"
    assert target.canonical == "https://api.provider.example"

    explicit = TransportTarget.parse(
        "https://api.provider.example:443",
        dns_policy="production_fail_closed",
    )
    assert explicit.to_dict() == target.to_dict()
    with pytest.raises(ModelGovernanceError, match="numeric port"):
        TransportTarget.parse("api.provider.example")


def test_provider_binding_accepts_bare_fqdn_only_in_external_context() -> None:
    value = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://api.provider.example",
        dns_policy="production_fail_closed",
        egress_policy_id="egress.provider",
    )
    target = TransportTarget.parse(
        "api.provider.example",
        dns_policy="production_fail_closed",
    )
    value["transport_target"] = "api.provider.example"
    value["transport_target_sha256"] = target.sha256
    parsed = ProviderBinding.from_dict(value)
    assert parsed.transport_target.to_dict() == target.to_dict()


def test_internal_shim_external_upstream_accepts_exact_grpc_ip_and_own_egress() -> None:
    value = _provider_binding_value(
        locality=ProviderLocality.INTERNAL_SHIM.value,
        target="provider-shim.internal:8000",
        upstream_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        upstream_target="172.16.120.35:9001",
        upstream_dns_policy="none",
        upstream_egress_policy_id="egress.embedding",
    )
    upstream = TransportTarget.parse("172.16.120.35:9001", dns_policy="none")
    value["upstream_transport_target"] = "172.16.120.35:9001"
    value["upstream_transport_target_sha256"] = upstream.sha256
    parsed = ProviderBinding.from_dict(value)
    assert parsed.upstream_transport_target is not None
    assert parsed.upstream_transport_target.canonical == "172.16.120.35:9001"
    assert parsed.upstream_egress_policy_id == "egress.embedding"


def test_transport_target_default_ports_and_grpc_requirements() -> None:
    for raw in ("http://api.provider.example", "http://api.provider.example:80"):
        target = TransportTarget.parse(raw)
        assert target.canonical == "http://api.provider.example"
        assert target.port_mode == "default"
    assert TransportTarget.parse("https://api.provider.example").to_dict() == TransportTarget.parse(
        "https://api.provider.example:443"
    ).to_dict()
    assert TransportTarget.parse("grpcs://api.provider.example").port == 443
    assert TransportTarget.parse("grpc://api.provider.example:9001").port_mode == "explicit_nondefault"
    with pytest.raises(ModelGovernanceError, match="numeric port"):
        TransportTarget.parse("grpc://api.provider.example")
    for raw in ("tcp://api.provider.example", "tls://api.provider.example"):
        with pytest.raises(ModelGovernanceError, match="numeric port"):
            TransportTarget.parse(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "[2001:db8::35]:9001",
        "http://[2001:db8::35]:7000",
    ],
)
def test_transport_target_supports_ipv6_literal_host_port(raw: str) -> None:
    target = TransportTarget.parse(raw)
    assert target.host == "2001:db8::35"
    assert target.port in {7000, 9001}
    assert target.canonical.startswith("[") or target.canonical.startswith("http://[")


def test_external_dns_policy_must_match_host_kind() -> None:
    fqdn_wrong_policy = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://api.provider.example",
        dns_policy="none",
        egress_policy_id="egress.provider",
    )
    with pytest.raises(ModelGovernanceError, match="hostname requires"):
        ProviderBinding.from_dict(fqdn_wrong_policy)

    ip_wrong_policy = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://172.16.120.35:7000",
        dns_policy="production_fail_closed",
        egress_policy_id="egress.provider",
    )
    with pytest.raises(ModelGovernanceError, match="IP literal requires"):
        ProviderBinding.from_dict(ip_wrong_policy)


@pytest.mark.parametrize(
    "host",
    (
        "127.1",
        "127.000.000.001",
        "2130706433",
        "0x7f000001",
        "0177.0.0.1",
    ),
)
def test_external_provider_rejects_ambiguous_numeric_ipv4_host_syntax(host: str) -> None:
    value = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://api.provider.example",
        dns_policy="production_fail_closed",
        egress_policy_id="egress.provider",
    )
    value["transport_target"] = f"http://{host}:7000"
    value["transport_target_sha256"] = "0" * 64

    with pytest.raises(ModelGovernanceError, match="ambiguous numeric IPv4"):
        ProviderBinding.from_dict(value)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    (
        ("http://127.0.0.1:7000", "http://127.0.0.1:7000"),
        (
            "https://model-127.vendor.example:7000",
            "https://model-127.vendor.example:7000",
        ),
        (
            "https://127.provider.example:7000",
            "https://127.provider.example:7000",
        ),
    ),
)
def test_transport_target_keeps_canonical_ips_and_numeric_label_fqdns(
    raw: str, canonical: str
) -> None:
    target = TransportTarget.parse(raw, dns_policy="none")

    assert target.canonical == canonical


def test_external_provider_accepts_numeric_label_fqdn() -> None:
    value = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://model-127.vendor.example:7000",
        dns_policy="production_fail_closed",
        egress_policy_id="egress.provider",
    )

    assert (
        ProviderBinding.from_dict(value).transport_target.canonical
        == "https://model-127.vendor.example:7000"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:pass@api.provider.example",
        "https://api.provider.example/path?query=1",
        "https://api.provider.example/path#fragment",
        "https://api.provider.example//path",
        "https://api.provider.example:",
        "api.provider.example:7000/path",
        "api.provider.example:abc",
    ],
)
def test_transport_target_rejects_ambiguous_url_forms(raw: str) -> None:
    with pytest.raises(ModelGovernanceError):
        TransportTarget.parse(raw)


def test_provider_binding_rejects_unpaired_or_misclassified_shim_egress() -> None:
    invalid_upstream_egress = _provider_binding_value(
        locality=ProviderLocality.INTERNAL_SHIM.value,
        target="provider-shim.internal:8000",
        upstream_locality=ProviderLocality.INTERNAL_ISOLATED.value,
        upstream_target="model.internal:8000",
        upstream_egress_policy_id="egress.not-allowed",
    )
    with pytest.raises(ModelGovernanceError, match="non-external upstream"):
        ProviderBinding.from_dict(invalid_upstream_egress)

    external_with_upstream_egress = _provider_binding_value(
        locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        target="https://api.provider.example",
        dns_policy="production_fail_closed",
        egress_policy_id="egress.provider",
        upstream_egress_policy_id="egress.unpaired",
    )
    with pytest.raises(ModelGovernanceError, match="cannot carry shim upstream"):
        ProviderBinding.from_dict(external_with_upstream_egress)


def test_classification_order_matches_canonical_contract_when_available() -> None:
    contracts = pytest.importorskip("anila_contracts.classification")
    canonical = tuple(level.value for level in contracts.ClassificationLevel)
    assert canonical == CLASSIFICATION_ORDER
    for rank, level in enumerate(contracts.ClassificationLevel):
        assert parse_classification_level(level) == level.value
        assert classification_rank(level) == rank


def _scoped_authority(scope: tuple[str, ...]) -> VerifiedModelGovernanceAuthority:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    callsite = InferenceCallsite(
        callsite_id="r7.synthetic.scoped",
        owner="tests",
        service="synthetic",
        source="tests",
        symbol="invoke",
        kind="synthetic",
        enabled=False,
        gateway_id="csp-model-gateway",
        raw_endpoint=False,
        classification_ceiling="極機密",
        usage_sink="csp.token_usage",
        audit_sink="csp.audit_log",
        agent_scope=scope,
        sink_kinds=("chat_completions",),
    )
    binding = CallsiteBinding(
        callsite_id=callsite.callsite_id,
        gateway_id="csp-model-gateway",
        classification_ceiling="極機密",
        usage_sink="csp.token_usage",
        audit_sink="csp.audit_log",
        agent_scope=scope,
        model_artifact_id="artifact.synthetic",
        deployment_id="deployment.synthetic",
    )
    artifact = ModelArtifact(
        artifact_id="artifact.synthetic",
        model_family="synthetic",
        digest="sha256:" + "a" * 64,
        revision="rev-1",
        license_id="Apache-2.0",
        license_approved=True,
        license_approval_artifact_id="legal.synthetic.v1",
        legal_approver_ids=("legal",),
    )
    deployment = Deployment(
        deployment_id="deployment.synthetic",
        artifact_id=artifact.artifact_id,
        image_digest="sha256:" + "b" * 64,
        gpu_vendor="NVIDIA",
        gpu_count=1,
        gpu_memory_gib=80,
        gpu_compute_capability="sm_90",
        health_url="/health",
        readiness_url="/ready",
        readiness_freshness_seconds=60,
        last_health_check=now,
        healthy=True,
        ready=True,
    )
    return VerifiedModelGovernanceAuthority(
        _token=_AUTHORITY_CONSTRUCTOR_TOKEN,
        profile_id="profile.synthetic",
        profile_version="v1",
        profile_content_sha256="a" * 64,
        inventory_sha256="b" * 64,
        enabled=True,
        enabled_callsites=(callsite.callsite_id,),
        disabled_callsites=(),
        callsites={callsite.callsite_id: callsite},
        bindings={binding.callsite_id: binding},
        model_artifacts={artifact.artifact_id: artifact},
        deployments={deployment.deployment_id: deployment},
        valid_from=now,
        valid_until=now.replace(hour=13),
    )


def test_registered_agent_scope_is_category_not_literal_id() -> None:
    authority = _scoped_authority(("registered-agent",))
    admitted = authority.authorize(
        "r7.synthetic.scoped",
        "無機密",
        "artifact.synthetic",
        "deployment.synthetic",
        agent_id="research-agent",
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert admitted.agent_scope == ("registered-agent",)

    with pytest.raises(ModelGovernanceError, match="selector"):
        authority.authorize(
            "r7.synthetic.scoped",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            agent_id="registered-agent",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )


def test_exact_agent_scope_remains_exact_and_missing_context_fails_closed() -> None:
    authority = _scoped_authority(("research-agent",))
    with pytest.raises(ModelGovernanceError, match="outside"):
        authority.authorize(
            "r7.synthetic.scoped",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            agent_id=None,
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
    with pytest.raises(ModelGovernanceError, match="outside"):
        authority.authorize(
            "r7.synthetic.scoped",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            agent_id="other-agent",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
    admitted = authority.authorize(
        "r7.synthetic.scoped",
        "無機密",
        "artifact.synthetic",
        "deployment.synthetic",
        agent_id="research-agent",
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert admitted.agent_scope == ("research-agent",)
