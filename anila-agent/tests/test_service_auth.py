"""service-wrapper 認證 fail-closed 契約。"""

from __future__ import annotations

import pytest

from anila_agent.serving.auth import trusted_user_identity, verify_service_token

pytestmark = pytest.mark.unit


def test_matching_token_allowed():
    assert verify_service_token("csk-abc", "csk-abc") is True


def test_mismatching_token_rejected():
    assert verify_service_token("csk-bad", "csk-abc") is False


def test_missing_provided_rejected():
    assert verify_service_token(None, "csk-abc") is False
    assert verify_service_token("", "csk-abc") is False


def test_unset_expected_fails_closed_by_default():
    # 未設 expected（忘記設 csk-）→ 預設拒絕，不是門戶大開。
    assert verify_service_token("anything", "") is False
    assert verify_service_token(None, "") is False


def test_unset_expected_allowed_only_with_explicit_optout():
    assert verify_service_token(None, "", allow_unset=True) is True


def test_identity_trusted_only_after_verify():
    assert trusted_user_identity(False, user_id="u", email="e", groups="g") == {}
    trusted = trusted_user_identity(True, user_id="u", email="e", groups="g")
    assert trusted == {"user_id": "u", "email": "e", "groups": "g"}
