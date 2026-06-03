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


def test_dev_mode_allows_when_expected_unset():
    # No configured token → dev mode: everything passes (startup warns).
    assert verify_service_token(None, "") is True
    assert verify_service_token("anything", "") is True


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
