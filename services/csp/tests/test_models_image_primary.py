# -*- coding: utf-8 -*-
"""``is_image_primary`` 三端點(doc 2026-07-06-flux-image-primary-design.md §1)。

完全比照既有 router-primary 模式(``app/api/models.py`` 的
set/unset/get-router-primary 三件組),換到 image 類型:

* ``POST /api/models/{id}/set-image-primary``  — admin;限 model_type=="image"
  且 is_active;先清舊 primary 再設(partial-unique 唯一性由應用層保證,
  SQLite 測試庫不跑 migration 的 partial unique index,行為以此測試為準)。
* ``POST /api/models/{id}/unset-image-primary`` — admin;冪等。
* ``GET  /api/models/image-primary``            — service token;未設 404、
  停用 409、成功回傳無 key 的最小 shape。
"""
from __future__ import annotations

import os

# 同 tests/test_revocations_endpoint.py / test_artifact_contract.py 的既有
# 慣例:單獨跑本檔(不靠其他測試模組在 import 期間先設好)也要能過。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi import HTTPException

from app.models.model_registry import ModelRegistry
from tests.conftest import login, make_user


def make_image_model(db, name="flux-cloud", is_active=True) -> ModelRegistry:
    m = ModelRegistry(
        name=name,
        display_name=name,
        model_type="image",
        endpoint_url="https://flux.example.com/v1",
        is_active=is_active,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def admin_headers(client, db, username="admin1") -> dict[str, str]:
    make_user(db, username=username, role="admin")
    token = login(client, username)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def service_token_header(monkeypatch) -> dict[str, str]:
    """同 tests/test_revocations_endpoint.py 的 legacy service-token 注入手法:
    monkeypatch 到 canonical settings + auth_service 綁定的那份,兩邊都補
    (auth_service import-time 綁定,不會隨後續 reload 自動同步)。"""
    from app.config import settings as canonical_settings
    from app.services import auth_service

    token = "csk-test-image-primary-12345"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)
    return {"X-CSP-Service-Token": token}


# ── POST /{model_id}/set-image-primary ──────────────────────────────────────


class TestSetImagePrimary:
    def test_success(self, client, db):
        model = make_image_model(db)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_image_primary"] is True

    def test_rejects_non_image_type(self, client, db):
        model = ModelRegistry(
            name="gpt-4o-mini-test",
            display_name="gpt-4o-mini-test",
            model_type="llm",
            endpoint_url="http://mock-llm:8080",
            is_active=True,
        )
        db.add(model)
        db.commit()
        db.refresh(model)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        assert resp.status_code == 400
        assert resp.json()["detail"] == "僅 image 類型可設為主圖像模型"

    def test_rejects_inactive_model(self, client, db):
        model = make_image_model(db, is_active=False)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        assert resp.status_code == 400

    def test_clears_previous_primary(self, client, db):
        first = make_image_model(db, name="flux-a")
        second = make_image_model(db, name="flux-b")
        headers = admin_headers(client, db)

        resp1 = client.post(f"/api/models/{first.id}/set-image-primary", headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["is_image_primary"] is True

        resp2 = client.post(f"/api/models/{second.id}/set-image-primary", headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["is_image_primary"] is True

        db.refresh(first)
        db.refresh(second)
        assert first.is_image_primary is False
        assert second.is_image_primary is True

    def test_requires_admin(self, client, db):
        model = make_image_model(db)
        make_user(db, username="plain1", role="user")
        token = login(client, "plain1")

        resp = client.post(
            f"/api/models/{model.id}/set-image-primary",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 403


# ── POST /{model_id}/unset-image-primary ────────────────────────────────────


class TestUnsetImagePrimary:
    def test_unset_after_set(self, client, db):
        model = make_image_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        resp = client.post(f"/api/models/{model.id}/unset-image-primary", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_image_primary"] is False

    def test_idempotent_when_not_primary(self, client, db):
        model = make_image_model(db)
        headers = admin_headers(client, db)

        resp = client.post(f"/api/models/{model.id}/unset-image-primary", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["is_image_primary"] is False


# ── GET /image-primary ───────────────────────────────────────────────────────


class TestGetImagePrimary:
    def test_requires_service_token(self, client, db):
        resp = client.get("/api/models/image-primary")
        assert resp.status_code == 401

    def test_404_when_unset(self, client, db, service_token_header):
        resp = client.get("/api/models/image-primary", headers=service_token_header)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "尚未指定主圖像模型"

    def test_409_when_primary_disabled(self, client, db, service_token_header):
        model = make_image_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        # 直接停用底層 row(不經 deactivate 端點),模擬 flag 落單的邊界情境。
        model.is_active = False
        db.commit()

        resp = client.get("/api/models/image-primary", headers=service_token_header)
        assert resp.status_code == 409

    def test_success_shape_has_no_key_field(self, client, db, service_token_header):
        model = make_image_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        resp = client.get("/api/models/image-primary", headers=service_token_header)

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
        for forbidden in ("key", "api_key", "api_key_secret_ref", "has_api_key"):
            assert forbidden not in body

    def test_active_primary_revalidates_provider_authority(
        self, client, db, service_token_header, monkeypatch
    ):
        """A revoked/rotated provider must not be served from the selector row."""
        model = make_image_model(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-image-primary", headers=headers)

        from app.api import models as models_api

        def deny(_model):
            raise HTTPException(status_code=503, detail="provider authority revoked")

        monkeypatch.setattr(models_api, "_require_provider_authority", deny)

        resp = client.get("/api/models/image-primary", headers=service_token_header)

        assert resp.status_code == 503
        assert "provider authority" in resp.json()["detail"]
