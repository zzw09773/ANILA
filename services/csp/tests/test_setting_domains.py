# -*- coding: utf-8 -*-
"""值域回歸：弱字串不能把任意文字送進各自的消費者。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.settings_registry import REGISTRY, SETTINGS


WEAK_STRING_KEYS = frozenset(
    {
        # Task L table: B_LOCKED / SEC rows.
        "card.ca_bundle_path",
        "card.initial_owners",
        "network.allowed_hosts",
        "network.allowed_origins",
        "network.environment",
        "network.ssl_cert_file",
        "network.trusted_hosts",
        "agents.template_dir",
        "alerts.smtp_from",
        "alerts.smtp_host",
        "alerts.smtp_to",
        "alerts.smtp_user",
        "app.host",
        "app.python_unbuffered",
        "db.legacy_sqlite_path",
        "ingestion.docling_ocr_langs",
        "ingestion.vision_model",
        # Task L table: A rows.
        "ingestion.vision_api_key",
        "proxy.model_gateway_api_key",
        "service.codeserver_password",
        "service.csp_service_token",
        "service.internal_platform_api_key",
        # Re-run census found these additional direct-string validators.
        "admin.password",
        "alerts.smtp_password",
        "auth.secret_key",
        "auth.secret_key_fallback",
        "db.app_role_password",
        "db.migration_url",
        "db.url",
    }
)

COMMON_BAD_VALUES = ("", " \t ", "\x00", "x" * 3000)
CENSUS_PROBES = (
    "",
    " \t ",
    "x" * 3000,
    "../../etc/passwd",
    "definitely not a path",
    "/nonexistent/nope.pem",
)


def _csp_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _ca_bundle() -> str:
    return str(_csp_dir() / "app" / "services" / "cspki_ca_bundle.pem")


def _accepts(domain, value) -> bool:
    try:
        return bool(domain(value))
    except (TypeError, ValueError):
        return False


ACCEPTED_VALUES = {
    "app.host": "csp.example.org",
    "app.python_unbuffered": "1",
    "db.url": "postgresql://csp:runtime-password@db:5432/csp",
    "db.migration_url": "postgresql://csp:migration-password@db:5432/csp",
    "db.app_role_password": "runtime-db-password-4321",
    "db.legacy_sqlite_path": str(Path(__file__).resolve()),
    "auth.secret_key": "runtime-secret-value-4321",
    "auth.secret_key_fallback": "fallback-secret-value-4321",
    "admin.password": "admin-password-value-4321",
    "card.initial_owners": "1234567,7654321",
    "card.ca_bundle_path": _ca_bundle(),
    "network.allowed_origins": "https://anila.example.org:4443,http://localhost",
    "network.allowed_hosts": "*.example.org,anila.example.org",
    "network.trusted_hosts": "gemma4,model.example.org,10.53.100.12",
    "network.environment": "production",
    "network.ssl_cert_file": _ca_bundle(),
    "proxy.model_gateway_api_key": "model-gateway-key-4321",
    "alerts.smtp_host": "smtp.example.org",
    "alerts.smtp_user": "smtp-user-4321",
    "alerts.smtp_password": "smtp-password-value-4321",
    "alerts.smtp_from": "alerts@example.org",
    "alerts.smtp_to": "ops@example.org",
    "service.csp_service_token": "csp-service-token-4321",
    "service.internal_platform_api_key": "internal-platform-key-4321",
    "service.codeserver_password": "code-server-password-4321",
    "agents.template_dir": str(_csp_dir()),
    "ingestion.vision_model": "org/vision-model-v2",
    "ingestion.vision_api_key": "vision-api-key-4321",
    "ingestion.docling_ocr_langs": "ch_tra,en",
}


def test_weak_string_census_is_explicit_and_complete():
    observed = {
        spec.key
        for spec in SETTINGS
        if spec.value_type.name == "str"
        and all(_accepts(spec.domain_fn, value) for value in CENSUS_PROBES)
    }
    assert observed == set(), observed
    assert set(ACCEPTED_VALUES) == WEAK_STRING_KEYS


def test_non_string_census_has_no_six_probe_weak_domain():
    observed = {
        spec.key
        for spec in SETTINGS
        if spec.value_type.name != "str"
        and all(_accepts(spec.domain_fn, value) for value in CENSUS_PROBES)
    }
    assert observed == set()


@pytest.mark.parametrize("key", sorted(WEAK_STRING_KEYS))
def test_each_weak_string_rejects_common_bad_shapes_and_accepts_a_real_shape(key):
    spec = REGISTRY[key]
    for value in COMMON_BAD_VALUES:
        assert not _accepts(spec.domain_fn, value), f"{key} 不應接受 {value!r}"
    accepted = ACCEPTED_VALUES[key]
    assert _accepts(spec.domain_fn, accepted), f"{key} 應接受 {accepted!r}"


@pytest.mark.parametrize(
    ("key", "bad_values"),
    [
        (
            "network.allowed_hosts",
            ("10.53.*.15", "http://example.org", "*.example.org/path"),
        ),
        (
            "network.trusted_hosts",
            ("*.example.org", "http://example.org", "example.org/path"),
        ),
        (
            "network.allowed_origins",
            ("*", "https://example.org/path", "ftp://example.org"),
        ),
    ],
)
def test_network_redline_domains_match_the_consumers(key, bad_values):
    domain = REGISTRY[key].domain_fn
    for value in bad_values:
        assert not domain(value), f"{key} 不應接受消費者不認得的 {value!r}"


def test_certificate_redline_requires_a_readable_pem_bundle_without_private_key(tmp_path):
    invalid = tmp_path / "invalid.pem"
    invalid.write_text("definitely not a certificate", encoding="ascii")
    private_key = tmp_path / "private-key.pem"
    private_key.write_bytes(
        b"-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n"
    )
    domain = REGISTRY["card.ca_bundle_path"].domain_fn
    ssl_domain = REGISTRY["network.ssl_cert_file"].domain_fn
    bad_values = (
        str(tmp_path / "missing.pem"),
        str(invalid),
        str(private_key),
    )
    for value in bad_values:
        assert not domain(value)
        assert not ssl_domain(value)
    assert domain(_ca_bundle())
    assert ssl_domain(_ca_bundle())


def test_card_owner_and_environment_domains_refuse_consumer_ambiguity():
    assert not REGISTRY["card.initial_owners"].domain_fn("12345")
    assert not REGISTRY["card.initial_owners"].domain_fn("1234567,abc")
    assert REGISTRY["card.initial_owners"].domain_fn("1234567,7654321")
    assert not REGISTRY["network.environment"].domain_fn("staging")
    assert REGISTRY["network.environment"].domain_fn("prod")
