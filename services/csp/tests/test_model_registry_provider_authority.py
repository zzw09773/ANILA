"""Focused tests for the CSP provider-locality persistence boundary."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import JSON as SAJSON
from sqlalchemy import CheckConstraint
from sqlalchemy.exc import IntegrityError

from anila_security import ProviderLocality, TransportTarget
from app.api.models import (
    _build_response,
    _enforce_endpoint_url,
    _prepare_provider_snapshot,
    create_model,
    update_model,
)
from app.models.model_registry import ModelRegistry
from app.schemas.model_registry import ModelCreate, ModelUpdate


def _target(value: str, *, dns_policy: str = "none") -> dict[str, object]:
    return TransportTarget.parse(value, dns_policy=dns_policy).to_dict()


def test_legacy_boolean_does_not_promote_locality() -> None:
    payload = ModelCreate(
        name="legacy",
        display_name="Legacy",
        model_type="llm",
        endpoint_url="https://api.example.com/v1",
        is_internal=False,
    )
    assert payload.provider_locality == ProviderLocality.UNCLASSIFIED.value
    assert payload.is_internal is False


@pytest.mark.parametrize(
    ("endpoint", "canonical"),
    [
        ("https://API.Provider.Example", "https://api.provider.example"),
        (
            "https://api.provider.example:8443/v1",
            "https://api.provider.example:8443/v1",
        ),
        ("model.internal:8000", "model.internal:8000"),
    ],
)
def test_snapshot_canonicalizes_host_port_and_default_port(
    endpoint: str, canonical: str
) -> None:
    snapshot = _prepare_provider_snapshot(
        endpoint_url=endpoint,
        provider_locality=ProviderLocality.INTERNAL_ISOLATED.value,
        transport_target=None,
        transport_target_sha256=None,
        model_registry_revision=None,
        upstream_provider_locality=None,
        upstream_transport_target=None,
        upstream_transport_target_sha256=None,
        egress_policy_id=None,
        upstream_egress_policy_id=None,
    )
    assert snapshot["endpoint_url"] == canonical
    assert snapshot["transport_target"]["host"] in {
        "api.provider.example",
        "model.internal",
    }
    assert snapshot["transport_target_sha256"] == TransportTarget.from_dict(
        snapshot["transport_target"]
    ).sha256


def test_external_projection_and_legacy_flag_are_distinct() -> None:
    row = SimpleNamespace(
        id=1,
        name="external",
        display_name="External",
        model_type="llm",
        endpoint_url="https://api.provider.example/v1",
        api_version="v1",
        is_active=True,
        is_router_primary=False,
        is_image_primary=False,
        health_status="unknown",
        health_checked_at=None,
        description=None,
        context_window=None,
        base_model_id=None,
        base_model=None,
        is_internal=True,
        provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        transport_target=_target(
            "https://api.provider.example/v1",
            dns_policy="production_fail_closed",
        ),
        transport_target_sha256="a" * 64,
        model_registry_revision="1",
        upstream_provider_locality=None,
        upstream_transport_target=_target("grpc://172.16.120.35:9001"),
        upstream_transport_target_sha256="b" * 64,
        egress_policy_id="egress.provider",
        upstream_egress_policy_id="egress.upstream",
        classification_ceiling="無機密",
        created_at=None,
        updated_at=None,
    )
    # The response projection follows locality, not the stale bool.
    admin_view = _build_response(row, caller=SimpleNamespace(role="admin"))
    assert admin_view["is_internal"] is False
    assert admin_view["transport_target"] is None
    assert admin_view["transport_target_sha256"] is None
    assert admin_view["model_registry_revision"] is None
    assert admin_view["upstream_transport_target"] is None
    assert admin_view["upstream_transport_target_sha256"] is None
    assert admin_view["egress_policy_id"] is None
    assert admin_view["upstream_egress_policy_id"] is None
    owner_view = _build_response(row, caller=SimpleNamespace(role="owner"))
    assert owner_view["transport_target"] == row.transport_target
    assert owner_view["transport_target_sha256"] == row.transport_target_sha256
    assert owner_view["model_registry_revision"] == row.model_registry_revision
    assert owner_view["upstream_transport_target"] == row.upstream_transport_target
    assert (
        owner_view["upstream_transport_target_sha256"]
        == row.upstream_transport_target_sha256
    )
    assert owner_view["egress_policy_id"] == row.egress_policy_id
    assert owner_view["upstream_egress_policy_id"] == row.upstream_egress_policy_id


def test_external_http_ip_is_rejected_until_signed_authority_is_server_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    target = _target(
        "http://172.16.120.35:7000/v1",
        dns_policy="production_fail_closed",
    )
    snapshot = _prepare_provider_snapshot(
        endpoint_url="http://172.16.120.35:7000/v1",
        provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        transport_target=target,
        transport_target_sha256=None,
        model_registry_revision=None,
        upstream_provider_locality=None,
        upstream_transport_target=None,
        upstream_transport_target_sha256=None,
        egress_policy_id="egress.provider",
        upstream_egress_policy_id=None,
    )
    assert snapshot["transport_target"]["dns_policy"] == "none"
    # Until Option C verifies signed authority server-side, neither a matching
    # client target nor an arbitrary egress ID may bypass production HTTPS.
    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url(snapshot["endpoint_url"])
    assert exc.value.status_code == 400
    assert "production model endpoint" in str(exc.value.detail)


def test_external_https_fqdn_remains_allowed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    _enforce_endpoint_url("https://api.provider.example/v1")


@pytest.mark.parametrize("allow_private", [False, True])
def test_external_https_private_ip_respects_allow_private_contract(
    monkeypatch: pytest.MonkeyPatch,
    allow_private: bool,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    if allow_private:
        monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
        _enforce_endpoint_url("https://172.16.120.35:7000/v1")
    else:
        monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
        with pytest.raises(HTTPException) as exc:
            _enforce_endpoint_url("https://172.16.120.35:7000/v1")
        assert "ANILA_ALLOW_PRIVATE_ENDPOINT" in str(exc.value.detail)


def test_direct_grpc_cannot_be_registered_as_the_csp_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    snapshot = _prepare_provider_snapshot(
        endpoint_url="grpc://172.16.120.35:9001",
        provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        transport_target="grpc://172.16.120.35:9001",
        transport_target_sha256=None,
        model_registry_revision=None,
        upstream_provider_locality=None,
        upstream_transport_target=None,
        upstream_transport_target_sha256=None,
        egress_policy_id="egress.provider",
        upstream_egress_policy_id=None,
    )

    with pytest.raises(HTTPException) as exc:
        _enforce_endpoint_url(snapshot["endpoint_url"])
    assert exc.value.status_code == 400


def test_internal_shim_can_snapshot_an_external_grpc_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANILA_ENV", raising=False)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "provider-shim")
    snapshot = _prepare_provider_snapshot(
        endpoint_url="http://provider-shim:8000/v1",
        provider_locality=ProviderLocality.INTERNAL_SHIM.value,
        transport_target=None,
        transport_target_sha256=None,
        model_registry_revision=None,
        upstream_provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        upstream_transport_target="grpc://172.16.120.35:9001",
        upstream_transport_target_sha256=None,
        egress_policy_id=None,
        upstream_egress_policy_id="egress.embedding",
    )

    assert snapshot["upstream_transport_target"]["scheme"] == "grpc"
    assert snapshot["upstream_transport_target"]["dns_policy"] == "none"
    assert snapshot["upstream_transport_target_sha256"]
    _enforce_endpoint_url(snapshot["endpoint_url"])


def test_external_governed_target_must_exactly_match_endpoint() -> None:
    with pytest.raises(HTTPException) as exc:
        _prepare_provider_snapshot(
            endpoint_url="http://172.16.120.35:7000/v1",
            provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
            transport_target="http://172.16.120.35:7001/v1",
            transport_target_sha256=None,
            model_registry_revision=None,
            upstream_provider_locality=None,
            upstream_transport_target=None,
            upstream_transport_target_sha256=None,
            egress_policy_id="egress.provider",
            upstream_egress_policy_id=None,
        )
    assert "must equal endpoint_url" in str(exc.value.detail)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:7000/v1",
        "http://169.254.169.254:7000/v1",
        "http://0.0.0.0:7000/v1",
        "http://224.0.0.1:7000/v1",
        "http://240.0.0.1:7000/v1",
    ],
)
def test_external_governed_never_admits_special_ip_ranges(
    endpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    snapshot = _prepare_provider_snapshot(
        endpoint_url=endpoint,
        provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        transport_target=endpoint,
        transport_target_sha256=None,
        model_registry_revision=None,
        upstream_provider_locality=None,
        upstream_transport_target=None,
        upstream_transport_target_sha256=None,
        egress_policy_id="egress.provider",
        upstream_egress_policy_id=None,
    )

    with pytest.raises(HTTPException):
        _enforce_endpoint_url(snapshot["endpoint_url"])


def test_explicit_null_provider_locality_is_not_a_patch_omit() -> None:
    with pytest.raises(ValueError, match="provider_locality"):
        ModelUpdate.model_validate({"provider_locality": None})
    update = ModelUpdate.model_validate({"display_name": "renamed"})
    assert "provider_locality" not in update.model_fields_set
    assert update.provider_locality is None


def test_partial_shim_update_is_rejected_before_orm_mutation() -> None:
    with pytest.raises(ValueError, match="upstream"):
        ModelUpdate(
            provider_locality=ProviderLocality.INTERNAL_SHIM.value,
            transport_target="shim.internal:8000",
        )


def test_endpoint_update_rebuilds_target_hash_and_revision_atomically() -> None:
    old = _prepare_provider_snapshot(
        endpoint_url="model.internal:8000",
        provider_locality=ProviderLocality.INTERNAL_ISOLATED.value,
        transport_target=None,
        transport_target_sha256=None,
        model_registry_revision=None,
        upstream_provider_locality=None,
        upstream_transport_target=None,
        upstream_transport_target_sha256=None,
        egress_policy_id=None,
        upstream_egress_policy_id=None,
    )
    new = _prepare_provider_snapshot(
        endpoint_url="model.internal:9000",
        provider_locality=ProviderLocality.INTERNAL_ISOLATED.value,
        transport_target=None,
        transport_target_sha256=None,
        model_registry_revision=old["model_registry_revision"],
        upstream_provider_locality=None,
        upstream_transport_target=None,
        upstream_transport_target_sha256=None,
        egress_policy_id=None,
        upstream_egress_policy_id=None,
        previous_revision=old["model_registry_revision"],
        snapshot_changed=True,
    )
    assert new["endpoint_url"] == "model.internal:9000"
    assert new["transport_target_sha256"] != old["transport_target_sha256"]
    assert new["model_registry_revision"] == "2"


def _orm_model(name: str, **values: object) -> ModelRegistry:
    defaults: dict[str, object] = {
        "name": name,
        "display_name": name,
        "model_type": "llm",
        "endpoint_url": "https://api.example.com/v1",
        "classification_ceiling": "無機密",
    }
    defaults.update(values)
    return ModelRegistry(**defaults)


def test_create_rejects_external_private_http_with_unverified_egress(
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    payload = ModelCreate(
        name="blocked-private-http-create",
        display_name="Blocked private HTTP create",
        model_type="llm",
        endpoint_url="http://172.16.120.35:7000/v1",
        provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        transport_target="http://172.16.120.35:7000/v1",
        egress_policy_id="egress.client-supplied",
    )

    with pytest.raises(HTTPException) as exc:
        create_model(
            request=payload,
            admin=SimpleNamespace(role="owner"),
            db=db,
        )
    assert exc.value.status_code == 400
    assert db.query(ModelRegistry).filter_by(name=payload.name).first() is None


@pytest.mark.parametrize("update_kind", ["endpoint", "is_active"])
def test_update_rejects_external_private_http_with_unverified_egress(
    db,
    monkeypatch: pytest.MonkeyPatch,
    update_kind: str,
) -> None:
    monkeypatch.setenv("ANILA_ENV", "production")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    private_http = "http://172.16.120.35:7000/v1"
    initial_endpoint = (
        "https://api.provider.example/v1"
        if update_kind == "endpoint"
        else private_http
    )
    target = _target(
        initial_endpoint,
        dns_policy=(
            "production_fail_closed"
            if update_kind == "endpoint"
            else "none"
        ),
    )
    row = _orm_model(
        f"blocked-private-http-update-{update_kind}",
        endpoint_url=initial_endpoint,
        is_active=False,
        provider_locality=ProviderLocality.EXTERNAL_GOVERNED.value,
        transport_target=target,
        transport_target_sha256=TransportTarget.from_dict(target).sha256,
        model_registry_revision="1",
        egress_policy_id="egress.client-supplied",
    )
    db.add(row)
    db.commit()

    update = (
        ModelUpdate(
            endpoint_url=private_http,
            egress_policy_id="egress.attacker-controlled",
        )
        if update_kind == "endpoint"
        else ModelUpdate(is_active=True)
    )
    with pytest.raises(HTTPException) as exc:
        update_model(
            model_id=row.id,
            request=update,
            admin=SimpleNamespace(role="owner"),
            db=db,
        )
    assert exc.value.status_code == 400
    db.refresh(row)
    assert row.endpoint_url == initial_endpoint
    assert row.is_active is False


@pytest.mark.parametrize(
    ("locality", "values"),
    [
        (
            ProviderLocality.EXTERNAL_GOVERNED.value,
            {
                "transport_target": _target("https://api.example.com/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
            },
        ),
        (
            ProviderLocality.INTERNAL_ISOLATED.value,
            {
                "transport_target": _target("http://model.internal:8000/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
                "egress_policy_id": "egress.unexpected",
            },
        ),
        (
            ProviderLocality.INTERNAL_SHIM.value,
            {
                "transport_target": _target("http://shim.internal:8000/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
                "upstream_provider_locality": ProviderLocality.EXTERNAL_GOVERNED.value,
                "upstream_transport_target": _target("https://api.example.com/v1"),
                "upstream_transport_target_sha256": "b" * 64,
            },
        ),
    ],
)
def test_classified_database_constraints_reject_incomplete_or_forbidden_snapshots(
    db, locality: str, values: dict[str, object]
) -> None:
    row = _orm_model(
        f"constraint-{locality}",
        provider_locality=locality,
        **values,
    )
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize(
    ("locality", "values"),
    [
        (
            ProviderLocality.EXTERNAL_GOVERNED.value,
            {
                "transport_target": _target("https://api.example.com/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
                "egress_policy_id": "egress.provider",
            },
        ),
        (
            ProviderLocality.INTERNAL_ISOLATED.value,
            {
                "transport_target": _target("http://model.internal:8000/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
            },
        ),
        (
            ProviderLocality.INTERNAL_SHIM.value,
            {
                "transport_target": _target("http://shim.internal:8000/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
                "upstream_provider_locality": ProviderLocality.EXTERNAL_GOVERNED.value,
                "upstream_transport_target": _target("https://api.example.com/v1"),
                "upstream_transport_target_sha256": "b" * 64,
                "upstream_egress_policy_id": "egress.upstream",
            },
        ),
    ],
)
def test_classified_database_constraints_accept_complete_snapshots(
    db, locality: str, values: dict[str, object]
) -> None:
    row = _orm_model(
        f"valid-{locality}",
        provider_locality=locality,
        **values,
    )
    db.add(row)
    db.commit()
    assert row.id is not None


@pytest.mark.parametrize(
    ("locality", "complete", "required_field"),
    [
        *[
            (
                ProviderLocality.EXTERNAL_GOVERNED.value,
                {
                    "transport_target": _target("https://api.example.com/v1"),
                    "transport_target_sha256": "a" * 64,
                    "model_registry_revision": "1",
                    "egress_policy_id": "egress.provider",
                },
                field,
            )
            for field in (
                "transport_target",
                "transport_target_sha256",
                "model_registry_revision",
                "egress_policy_id",
            )
        ],
        *[
            (
                ProviderLocality.INTERNAL_ISOLATED.value,
                {
                    "transport_target": _target("http://model.internal:8000/v1"),
                    "transport_target_sha256": "a" * 64,
                    "model_registry_revision": "1",
                },
                field,
            )
            for field in (
                "transport_target",
                "transport_target_sha256",
                "model_registry_revision",
            )
        ],
        *[
            (
                ProviderLocality.INTERNAL_SHIM.value,
                {
                    "transport_target": _target("http://shim.internal:8000/v1"),
                    "transport_target_sha256": "a" * 64,
                    "model_registry_revision": "1",
                    "upstream_provider_locality": ProviderLocality.EXTERNAL_GOVERNED.value,
                    "upstream_transport_target": _target("grpc://172.16.120.35:9001"),
                    "upstream_transport_target_sha256": "b" * 64,
                    "upstream_egress_policy_id": "egress.upstream",
                },
                field,
            )
            for field in (
                "transport_target",
                "transport_target_sha256",
                "model_registry_revision",
                "upstream_provider_locality",
                "upstream_transport_target",
                "upstream_transport_target_sha256",
                "upstream_egress_policy_id",
            )
        ],
    ],
)
def test_classified_database_constraints_reject_each_null_required_field(
    db,
    locality: str,
    complete: dict[str, object],
    required_field: str,
) -> None:
    values = {**complete, required_field: None}
    row = _orm_model(
        f"null-{locality}-{required_field}",
        provider_locality=locality,
        **values,
    )
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize(
    ("field", "values"),
    [
        (
            "transport_target",
            {
                "provider_locality": ProviderLocality.EXTERNAL_GOVERNED.value,
                "transport_target": _target("https://api.example.com/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
                "egress_policy_id": "egress.provider",
            },
        ),
        (
            "upstream_transport_target",
            {
                "provider_locality": ProviderLocality.INTERNAL_SHIM.value,
                "transport_target": _target("http://shim.internal:8000/v1"),
                "transport_target_sha256": "a" * 64,
                "model_registry_revision": "1",
                "upstream_provider_locality": ProviderLocality.EXTERNAL_GOVERNED.value,
                "upstream_transport_target": _target("grpc://172.16.120.35:9001"),
                "upstream_transport_target_sha256": "b" * 64,
                "upstream_egress_policy_id": "egress.upstream",
            },
        ),
    ],
)
def test_database_constraints_reject_json_null_targets(
    db,
    field: str,
    values: dict[str, object],
) -> None:
    values = {**values, field: SAJSON.NULL}
    row = _orm_model(f"json-null-{field}", **values)
    db.add(row)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_orm_and_migration_snapshot_checks_are_identical() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/r1_0030_model_registry_provider_authority.py"
    )
    spec = importlib.util.spec_from_file_location("r1_0030_static", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    orm_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in ModelRegistry.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    migration_checks = {
        "ck_model_registry_external_snapshot": migration._EXTERNAL_SNAPSHOT,
        "ck_model_registry_isolated_snapshot": migration._ISOLATED_SNAPSHOT,
        "ck_model_registry_shim_snapshot": migration._SHIM_SNAPSHOT,
    }

    def normalize(value: str) -> str:
        return re.sub(r"\s+", "", value)

    assert {
        name: normalize(orm_checks[name]) for name in migration_checks
    } == {
        name: normalize(expression) for name, expression in migration_checks.items()
    }


def test_schema_and_migration_expose_authority_columns_and_head() -> None:
    expected = {
        "provider_locality",
        "transport_target",
        "transport_target_sha256",
        "model_registry_revision",
        "upstream_provider_locality",
        "upstream_transport_target",
        "upstream_transport_target_sha256",
        "egress_policy_id",
        "upstream_egress_policy_id",
    }
    assert expected <= set(ModelRegistry.__table__.columns.keys())

    migration = Path(__file__).resolve().parents[1] / "migrations/versions/r1_0030_model_registry_provider_authority.py"
    text = migration.read_text(encoding="utf-8")
    assert 'revision: str = "r1_0030"' in text
    assert 'down_revision: Union[str, None] = "r1_0029"' in text
    assert "provider_locality IN" in text
    assert "ck_model_registry_external_snapshot" in text
    assert "ck_model_registry_isolated_snapshot" in text
    assert "ck_model_registry_shim_snapshot" in text
    assert "unclassified" in text
    assert re.search(r"UPDATE model_registry SET provider_locality", text)


def test_context_window_must_be_positive_at_the_write_boundary() -> None:
    """``context_window`` 現在會決定 Router 實際送出的 ``max_tokens``。

    它從「只給 UI 顯示的註記」升級成 Router token 預算的輸入
    (``anila_core.router.token_budget``),所以 0 / 負值不能再存進去 —— 一個 0 會
    讓預算算出「輸入上限為負」,把每一次呼叫都擋掉。在寫入邊界擋比讓壞值流到推論
    路徑再去猜要清楚得多。``None``(未登記)仍然合法:Router 對未登記的降級行為
    是刻意設計的 fail-safe。
    """

    base = {
        "name": "ctx",
        "display_name": "Ctx",
        "model_type": "llm",
        "endpoint_url": "https://api.example.com/v1",
    }
    assert ModelCreate(**base, context_window=8192).context_window == 8192
    assert ModelCreate(**base, context_window=None).context_window is None
    assert ModelUpdate(context_window=32768).context_window == 32768
    assert ModelUpdate().context_window is None

    for bad in (0, -1):
        with pytest.raises(ValidationError):
            ModelCreate(**base, context_window=bad)
        with pytest.raises(ValidationError):
            ModelUpdate(context_window=bad)
