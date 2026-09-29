"""撤銷清單冷啟動同步要帶 CSP 核發的專屬憑證檔，不是舊的 CSP_SERVICE_TOKEN。

舊寫法只看 settings.CSP_SERVICE_TOKEN；compose 早已不注入它，gateway 一開就
對 /api/auth/revocations 拿 401、startup 失敗、容器不停重啟。
"""
from __future__ import annotations

import httpx
import pytest

from app.services import revocation_cache as revocation_cache_mod
from app.services.revocation_cache import RevocationCache


@pytest.mark.asyncio
async def test_cold_start_sync_sends_the_token_file(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("svc-token-from-file\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(revocation_cache_mod.settings, "CSP_SERVICE_TOKEN", "")

    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("X-CSP-Service-Token", "")
        return httpx.Response(200, json={"revocations": [], "revoked_kids": []})

    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(revocation_cache_mod.httpx, "AsyncClient", client_factory)

    await RevocationCache()._cold_start_sync()

    assert seen["token"] == "svc-token-from-file"
