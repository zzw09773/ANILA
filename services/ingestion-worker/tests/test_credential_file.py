"""ingestion-worker 的 sk- 只從 CSP 寫的憑證檔來。

路徑有設時，不採用 EMBEDDING_API_KEY／VISION_API_KEY／RELATION_LLM_API_KEY。
檔案不在或讀不到就失敗即關閉。檔案變了要重讀；上游 401／403 再讀一次。
健康輸出只報來源，不報明文。
"""
from __future__ import annotations

import httpx
import pytest
import respx

from ingestion_worker.embedder import Embedder
from ingestion_worker.settings import WorkerSettings


def _settings(**overrides) -> WorkerSettings:
    base = {
        "embedding_base_url": "http://embed.test/v1",
        "embedding_model": "test-model",
        "embedding_api_key": "sk-from-env",
        "vision_api_key": "sk-vision-env",
        "relation_llm_api_key": "sk-relation-env",
        "embedding_dim": 8,
        "embedding_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return WorkerSettings(_env_file=None, **base)


def _load():
    from ingestion_worker import credential_file

    credential_file.reload(force=True)
    return credential_file


def test_configured_file_replaces_every_env_key(tmp_path, monkeypatch):
    secret = "sk-from-file"
    path = tmp_path / "ingestion-worker.token"
    path.write_text(secret + "\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    cred = _load()
    settings = _settings()
    assert cred.api_key(settings.embedding_api_key) == secret
    assert cred.api_key(settings.vision_api_key) == secret
    assert cred.api_key(settings.relation_llm_api_key) == secret
    health = cred.credential_health()
    assert health == {"token_source": "file"}
    assert secret not in str(health)


def test_missing_file_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(tmp_path / "missing.token"))
    cred = _load()
    settings = _settings()
    assert cred.source() == "file_missing"
    assert cred.api_key(settings.embedding_api_key) == ""
    assert cred.api_key(settings.vision_api_key) == ""
    assert "sk-from-env" not in str(cred.credential_health())


def test_unreadable_file_does_not_fall_back(tmp_path, monkeypatch):
    path = tmp_path / "ingestion-worker.token"
    path.write_text("sk-hidden\n", encoding="utf-8")
    path.chmod(0)
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    try:
        cred = _load()
        assert cred.source() == "file_error"
        assert cred.api_key("sk-from-env") == ""
    finally:
        path.chmod(0o600)


def test_reread_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "ingestion-worker.token"
    path.write_text("sk-first\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    cred = _load()
    assert cred.api_key("sk-from-env") == "sk-first"
    path.write_text("sk-second\n", encoding="utf-8")
    assert cred.api_key("sk-from-env") == "sk-second"
    assert cred.source() == "file"


@pytest.mark.asyncio
@respx.mock
async def test_embedder_rereads_once_after_401(tmp_path, monkeypatch):
    path = tmp_path / "ingestion-worker.token"
    path.write_text("sk-stale\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(path))
    _load()
    settings = _settings(embedding_dim=4)
    embedder = Embedder(settings)
    url = "http://embed.test/v1/embeddings"
    calls = {"n": 0}

    def respond(request):
        calls["n"] += 1
        if calls["n"] == 1:
            path.write_text("sk-fresh\n", encoding="utf-8")
            return httpx.Response(401, json={"detail": "stale"})
        assert request.headers["authorization"] == "Bearer sk-fresh"
        return httpx.Response(
            200,
            json={"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]},
        )

    try:
        respx.post(url).mock(side_effect=respond)
        vectors = await embedder.embed(["hello"])
    finally:
        await embedder.close()
    assert calls["n"] == 2
    assert vectors == [[0.1, 0.2, 0.3, 0.4]]
