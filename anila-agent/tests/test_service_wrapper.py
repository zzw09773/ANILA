"""verify_service_token — inbound X-CSP-Service-Token check for the service
wrapper example (#114).

Only the pure security helper is exercised here; the FastAPI app itself is
copy-into-your-repo example code.
"""
from __future__ import annotations

import pytest

# The service wrapper needs the `serving` extra (fastapi). Skip gracefully when
# it isn't installed — core anila-agent is a CLI/library without it.
pytest.importorskip("fastapi")

from examples.service_wrapper import verify_service_token  # noqa: E402


def test_unset_token_rejected_by_default():
    # S-Q1/Q3: no configured token → fail-closed (reject), not wide-open.
    assert verify_service_token(None, "") is False
    assert verify_service_token("anything", "") is False


def test_unset_token_allowed_only_with_explicit_dev_optout():
    # ANILA_ALLOW_NO_SERVICE_TOKEN=1 maps to allow_unset=True (local dev only).
    assert verify_service_token(None, "", allow_unset=True) is True
    assert verify_service_token("anything", "", allow_unset=True) is True


def test_matching_token_allowed():
    assert verify_service_token("csk-secret", "csk-secret") is True


def test_mismatched_token_rejected():
    assert verify_service_token("csk-wrong", "csk-secret") is False


def test_missing_token_rejected_when_expected_set():
    assert verify_service_token(None, "csk-secret") is False
    assert verify_service_token("", "csk-secret") is False


@pytest.mark.parametrize("provided", ["csk-secre", "csk-secret ", " csk-secret", "CSK-SECRET"])
def test_near_miss_tokens_rejected(provided):
    assert verify_service_token(provided, "csk-secret") is False
