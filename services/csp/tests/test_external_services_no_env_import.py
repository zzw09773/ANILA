"""文件解析與語音位址只走治理中心。開機不再匯入 DOC_PARSER / ASR_DECODE_*。"""
from __future__ import annotations

import inspect

from app.main import lifespan
from tests.conftest import login, make_user

PARSER_URL = "https://docling.example.test:9100"


def test_lifespan_does_not_import_legacy_external_service_env():
    source = inspect.getsource(lifespan)
    assert "import_legacy_env_once" not in source


def test_console_save_works_while_legacy_env_is_set(client, db, monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", "https://from-env.example.test")
    monkeypatch.setenv("DOCLING_SERVICE_TOKEN", "env-token-must-not-win")
    monkeypatch.setenv("ASR_DECODE_URL", "https://asr-from-env.example.test")
    monkeypatch.setenv("ASR_DECODER_TOKEN", "asr-env-token")
    monkeypatch.setenv("ASR_OPENAI_MODEL", "env-whisper")
    make_user(db, username="ext-console-admin", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'ext-console-admin')}"}
    saved = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL, "credential": "console-secret"},
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["base_url"] == PARSER_URL
    assert "from-env" not in saved.text
    assert "env-token" not in saved.text
    assert "console-secret" not in saved.text
