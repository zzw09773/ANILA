# -*- coding: utf-8 -*-
"""``is_asr_primary`` 三端點 — 完全比照 image-primary 模式。

* ``POST /api/models/{id}/set-asr-primary``  — admin;限 model_type=="asr"
* ``POST /api/models/{id}/unset-asr-primary`` — admin;冪等
* ``GET  /api/models/asr-primary``            — service token 或已登入使用者;
  未設 404、停用 409;成功回傳無 decoder token / api_key。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.model_registry import ModelRegistry
from app.services.endpoint_author_service import ENDPOINT_REDACTED
from tests.conftest import login, make_user


def make_asr_model(db, name="asr-decoder-gpu", is_active=True) -> ModelRegistry:
    m = ModelRegistry(
        name=name,
        display_name=name,
        model_type="asr",
        endpoint_url="https://asr-decoder.example.test:9000",
        is_active=is_active,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def admin_headers(client, db, username="admin-asr") -> dict[str, str]:
    make_user(db, username=username, role="admin")
    token = login(client, username)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def service_token_header(monkeypatch) -> dict[str, str]:
    from app.config import settings as canonical_settings
    from app.services import auth_service

    token = "csk-test-asr-primary-12345"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)
    return {"X-CSP-Service-Token": token}


class TestSetAsrPrimary:
    def test_success(self, client, db):
        model = make_asr_model(db)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_asr_primary"] is True

    def test_rejects_non_asr_type(self, client, db):
        model = ModelRegistry(
            name="gpt-asr-wrong-type",
            display_name="gpt-asr-wrong-type",
            model_type="llm",
            endpoint_url="http://mock-llm:8080",
            is_active=True,
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        assert resp.status_code == 400
        assert resp.json()["detail"] == "僅 asr 類型可設為主語音辨識模型"

    def test_rejects_inactive_model(self, client, db):
        model = make_asr_model(db, is_active=False)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        assert resp.status_code == 400

    def test_clears_previous_primary(self, client, db):
        first = make_asr_model(db, name="asr-a")
        second = make_asr_model(db, name="asr-b")
        headers = admin_headers(client, db)

        resp1 = client.post(f"/api/models/{first.id}/set-asr-primary", headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["is_asr_primary"] is True

        resp2 = client.post(f"/api/models/{second.id}/set-asr-primary", headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["is_asr_primary"] is True

        db.refresh(first)
        db.refresh(second)
        assert first.is_asr_primary is False
        assert second.is_asr_primary is True

    def test_requires_admin(self, client, db):
        model = make_asr_model(db)
        make_user(db, username="plain-asr", role="user")
        token = login(client, "plain-asr")

        resp = client.post(
            f"/api/models/{model.id}/set-asr-primary",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 403


class TestUnsetAsrPrimary:
    def test_unset_after_set(self, client, db):
        model = make_asr_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        resp = client.post(f"/api/models/{model.id}/unset-asr-primary", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_asr_primary"] is False

    def test_idempotent_when_not_primary(self, client, db):
        model = make_asr_model(db)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/unset-asr-primary", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_asr_primary"] is False


class TestGetAsrPrimary:
    def test_requires_auth(self, client, db):
        resp = client.get("/api/models/asr-primary")
        assert resp.status_code == 401

    def test_404_when_unset(self, client, db, service_token_header):
        resp = client.get("/api/models/asr-primary", headers=service_token_header)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "尚未指定主語音辨識模型"

    def test_409_when_primary_disabled(self, client, db, service_token_header):
        model = make_asr_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        model.is_active = False
        db.commit()

        resp = client.get("/api/models/asr-primary", headers=service_token_header)
        assert resp.status_code == 409

    def test_service_token_sees_real_endpoint(self, client, db, service_token_header):
        model = make_asr_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        resp = client.get("/api/models/asr-primary", headers=service_token_header)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body.keys()) == {
            "id",
            "name",
            "display_name",
            "model_type",
            "endpoint_url",
            "api_version",
            "health_status",
        }
        assert body["endpoint_url"] == "https://asr-decoder.example.test:9000"
        for forbidden in (
            "key",
            "api_key",
            "api_key_secret_ref",
            "has_api_key",
            "ASR_DECODER_TOKEN",
            "token",
        ):
            assert forbidden not in body

    def test_undesignated_user_sees_redacted_endpoint(self, client, db):
        model = make_asr_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        make_user(db, username="plain-asr2", role="user")
        token = login(client, "plain-asr2")

        resp = client.get(
            "/api/models/asr-primary",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["endpoint_url"] == ENDPOINT_REDACTED

    def test_deactivate_clears_asr_primary(self, client, db):
        model = make_asr_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-asr-primary", headers=headers)

        resp = client.delete(f"/api/models/{model.id}", headers=headers)
        assert resp.status_code == 200, resp.text

        db.refresh(model)
        assert model.is_asr_primary is False
