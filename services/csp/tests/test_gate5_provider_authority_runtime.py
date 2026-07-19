from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from anila_security import ProviderBinding, TransportTarget
from app.api import models as models_api
from app.models.model_registry import ModelRegistry
from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.services import auto_seed, health_checker
from app.services.model_governance_runtime import (
    ModelGovernanceRuntime,
    ModelGovernanceRuntimeError,
)
from tests.conftest import make_model, make_user


def _provider(
    *,
    binding_id: str = "provider.registry.42",
    revision: str = "registry-rev-1",
    endpoint: str = "model.internal:8000",
) -> ProviderBinding:
    target = TransportTarget.parse(endpoint, dns_policy="none")
    return ProviderBinding.from_dict(
        {
            "provider_binding_id": binding_id,
            "model_registry_id": "42",
            "model_registry_name": "synthetic-model",
            "model_registry_revision": revision,
            "provider_locality": "internal_isolated",
            "transport_target": target.to_dict(),
            "transport_target_sha256": target.sha256,
            "upstream_provider_locality": None,
            "upstream_transport_target": None,
            "upstream_transport_target_sha256": None,
            "egress_policy_id": None,
            "upstream_egress_policy_id": None,
            "model_artifact_id": "artifact.synthetic",
            "deployment_id": "deployment.synthetic",
        }
    )


def _model(*, provider: ProviderBinding | None = None, **changes):
    provider = provider or _provider()
    values = {
        "id": 42,
        "name": "synthetic-model",
        "model_registry_revision": "registry-rev-1",
        "provider_locality": "internal_isolated",
        "endpoint_url": provider.transport_target.canonical,
        "transport_target": provider.transport_target.to_dict(),
        "transport_target_sha256": provider.transport_target_sha256,
        "upstream_provider_locality": None,
        "upstream_transport_target": None,
        "upstream_transport_target_sha256": None,
        "egress_policy_id": None,
        "upstream_egress_policy_id": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _runtime(monkeypatch: pytest.MonkeyPatch, *providers: ProviderBinding):
    runtime = ModelGovernanceRuntime(
        enabled=True,
        inventory_path=None,
        profile_path=None,
        trust_store_path=None,
        observed_facts_path=None,
        gateway_endpoint="https://csp-model-gateway/v1",
    )
    authority = SimpleNamespace(
        provider_bindings={item.provider_binding_id: item for item in providers}
    )
    monkeypatch.setattr(runtime, "_require_ready", lambda *, now: authority)
    return runtime


def test_exact_registry_snapshot_resolves_one_server_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    runtime = _runtime(monkeypatch, provider)

    assert runtime.resolve_provider_binding(_model()).provider_binding_id == (
        provider.provider_binding_id
    )


@pytest.mark.parametrize(
    "authority_endpoint,registry_endpoint",
    [
        ("MODEL.Internal.:8000", "model.internal:8000"),
        ("172.16.120.35:9001", "172.16.120.35:9001"),
    ],
    ids=["fqdn-canonical", "exact-host-port"],
)
def test_canonical_fqdn_and_exact_host_port_match(
    monkeypatch: pytest.MonkeyPatch,
    authority_endpoint: str,
    registry_endpoint: str,
) -> None:
    provider = _provider(endpoint=authority_endpoint)
    runtime = _runtime(monkeypatch, provider)

    assert runtime.resolve_provider_binding(
        _model(provider=provider, endpoint_url=registry_endpoint)
    ).provider_binding_id == provider.provider_binding_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "other-model"),
        ("model_registry_revision", "registry-rev-2"),
        ("provider_locality", "external_governed"),
        ("endpoint_url", "model.internal:9000"),
        ("transport_target_sha256", "0" * 64),
        ("egress_policy_id", "egress.untrusted"),
    ],
)
def test_registry_identity_or_snapshot_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    runtime = _runtime(monkeypatch, _provider())

    with pytest.raises(ModelGovernanceRuntimeError):
        runtime.resolve_provider_binding(_model(**{field: value}))


def test_same_binding_id_profile_replacement_target_revision_fails_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A profile rotation may keep the provider_binding_id while changing its
    # target/revision.  The locked registry snapshot must reject that pair;
    # callers therefore never reach their network implementation.
    original = _provider()
    replacement = _provider(
        revision="registry-rev-2", endpoint="model.replacement:9000"
    )
    runtime = _runtime(monkeypatch, replacement)

    with pytest.raises(ModelGovernanceRuntimeError, match="snapshot differs"):
        runtime.resolve_provider_binding(_model(provider=original))


def test_multiple_provider_bindings_for_one_registry_row_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(
        monkeypatch,
        _provider(binding_id="provider.registry.42.a"),
        _provider(binding_id="provider.registry.42.b"),
    )

    with pytest.raises(ModelGovernanceRuntimeError, match="multiple"):
        runtime.resolve_provider_binding(_model())


def test_unclassified_and_missing_profile_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(monkeypatch, _provider())
    with pytest.raises(ModelGovernanceRuntimeError, match="unclassified"):
        runtime.resolve_provider_binding(_model(provider_locality="unclassified"))

    missing = ModelGovernanceRuntime(
        enabled=True,
        inventory_path="/definitely/missing/inventory.json",
        profile_path="/definitely/missing/profile.json",
        trust_store_path="/definitely/missing/trust.json",
        observed_facts_path="/definitely/missing/observed.json",
        gateway_endpoint="https://csp-model-gateway/v1",
    )
    with pytest.raises(ModelGovernanceRuntimeError, match="not ready"):
        missing.resolve_provider_binding(_model())


def _deny_authority(_model: ModelRegistry) -> None:
    raise HTTPException(status_code=503, detail="synthetic authority denial")


def test_formal_active_create_rolls_back_before_commit(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_user(db, username="provider-create-admin", role="admin")
    monkeypatch.setattr(models_api, "_enforce_endpoint_url", lambda _url: None)
    monkeypatch.setattr(models_api, "_require_provider_authority", _deny_authority)
    request = ModelCreate(
        name="provider-create-denied",
        display_name="Provider create denied",
        model_type="llm",
        endpoint_url="http://model.internal:8000",
    )

    with pytest.raises(HTTPException, match="synthetic authority denial"):
        models_api.create_model(request=request, admin=admin, db=db)

    assert db.query(ModelRegistry).filter_by(name=request.name).first() is None


def test_model_mutation_row_lock_helper_is_for_update(monkeypatch):
    calls = []

    class _Query:
        def filter(self, *_args):
            calls.append("filter")
            return self

        def populate_existing(self):
            calls.append("populate_existing")
            return self

        def with_for_update(self):
            calls.append("with_for_update")
            return self

        def one_or_none(self):
            calls.append("one_or_none")
            return object()

    class _Db:
        def query(self, _model):
            return _Query()

    assert models_api._lock_model_row(_Db(), 42) is not None
    assert calls == ["filter", "populate_existing", "with_for_update", "one_or_none"]


def test_activation_and_active_update_leave_database_unchanged_on_denial(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_user(db, username="provider-mutation-admin", role="admin")
    model = make_model(db, name="provider-mutation-denied")
    model.is_active = False
    db.commit()
    monkeypatch.setattr(models_api, "_enforce_endpoint_url", lambda _url: None)
    monkeypatch.setattr(models_api, "_require_provider_authority", _deny_authority)

    with pytest.raises(HTTPException):
        models_api.activate_model(model.id, None, admin, db)  # type: ignore[arg-type]
    db.rollback()
    db.refresh(model)
    assert model.is_active is False

    with pytest.raises(HTTPException):
        models_api.update_model(model.id, ModelUpdate(is_active=True), admin, db)
    db.rollback()
    db.refresh(model)
    assert model.is_active is False


def test_primary_denial_does_not_clear_existing_primary(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_user(db, username="provider-primary-admin", role="admin")
    current = make_model(db, name="provider-primary-current")
    candidate = make_model(db, name="provider-primary-candidate")
    current.is_router_primary = True
    db.commit()
    monkeypatch.setattr(models_api, "_require_provider_authority", _deny_authority)

    with pytest.raises(HTTPException):
        models_api.set_router_primary(candidate.id, admin, db)
    db.rollback()
    db.refresh(current)
    db.refresh(candidate)
    assert current.is_router_primary is True
    assert candidate.is_router_primary is False


def test_manual_probe_denial_is_zero_network(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = make_user(db, username="provider-probe-admin", role="admin")
    model = make_model(db, name="provider-probe-denied")
    network_calls = []

    async def forbidden_probe(*_args, **_kwargs):
        network_calls.append(True)
        raise AssertionError("probe must not reach network")

    monkeypatch.setattr(models_api, "_require_provider_authority", _deny_authority)
    monkeypatch.setattr(models_api, "probe_model_health_detailed", forbidden_probe)

    with pytest.raises(HTTPException):
        asyncio.run(models_api._probe_and_persist(model, admin, db, None))
    assert network_calls == []


@pytest.mark.asyncio
async def test_background_health_denial_is_zero_network(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = make_model(db, name="provider-background-denied")
    network_calls = []

    class _SessionProxy:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            return None

    async def forbidden_health(*_args, **_kwargs):
        network_calls.append(True)
        raise AssertionError("health network must not run")

    sleep_calls = 0

    async def stop_after_one_cycle(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        raise asyncio.CancelledError

    monkeypatch.setattr(health_checker, "SessionLocal", _SessionProxy)
    monkeypatch.setattr(
        health_checker,
        "admit_registry_provider",
        lambda _model: (_ for _ in ()).throw(RuntimeError("denied")),
    )
    monkeypatch.setattr(health_checker, "check_model_health", forbidden_health)
    monkeypatch.setattr(health_checker, "upsert_alert", lambda *_a, **_k: None)
    monkeypatch.setattr(health_checker, "resolve_alert_by_fingerprint", lambda *_a, **_k: None)
    monkeypatch.setattr(health_checker.asyncio, "sleep", stop_after_one_cycle)

    with pytest.raises(asyncio.CancelledError):
        await health_checker._health_check_loop()
    assert sleep_calls == 1
    assert network_calls == []
    db.refresh(model)
    assert model.health_status == health_checker.HEALTH_UNHEALTHY


def test_auto_seed_formal_registration_is_inactive_quarantine(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SessionProxy:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            return None

    monkeypatch.setattr(auto_seed, "SessionLocal", _SessionProxy)
    monkeypatch.setattr(auto_seed, "_formal_model_governance_enabled", lambda: True)
    monkeypatch.setattr(auto_seed, "_parse_model_env_vars", lambda: [])
    monkeypatch.setattr(auto_seed.settings, "ADMIN_USERNAME", "provider-seed-admin")
    monkeypatch.setattr(auto_seed.settings, "ADMIN_PASSWORD", "provider-seed-password")
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_MODELS",
        '[{"name":"provider-seed-model","endpoint_url":"http://seed.internal:8000"}]',
    )
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_AGENTS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_LINKS", "")

    auto_seed.auto_seed()

    row = db.query(ModelRegistry).filter_by(name="provider-seed-model").one()
    assert row.is_active is False


def test_auto_seed_development_registration_remains_active(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _SessionProxy:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            return None

    monkeypatch.setattr(auto_seed, "SessionLocal", _SessionProxy)
    monkeypatch.setattr(auto_seed, "_formal_model_governance_enabled", lambda: False)
    monkeypatch.setattr(auto_seed, "_parse_model_env_vars", lambda: [])
    monkeypatch.setattr(auto_seed.settings, "ADMIN_USERNAME", "provider-dev-seed-admin")
    monkeypatch.setattr(auto_seed.settings, "ADMIN_PASSWORD", "provider-dev-seed-password")
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_MODELS",
        '[{"name":"provider-dev-seed-model","endpoint_url":"http://seed.internal:8000"}]',
    )
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_AGENTS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_LINKS", "")

    auto_seed.auto_seed()

    row = db.query(ModelRegistry).filter_by(name="provider-dev-seed-model").one()
    assert row.is_active is True


def test_auto_seed_readiness_exception_is_preserved_and_session_closed(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = []

    class _SessionProxy:
        def __getattr__(self, name):
            return getattr(db, name)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(auto_seed, "SessionLocal", _SessionProxy)
    monkeypatch.setattr(
        auto_seed,
        "_formal_model_governance_enabled",
        lambda: (_ for _ in ()).throw(RuntimeError("bootstrap readiness failed")),
    )

    with pytest.raises(RuntimeError, match="bootstrap readiness failed"):
        auto_seed.auto_seed()
    assert closed == [True]


class _TrackedSeedSession:
    def __init__(self, db, *, fail_commit: bool = False) -> None:
        self.db = db
        self.fail_commit = fail_commit
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def __getattr__(self, name):
        return getattr(self.db, name)

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise RuntimeError("synthetic seed commit failure")
        return self.db.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.db.rollback()

    def close(self):
        self.closes += 1


def _configure_formal_seed(
    monkeypatch: pytest.MonkeyPatch,
    session: _TrackedSeedSession,
    *,
    admin_username: str,
    models: str = "",
    links: str = "",
) -> None:
    monkeypatch.setattr(auto_seed, "SessionLocal", lambda: session)
    monkeypatch.setattr(auto_seed, "_formal_model_governance_enabled", lambda: True)
    monkeypatch.setattr(auto_seed, "_parse_model_env_vars", lambda: [])
    monkeypatch.setattr(auto_seed.settings, "ADMIN_USERNAME", admin_username)
    monkeypatch.setattr(auto_seed.settings, "ADMIN_PASSWORD", "unused-existing-admin")
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_MODELS", models)
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_AGENTS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_SEED_API_KEYS", "")
    monkeypatch.setattr(auto_seed.settings, "AUTO_REGISTER_LINKS", links)


@pytest.mark.parametrize("endpoint_drift", [False, True])
def test_formal_active_seed_authority_error_rolls_back_without_startup_crash(
    db,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    endpoint_drift: bool,
) -> None:
    admin = make_user(db, username=f"seed-authority-admin-{endpoint_drift}")
    model = make_model(db, name=f"formal-seed-existing-{endpoint_drift}")
    model.endpoint_url = "http://model.internal:8000"
    model.is_active = True
    db.commit()
    session = _TrackedSeedSession(db)
    configured_endpoint = (
        "http://other.internal:8000"
        if endpoint_drift
        else "http://model.internal:8000"
    )
    _configure_formal_seed(
        monkeypatch,
        session,
        admin_username=admin.username,
        models=(
            '[{"name":"'
            + model.name
            + '","endpoint_url":"'
            + configured_endpoint
            + '"}]'
        ),
    )
    authority_calls = []

    def deny_authority(row):
        authority_calls.append(row.id)
        raise RuntimeError("synthetic signed authority missing")

    monkeypatch.setattr(auto_seed, "admit_registry_provider", deny_authority)

    auto_seed.auto_seed()

    db.expire_all()
    persisted = db.get(ModelRegistry, model.id)
    assert persisted.endpoint_url == "http://model.internal:8000"
    assert persisted.is_active is True
    assert authority_calls == ([] if endpoint_drift else [model.id])
    assert (session.commits, session.rollbacks, session.closes) == (0, 1, 1)
    assert "seed transaction 已回滾" in caplog.text


@pytest.mark.parametrize("failure_stage", ["links", "commit"])
def test_formal_non_governance_seed_error_rolls_back_without_startup_crash(
    db,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_stage: str,
) -> None:
    admin = make_user(db, username=f"seed-error-admin-{failure_stage}")
    session = _TrackedSeedSession(db, fail_commit=(failure_stage == "commit"))
    _configure_formal_seed(
        monkeypatch,
        session,
        admin_username=admin.username,
        links=(
            '[{"name":"bad-link","url":"ftp://invalid.example"}]'
            if failure_stage == "links"
            else ""
        ),
    )

    auto_seed.auto_seed()

    assert session.commits == (1 if failure_stage == "commit" else 0)
    assert session.rollbacks == 1
    assert session.closes == 1
    assert "seed transaction 已回滾" in caplog.text


def test_formal_active_auto_seed_row_requires_authority_and_cannot_swap_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        name="formal-seeded-row",
        endpoint_url="http://model.internal:8000",
        is_active=True,
    )
    admissions = []
    monkeypatch.setattr(
        auto_seed,
        "admit_registry_provider",
        lambda model: admissions.append(model),
    )

    auto_seed._admit_active_seed_row(
        row, "http://model.internal:8000", formal=True
    )
    assert admissions == [row]

    with pytest.raises(RuntimeError, match="禁止.*改寫"):
        auto_seed._admit_active_seed_row(
            row, "http://other.internal:8000", formal=True
        )
    assert admissions == [row]
