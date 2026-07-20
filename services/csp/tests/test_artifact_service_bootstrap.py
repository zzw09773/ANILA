"""Named artifact Service Client bootstrap contract."""

from __future__ import annotations

import pytest

from app.models.registered_service import RegisteredService
from app.models.service_client import ServiceClient
from app.services.artifact_service_bootstrap import (
    CLIENT_NAME,
    SERVICE_SLUG,
    ensure_artifact_service,
)


TOKEN = "csk-bootstrap-artifact-test"


@pytest.fixture(autouse=True)
def _credential_master_key(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "artifact-bootstrap-test-master-key")


def test_bootstrap_creates_exact_nonlegacy_capability_and_is_idempotent(db):
    first = ensure_artifact_service(db, token=TOKEN)
    second = ensure_artifact_service(db, token=TOKEN)

    client = db.query(ServiceClient).filter_by(client_name=CLIENT_NAME).one()
    services = db.query(RegisteredService).filter_by(service_client_id=client.id).all()
    assert client.is_active is True
    assert client.is_legacy is False
    assert first.id == second.id
    assert len(services) == 1
    assert services[0].slug == SERVICE_SLUG
    assert services[0].service_type == "artifact_tool"
    assert services[0].data_egress == ["artifact"]


def test_bootstrap_refuses_implicit_token_rotation(db):
    ensure_artifact_service(db, token=TOKEN)
    with pytest.raises(RuntimeError, match="audited rotation"):
        ensure_artifact_service(db, token="csk-different-token")


def test_bootstrap_refuses_capability_broadening(db):
    service = ensure_artifact_service(db, token=TOKEN)
    service.data_egress = ["artifact", "trace"]
    db.commit()
    with pytest.raises(RuntimeError, match="capability"):
        ensure_artifact_service(db, token=TOKEN)
