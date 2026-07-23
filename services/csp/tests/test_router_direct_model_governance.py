"""R7.1: CSP internal direct-answer model governance endpoint.

The Router derives its DIRECT_ANSWER classification ceiling from this seam
instead of a manual env knob.  Auth mirrors the internal registry snapshot:
a named, non-legacy service client only; unknown/inactive/non-llm rows fail
closed as 404.
"""

from __future__ import annotations

import pytest

from app.main import app
from app.services.agent_credential_service import CallerIdentity
from app.services.auth_service import verify_service_token
from tests.conftest import make_model

ENDPOINT = "/internal/v1/router/direct-model-governance"


def _named_service_client() -> CallerIdentity:
    return CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=1,
        credential_id=1,
        is_legacy=False,
        used_previous_token=False,
    )


def test_direct_model_governance_requires_token(client, db):
    make_model(db, name="google/gemma4")
    resp = client.get(ENDPOINT, params={"model": "google/gemma4"})
    assert resp.status_code == 401, resp.text


def test_direct_model_governance_rejects_wrong_token(client, db):
    make_model(db, name="google/gemma4")
    resp = client.get(
        ENDPOINT,
        params={"model": "google/gemma4"},
        headers={"X-CSP-Service-Token": "csk-not-a-real-token"},
    )
    assert resp.status_code == 401, resp.text


def test_direct_model_governance_named_client_returns_ceiling(client, db):
    model = make_model(db, name="google/gemma4")
    model.classification_ceiling = "機密"
    db.commit()
    app.dependency_overrides[verify_service_token] = _named_service_client
    try:
        resp = client.get(ENDPOINT, params={"model": "google/gemma4"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "model_id": "google/gemma4",
            "gateway": "csp",
            "classification_ceiling": "機密",
        }
    finally:
        app.dependency_overrides.pop(verify_service_token, None)


@pytest.mark.parametrize(
    "identity",
    [
        CallerIdentity(
            kind="agent",
            agent_id=1,
            service_client_id=None,
            credential_id=1,
            is_legacy=False,
            used_previous_token=False,
        ),
        CallerIdentity(
            kind="service_client",
            agent_id=None,
            service_client_id=1,
            credential_id=1,
            is_legacy=True,
            used_previous_token=False,
        ),
    ],
)
def test_direct_model_governance_rejects_agent_or_legacy(client, db, identity):
    make_model(db, name="google/gemma4")
    app.dependency_overrides[verify_service_token] = lambda: identity
    try:
        resp = client.get(ENDPOINT, params={"model": "google/gemma4"})
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.pop(verify_service_token, None)


def test_direct_model_governance_unknown_model_404(client, db):
    app.dependency_overrides[verify_service_token] = _named_service_client
    try:
        resp = client.get(ENDPOINT, params={"model": "does-not-exist"})
        assert resp.status_code == 404, resp.text
    finally:
        app.dependency_overrides.pop(verify_service_token, None)


def test_direct_model_governance_inactive_model_404(client, db):
    model = make_model(db, name="google/gemma4")
    model.is_active = False
    db.commit()
    app.dependency_overrides[verify_service_token] = _named_service_client
    try:
        resp = client.get(ENDPOINT, params={"model": "google/gemma4"})
        assert resp.status_code == 404, resp.text
    finally:
        app.dependency_overrides.pop(verify_service_token, None)


def test_direct_model_governance_non_llm_model_404(client, db):
    model = make_model(db, name="embed-model")
    model.model_type = "embedding"
    db.commit()
    app.dependency_overrides[verify_service_token] = _named_service_client
    try:
        resp = client.get(ENDPOINT, params={"model": "embed-model"})
        assert resp.status_code == 404, resp.text
    finally:
        app.dependency_overrides.pop(verify_service_token, None)
