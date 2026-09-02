# -*- coding: utf-8 -*-
"""``is_slides_primary`` 三端點 — 「主簡報模型」旋鈕，完全比照 image/asr-primary。

* ``POST /api/models/{id}/set-slides-primary``  — admin；限 model_type=="llm"
* ``POST /api/models/{id}/unset-slides-primary`` — admin；冪等
* ``GET  /api/models/slides-primary``            — service token 或已登入使用者；
  未設 404、停用 409；回傳不含金鑰。

anila-studio 每 60 秒問一次這個端點，拿到什麼就用什麼模型做簡報；沒設就退回
環境變數 ANILA_STUDIO_SLIDES_MODEL（2026-09-02 之前只有環境變數，還沒接進 compose）。
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.model_registry import ModelRegistry
from tests.conftest import login, make_user


def make_llm(db, name="gemma26-nothink", is_active=True) -> ModelRegistry:
    m = ModelRegistry(
        name=name, display_name=name, model_type="llm",
        endpoint_url="http://172.16.120.35:28080/v1", is_active=is_active,
    )
    db.add(m); db.commit(); db.refresh(m)
    return m


def admin_headers(client, db, username="admin-slides") -> dict[str, str]:
    make_user(db, username=username, role="admin")
    return {"Authorization": f"Bearer {login(client, username)}"}


@pytest.fixture
def service_token_header(monkeypatch) -> dict[str, str]:
    from app.config import settings as canonical_settings
    from app.services import auth_service
    token = "csk-test-slides-primary-12345"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)
    return {"X-CSP-Service-Token": token}


class TestSetSlidesPrimary:
    def test_success(self, client, db):
        model = make_llm(db)
        resp = client.post(f"/api/models/{model.id}/set-slides-primary", headers=admin_headers(client, db))
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_slides_primary"] is True

    def test_rejects_non_llm(self, client, db):
        m = ModelRegistry(name="emb", display_name="emb", model_type="embedding", endpoint_url="http://e/v1", is_active=True)
        db.add(m); db.commit(); db.refresh(m)
        resp = client.post(f"/api/models/{m.id}/set-slides-primary", headers=admin_headers(client, db))
        assert resp.status_code == 400
        assert resp.json()["detail"] == "僅 llm 類型可設為主簡報模型"

    def test_rejects_inactive(self, client, db):
        model = make_llm(db, is_active=False)
        resp = client.post(f"/api/models/{model.id}/set-slides-primary", headers=admin_headers(client, db))
        assert resp.status_code == 400

    def test_clears_previous_primary(self, client, db):
        a = make_llm(db, name="a"); b = make_llm(db, name="b")
        headers = admin_headers(client, db)
        assert client.post(f"/api/models/{a.id}/set-slides-primary", headers=headers).status_code == 200
        assert client.post(f"/api/models/{b.id}/set-slides-primary", headers=headers).status_code == 200
        db.refresh(a); db.refresh(b)
        assert a.is_slides_primary is False and b.is_slides_primary is True

    def test_requires_admin(self, client, db):
        model = make_llm(db)
        make_user(db, username="plain-slides", role="user")
        resp = client.post(f"/api/models/{model.id}/set-slides-primary",
                           headers={"Authorization": f"Bearer {login(client, 'plain-slides')}"})
        assert resp.status_code == 403

    def test_list_carries_the_flag(self, client, db):
        model = make_llm(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-slides-primary", headers=headers)
        rows = client.get("/api/models", headers=headers).json()
        assert [r["is_slides_primary"] for r in rows if r["id"] == model.id] == [True]


class TestUnsetSlidesPrimary:
    def test_unset_after_set(self, client, db):
        model = make_llm(db)
        headers = admin_headers(client, db)
        client.post(f"/api/models/{model.id}/set-slides-primary", headers=headers)
        resp = client.post(f"/api/models/{model.id}/unset-slides-primary", headers=headers)
        assert resp.status_code == 200 and resp.json()["is_slides_primary"] is False

    def test_idempotent(self, client, db):
        model = make_llm(db)
        resp = client.post(f"/api/models/{model.id}/unset-slides-primary", headers=admin_headers(client, db))
        assert resp.status_code == 200 and resp.json()["is_slides_primary"] is False


class TestGetSlidesPrimary:
    def test_requires_auth(self, client, db):
        assert client.get("/api/models/slides-primary").status_code == 401

    def test_404_when_unset(self, client, db, service_token_header):
        resp = client.get("/api/models/slides-primary", headers=service_token_header)
        assert resp.status_code == 404
        assert resp.json()["detail"] == "尚未指定主簡報模型"

    def test_409_when_disabled(self, client, db, service_token_header):
        model = make_llm(db)
        client.post(f"/api/models/{model.id}/set-slides-primary", headers=admin_headers(client, db))
        model.is_active = False; db.commit()
        assert client.get("/api/models/slides-primary", headers=service_token_header).status_code == 409

    def test_service_token_gets_name_and_nothing_secret(self, client, db, service_token_header):
        model = make_llm(db)
        client.post(f"/api/models/{model.id}/set-slides-primary", headers=admin_headers(client, db))
        resp = client.get("/api/models/slides-primary", headers=service_token_header)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "gemma26-nothink"
        assert set(body.keys()) == {"id", "name", "display_name", "model_type", "endpoint_url", "api_version", "health_status"}
        assert "api_key" not in resp.text and "secret" not in resp.text
