# -*- coding: utf-8 -*-
"""主圖像旗標已不是生圖設定來源。路由與回應欄位都要消失。"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.model_registry import ModelRegistry
from tests.conftest import login, make_user


def make_image_model(db, name="flux-cloud", is_active=True) -> ModelRegistry:
    m = ModelRegistry(
        name=name,
        display_name=name,
        model_type="image",
        endpoint_url="https://images.example.com/v1",
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


def test_image_primary_routes_are_not_registered():
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/models/image-primary" not in paths
    assert not any(path.endswith("/set-image-primary") for path in paths)
    assert not any(path.endswith("/unset-image-primary") for path in paths)


def test_model_response_omits_image_primary_flag(client, db):
    model = make_image_model(db)
    headers = admin_headers(client, db, username="admin-no-flag")
    resp = client.get(f"/api/models/{model.id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "is_image_primary" not in resp.json()
    assert "is_image_primary" not in ModelRegistry.__table__.columns
