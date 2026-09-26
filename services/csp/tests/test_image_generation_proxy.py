"""生圖走 CSP 代理，不把模型主機位址交給呼叫端。

角色沒設、或健康狀態不是 healthy，代理不打上游。
呼叫端用自己的憑證（與簡報、視覺角色相同），金鑰留在 CSP。
"""
from __future__ import annotations

import json

from app.models.model_registry import ModelRegistry
from app.models.model_role import ModelRole
from app.services import proxy_service
from tests.conftest import login, make_user

_PNG_B64 = "iVBORw0KGgo="


class _Captured:
    def __init__(self) -> None:
        self.calls: list[dict] = []


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}
    text = json.dumps({"data": [{"b64_json": _PNG_B64}]})

    def json(self):
        return json.loads(self.text)


def _client_factory(captured: _Captured):
    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured.calls.append({"url": url, "json": json, "headers": headers or {}})
            return _Response()

    return _Client


def _image_model(db, *, health: str = "healthy") -> ModelRegistry:
    row = ModelRegistry(
        name="painter",
        display_name="Painter",
        model_type="image",
        endpoint_url="https://images.example.test/v1",
        api_version="v1",
        is_active=True,
        health_status=health,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _admin(client, db) -> dict[str, str]:
    make_user(db, username="image-proxy-admin", role="admin")
    return {"Authorization": f"Bearer {login(client, 'image-proxy-admin')}"}


def _user_headers(client, db) -> dict[str, str]:
    make_user(db, username="image-proxy-user", role="user")
    return {"Authorization": f"Bearer {login(client, 'image-proxy-user')}"}


def _assign(client, db, model_id: int) -> None:
    headers = _admin(client, db)
    assigned = client.put(
        "/api/models/roles/image_generation",
        json={"model_id": model_id},
        headers=headers,
    )
    assert assigned.status_code == 200, assigned.text
    granted = client.post(
        "/api/models/roles/image_generation/grant-all-users",
        headers=headers,
    )
    assert granted.status_code == 200, granted.text


def test_unset_image_role_does_not_call_a_model_host(client, db, monkeypatch):
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={"model": "https://evil.example/v1", "prompt": "a tank"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert "生圖模型" in resp.json()["detail"]
    assert captured.calls == []
    assert db.query(ModelRole).count() == 0


def test_unhealthy_image_role_does_not_call_a_model_host(client, db, monkeypatch):
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="unhealthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "a tank"},
        headers=headers,
    )
    assert resp.status_code == 409
    assert "生圖模型" in resp.json()["detail"]
    assert captured.calls == []


def test_image_role_without_grant_does_not_call_a_model_host(client, db, monkeypatch):
    """沒有全院授權時，一般使用者的憑證不能打這顆生圖模型。"""
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="healthy")
    headers = _admin(client, db)
    assigned = client.put(
        "/api/models/roles/image_generation",
        json={"model_id": model.id},
        headers=headers,
    )
    assert assigned.status_code == 200, assigned.text
    user = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "a tank"},
        headers=user,
    )
    assert resp.status_code == 403
    assert captured.calls == []


def test_healthy_image_role_is_proxied_through_csp(client, db, monkeypatch):
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="healthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={
            "model": "https://evil.example/generate",
            "prompt": "a tank on a ridge",
            "size": "1792x1024",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["b64_json"] == _PNG_B64
    assert len(captured.calls) == 1
    call = captured.calls[0]
    assert call["url"] == "https://images.example.test/v1/images/generations"
    assert "evil.example" not in call["url"]
    assert call["json"]["model"] == "painter"
    assert call["json"]["prompt"] == "a tank on a ridge"
    assert call["json"]["response_format"] == "b64_json"
    # 使用者憑證停在 CSP；上游不該看到呼叫端帶來的 Bearer。
    auth = call["headers"].get("Authorization") or ""
    assert headers["Authorization"] not in auth


def test_image_size_is_a_whitelist_with_a_pixel_cap():
    """超大或未列在白名單的尺寸不能送上游。"""
    from app.api.proxy import image_size_allowed

    assert image_size_allowed("1792x1024")
    assert image_size_allowed("1024x1024")
    assert not image_size_allowed("99999x99999")
    assert not image_size_allowed("100x100")
    assert not image_size_allowed("2048x2048")


def test_pixel_cap_rejects_a_whitelisted_shape_that_is_too_large(monkeypatch):
    import app.api.proxy as proxy

    monkeypatch.setattr(
        proxy, "_IMAGE_SIZES", frozenset({"4000x4000", "1024x1024"})
    )
    monkeypatch.setattr(proxy, "_IMAGE_PIXEL_CAP", 1792 * 1024)
    assert proxy.image_size_allowed("1024x1024")
    assert not proxy.image_size_allowed("4000x4000")


def test_oversized_image_size_does_not_call_upstream(client, db, monkeypatch):
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="healthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "a tank on a ridge", "size": "99999x99999"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert captured.calls == []


def test_raw_knowledge_prompt_is_refused_before_upstream(client, db, monkeypatch):
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="healthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    leaked = "來源：規章.pdf chunk leaf-00002 第 3 條 " + ("申訴期限內提出。" * 20)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": leaked, "size": "1024x1024"},
        headers=headers,
    )
    assert resp.status_code == 400
    assert captured.calls == []


def test_internal_details_are_stripped_before_the_image_model(client, db, monkeypatch):
    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="healthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={
            "prompt": (
                "wide shot of a ridge at dusk "
                "https://csp.internal/secrets ANILA_DB_PASSWORD"
            ),
            "size": "1024x1024",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(captured.calls) == 1
    forwarded = captured.calls[0]["json"]["prompt"]
    assert "ridge" in forwarded
    assert "https://" not in forwarded
    assert "ANILA_DB_PASSWORD" not in forwarded
    assert "csp.internal" not in forwarded


def test_image_call_refuses_when_task_exceeds_model_ceiling(client, db, monkeypatch):
    from app.models.task import Task
    from app.models.user import User

    captured = _Captured()
    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _client_factory(captured))
    model = _image_model(db, health="healthy")
    model.classification_ceiling = "無機密"
    db.commit()
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    user = db.query(User).filter(User.username == "image-proxy-user").one()
    task = Task(
        title="簡報",
        task_type="generate_artifact",
        requester_user_id=user.id,
        classification_level="機密",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "a tank on a ridge", "size": "1024x1024"},
        headers={**headers, "X-ANILA-Task-Id": str(task.id)},
    )
    assert resp.status_code == 403, resp.text
    assert captured.calls == []


def test_upstream_image_body_is_capped_before_parse(client, db, monkeypatch):
    parsed = {"n": 0}

    class _Big(_Response):
        text = json.dumps({"data": [{"b64_json": _PNG_B64}]}) + (" " * 400)

        def json(self):
            parsed["n"] += 1
            return {"data": [{"b64_json": _PNG_B64}]}

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _Big()

    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _Client)
    monkeypatch.setattr("app.api.proxy.IMAGE_UPSTREAM_MAX_BYTES", 80)
    model = _image_model(db, health="healthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "a tank on a ridge", "size": "1024x1024"},
        headers=headers,
    )
    assert resp.status_code == 502, resp.text
    assert parsed["n"] == 0


def test_non_image_payload_is_rejected(client, db, monkeypatch):
    import base64

    class _Junk(_Response):
        text = json.dumps(
            {"data": [{"b64_json": base64.b64encode(b"NOT-AN-IMAGE").decode("ascii")}]}
        )

        def json(self):
            return json.loads(self.text)

    captured = _Captured()

    class _Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            captured.calls.append({"url": url})
            return _Junk()

    monkeypatch.setattr(proxy_service.httpx, "AsyncClient", _Client)
    model = _image_model(db, health="healthy")
    _assign(client, db, model.id)
    headers = _user_headers(client, db)
    resp = client.post(
        "/v1/images/generations",
        json={"prompt": "a tank on a ridge", "size": "1024x1024"},
        headers=headers,
    )
    assert resp.status_code == 502, resp.text
    body = resp.text
    assert "NOT-AN-IMAGE" not in body
