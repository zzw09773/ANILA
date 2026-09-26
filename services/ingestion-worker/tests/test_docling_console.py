"""文件解析位址來自治理中心。沒設定才用原生解析器。"""
from __future__ import annotations

import httpx
import pytest

from anila_core.ingestion.docling_source import (
    DoclingEndpoint,
    register_docling_source,
    reset_docling_source,
)
from anila_core.ingestion.errors import ParseError, RemoteParseError
from anila_core.ingestion.parser_registry import ParserRegistry
from anila_core.ingestion.parsers import extract_text
from ingestion_worker.docling_source import refresh_document_parser, reset_for_tests

PARSER_URL = "https://docling.example.test:9100"


class _Response:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _AsyncClient:
    seen: list[tuple] = []
    body: dict = {}
    status_code = 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        type(self).seen.append((url, headers or {}))
        return _Response(type(self).status_code, type(self).body)


@pytest.fixture(autouse=True)
def _isolated_source():
    reset_for_tests()
    reset_docling_source()
    ParserRegistry._reset_docling_cache()
    yield
    reset_for_tests()
    reset_docling_source()
    ParserRegistry._reset_docling_cache()


def test_console_not_configured_uses_native_even_if_env_says_docling(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", "https://should-not-be-used.example.test")
    register_docling_source(lambda: None)
    parser = ParserRegistry.get("notes.pdf")
    assert type(parser).__name__ == "PdfParser"


def test_console_configured_uses_docling_even_if_env_says_native(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "native")
    register_docling_source(lambda: DoclingEndpoint(PARSER_URL, "tok"))
    parser = ParserRegistry.get("notes.pdf")
    assert type(parser).__name__ == "RemoteDoclingParser"
    assert parser._base_url == PARSER_URL


def test_unread_console_does_not_fall_back_to_native(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "native")
    from anila_core.ingestion.docling_source import DoclingSourceUnavailable

    def _boom():
        raise DoclingSourceUnavailable("db down")

    register_docling_source(_boom)
    with pytest.raises(RemoteParseError) as caught:
        ParserRegistry.get("notes.pdf")
    assert caught.value.retryable is True


def test_enabled_without_url_does_not_fall_back_to_native():
    from anila_core.ingestion.docling_source import DoclingMisconfigured

    def _bad():
        raise DoclingMisconfigured("no url")

    register_docling_source(_bad)
    with pytest.raises(ParseError) as caught:
        ParserRegistry.get("notes.pdf")
    assert caught.value.code == "E_PARSE_BAD_CONFIG"
    assert caught.value.retryable is False


def test_configured_but_down_fails_instead_of_native_text(monkeypatch):
    register_docling_source(lambda: DoclingEndpoint(PARSER_URL, "tok"))

    class _Down:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("docling down")

        def close(self):
            return None

    monkeypatch.setattr(
        "anila_core.ingestion.docling_parser.httpx.Client", _Down
    )
    with pytest.raises(RemoteParseError):
        extract_text("notes.pdf", b"%PDF-1.4\n", "application/pdf")


def _arm(monkeypatch, tmp_path, body, status_code=200):
    token_file = tmp_path / "token"
    token_file.write_text("sk-worker-docling\n", encoding="utf-8")
    monkeypatch.setenv("ANILA_SERVICE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CSP_BASE_URL", "http://csp.test")
    _AsyncClient.seen = []
    _AsyncClient.body = body
    _AsyncClient.status_code = status_code
    monkeypatch.setattr(
        "ingestion_worker.docling_source.httpx.AsyncClient", _AsyncClient
    )


@pytest.mark.asyncio
async def test_worker_reads_docling_from_csp_with_its_credential_file(
    monkeypatch, tmp_path
):
    _arm(
        monkeypatch,
        tmp_path,
        {
            "enabled": True,
            "base_url": PARSER_URL,
            "credential": "worker-secret",
            "misconfigured": False,
        },
    )
    await refresh_document_parser(pool=object())
    parser = ParserRegistry.get("notes.pdf")
    assert type(parser).__name__ == "RemoteDoclingParser"
    assert parser._token == "worker-secret"
    url, headers = _AsyncClient.seen[0]
    assert url == "http://csp.test/api/internal/external-services/document_parser"
    assert headers["Authorization"] == "Bearer sk-worker-docling"


@pytest.mark.asyncio
async def test_worker_treats_disabled_payload_as_native(monkeypatch, tmp_path):
    monkeypatch.setenv("DOC_PARSER", "docling")
    _arm(
        monkeypatch,
        tmp_path,
        {"enabled": False, "base_url": PARSER_URL, "credential": "nope"},
    )
    await refresh_document_parser()
    parser = ParserRegistry.get("notes.pdf")
    assert type(parser).__name__ == "PdfParser"


@pytest.mark.asyncio
async def test_worker_enabled_without_url_fails_loudly(monkeypatch, tmp_path):
    _arm(
        monkeypatch,
        tmp_path,
        {"enabled": True, "base_url": "  ", "misconfigured": True},
    )
    await refresh_document_parser()
    with pytest.raises(ParseError) as caught:
        ParserRegistry.get("notes.pdf")
    assert caught.value.code == "E_PARSE_BAD_CONFIG"
