"""Studio UI entries resolve to a fixed API on a guarded intranet origin."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re

from fastapi import HTTPException
import httpx
import pytest

from app.config import settings
from app.services import quick_document as documents, quick_mcp
from tests.test_quick_document import (
    BytesStream,
    render_setup as _render_setup,
    review_rows as _review_rows,
    transport,
)

render_setup = _render_setup
review_rows = _review_rows


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("https://studio.internal", "https://studio.internal"),
        ("https://studio.internal:8443/studio/reports", "https://studio.internal:8443"),
        ("/studio", "https://platform.internal:8443"),
        ("/studio/reports/", "https://platform.internal:8443"),
    ],
)
def test_registered_ui_entries_render_at_fixed_root_api(
    db, render_setup, monkeypatch, entry, expected
):
    user, run, service, _ = render_setup
    service.entry_url = entry
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "https://platform.internal:8443")
    guarded = []
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", guarded.append)
    content = b"synthetic-docx"
    sent = []

    async def handler(request):
        sent.append(str(request.url))
        body = json.loads(request.content)
        return httpx.Response(
            200,
            headers={
                "Content-Type": documents.MEDIA_TYPES["docx"],
                "X-ANILA-Job-Id": body["job_id"],
                "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
            stream=BytesStream(content),
        )

    transport(monkeypatch, handler)
    origin = asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    result = asyncio.run(
        documents.render_document(
            db, user, studio_url=origin, run_id=run.id, text="合成內容",
            preset="key_summary", output_format="docx", classification_level="無機密",
            before_send=lambda: (user, "無機密"),
        )
    )
    assert origin == expected
    assert guarded and set(guarded) == {expected}
    assert sent == [expected + "/api/quick-documents/render"]
    assert result["content"] == content


@pytest.mark.parametrize(
    "entry",
    [
        "//outside.invalid/studio", "/\\outside.invalid/studio", "studio/reports",
        "/other/studio", "/studio/../other", "/studio/%2e%2e/other",
        "/studio?target=https://outside.invalid", "/studio#outside",
        "/studio/\t/reports", "https://user:password@studio.internal/studio",
        "https://studio.internal/studio?next=https://outside.invalid",
        "https://studio.internal/studio#fragment", "https://studio.internal\\@outside.invalid",
        "https://studio.internal:invalid", "file:///studio", " https://studio.internal",
    ],
)
def test_ambiguous_or_untrusted_entry_shapes_fail_before_dns(
    db, render_setup, monkeypatch, entry
):
    user, _, service, _ = render_setup
    service.entry_url = entry
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "https://platform.internal")
    guarded = []
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", guarded.append)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.status_code == 503
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_ENDPOINT"
    assert guarded == []


@pytest.mark.parametrize(
    "site", ["//outside.invalid", "https://user:password@platform.internal", "https://platform.internal/base", "https://platform.internal?next=outside", "https://platform.internal#outside"]
)
def test_relative_entry_requires_server_configured_plain_origin(
    db, render_setup, monkeypatch, site
):
    user, _, service, _ = render_setup
    service.entry_url = "/studio/reports"
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", site)
    guarded = []
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", guarded.append)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_ENDPOINT"
    assert guarded == []


@pytest.mark.parametrize("entry", ["https://studio.internal/studio", "/studio/reports"])
@pytest.mark.parametrize("address", ["8.8.8.8", "127.0.0.1", "169.254.169.254", "::1"])
def test_both_entry_forms_keep_real_guard_private_address_restriction(
    db, render_setup, monkeypatch, entry, address
):
    user, _, service, _ = render_setup
    service.entry_url = entry
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "https://platform.internal")
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", _REAL_GUARD)
    monkeypatch.setattr(quick_mcp, "get_cached_hosts", lambda: {"studio.internal", "platform.internal"})
    monkeypatch.setattr(quick_mcp, "_env_trusted_hosts", lambda: set())
    # No network is used; the real MCP guard still applies its stricter
    # RFC1918/ULA rule to these deterministic DNS answers.
    monkeypatch.setattr(quick_mcp, "validate_outbound_url", lambda *a, **kw: None)
    monkeypatch.setattr(quick_mcp, "_resolve_host", lambda *a: [(None, None, None, None, (address, 443))])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_ENDPOINT"


_REAL_GUARD = quick_mcp._guard_endpoint


def test_relative_entry_platform_host_still_requires_explicit_trust(
    db, render_setup, monkeypatch
):
    user, _, service, _ = render_setup
    service.entry_url = "/studio/reports"
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "https://platform.internal")
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", _REAL_GUARD)
    monkeypatch.setattr(quick_mcp, "get_cached_hosts", lambda: set())
    monkeypatch.setattr(quick_mcp, "_env_trusted_hosts", lambda: set())
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_ENDPOINT"


@pytest.mark.parametrize("entry", ["http://platform.internal/studio", "/studio/reports"])
def test_entry_resolution_does_not_bypass_existing_http_policy(
    db, render_setup, monkeypatch, entry
):
    from anila_core.security import validate_outbound_url

    user, _, service, _ = render_setup
    service.entry_url = entry
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "http://platform.internal")
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", _REAL_GUARD)
    monkeypatch.setattr(quick_mcp, "get_cached_hosts", lambda: {"platform.internal"})
    monkeypatch.setattr(quick_mcp, "_env_trusted_hosts", lambda: set())
    monkeypatch.setattr(quick_mcp, "validate_outbound_url", validate_outbound_url)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "0")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_ENDPOINT"


@pytest.mark.parametrize("addresses", [["10.20.30.40", "8.8.8.8"], ["fc00::12", "::1"]])
def test_relative_entry_rejects_a_single_unsafe_dns_answer(
    db, render_setup, monkeypatch, addresses
):
    user, _, service, _ = render_setup
    service.entry_url = "/studio/reports"
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "https://platform.internal")
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", _REAL_GUARD)
    monkeypatch.setattr(quick_mcp, "get_cached_hosts", lambda: {"platform.internal"})
    monkeypatch.setattr(quick_mcp, "_env_trusted_hosts", lambda: set())
    monkeypatch.setattr(quick_mcp, "validate_outbound_url", lambda *a, **kw: None)
    monkeypatch.setattr(quick_mcp, "_resolve_host", lambda *a: [(None, None, None, None, (address, 443)) for address in addresses])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_ENDPOINT"


def test_platform_origin_change_during_last_preflight_sends_nothing(
    db, render_setup, monkeypatch
):
    user, run, service, _ = render_setup
    service.entry_url = "/studio/reports"
    db.commit()
    monkeypatch.setattr(settings, "SITE_URL", "https://platform.internal")
    sent = []

    def before_send():
        monkeypatch.setattr(settings, "SITE_URL", "https://changed.internal")
        return user, "無機密"

    async def handler(request):
        sent.append(request)
        raise AssertionError("changed origin must not receive text or credentials")

    transport(monkeypatch, handler)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.render_document(
            db, user, studio_url="https://platform.internal", run_id=run.id,
            text="合成內容", preset="key_summary", output_format="docx",
            classification_level="無機密", before_send=before_send,
        ))
    assert caught.value.status_code == 409
    assert sent == []


def test_both_ingress_blocks_route_fixed_renderer_api_to_studio():
    config = Path(__file__).parents[3] / "infra/nginx/anila.conf"
    blocks = re.findall(r"location ~ (\^/api/[^\s]+) \{(.*?)\n    \}", config.read_text(), re.S)
    matches = [(pattern, body) for pattern, body in blocks if re.search(pattern, "/api/quick-documents/render")]
    assert len(matches) == 2
    for pattern, body in matches:
        assert "proxy_pass http://anila_studio_backend;" in body
        assert not re.search(pattern, "/api/quick-documents-other/render")
        assert not re.search(pattern, "/api/quick-builder/runs/1")
