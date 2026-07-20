"""Formal Gate 2 G17 startup posture checks."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.startup_security import assert_card_crl_policy


def test_non_card_profile_does_not_require_crl(monkeypatch):
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", False)
    monkeypatch.setattr(settings, "CARD_CRL_REQUIRED", False)
    assert_card_crl_policy()


@pytest.mark.parametrize(
    "required,path,policies,source",
    [
        (False, "/formal/card.crl.pem", "1.2.3.4", "offline-feed"),
        (True, "", "1.2.3.4", "offline-feed"),
        (True, "/formal/card.crl.pem", "", "offline-feed"),
        (True, "/formal/card.crl.pem", "1.2.3.4", ""),
        (True, "/formal/card.crl.pem", "1.2.3.4", "manual"),
        (
            True,
            "/formal/card.crl.pem",
            "1.2.3.4",
            "<offline-crl-source-and-sync-owner>",
        ),
    ],
)
def test_card_only_profile_rejects_incomplete_crl_posture(
    monkeypatch, required, path, policies, source,
):
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(settings, "CARD_CRL_REQUIRED", required)
    monkeypatch.setattr(settings, "CARD_CRL_BUNDLE_PATH", path)
    monkeypatch.setattr(settings, "CARD_REQUIRED_CERT_POLICY_OIDS", policies)
    monkeypatch.setattr(settings, "CARD_CRL_SOURCE", source)
    with pytest.raises(RuntimeError):
        assert_card_crl_policy()


def test_card_only_profile_accepts_complete_crl_posture(
    monkeypatch, tmp_path, synthetic_card_trust, caplog,
):
    from app.services import card_auth

    crl_path = tmp_path / "card.crl.pem"
    crl_path.write_bytes(synthetic_card_trust.crl_pem())
    monkeypatch.setattr(card_auth, "_crl_cache", None)
    monkeypatch.setattr(card_auth, "_crl_cache_source", None)
    monkeypatch.setattr(settings, "REQUIRE_CARD_LOGIN_ONLY", True)
    monkeypatch.setattr(settings, "CARD_CRL_REQUIRED", True)
    monkeypatch.setattr(settings, "CARD_CRL_BUNDLE_PATH", str(crl_path))
    monkeypatch.setattr(settings, "CARD_CRL_SOURCE", "synthetic-offline-feed")
    monkeypatch.setattr(
        settings,
        "CARD_REQUIRED_CERT_POLICY_OIDS",
        "1.3.6.1.4.1.55555.1.1",
    )
    with caplog.at_level("INFO"):
        assert_card_crl_policy()
    records = [
        record.getMessage()
        for record in caplog.records
        if "card CRL validated" in record.getMessage()
    ]
    assert records
    assert all("source=synthetic-offline-feed" in record for record in records)
    assert all("this_update=" in record and "next_update=" in record for record in records)
