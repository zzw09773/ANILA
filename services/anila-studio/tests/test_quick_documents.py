"""Pure render tests with synthetic JWTs and renderer substitutes; no services."""

from datetime import datetime, timezone
import hashlib
import json

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, HTTPException
import httpx
from jose import jwt
from pydantic import ValidationError
import pytest

from app import auth
from app.api.quick_documents import require_render_identity, render_quick_document
from app.api import quick_documents
from app.schemas.quick_document import RenderQuickDocument
from app.services import quick_document_render as renderer


def request(**overrides):
    return RenderQuickDocument(
        **{
            "job_id": "quick-3-synthetic",
            "preset": "key_summary",
            "format": "html",
            "title": "合成公文",
            "text": "合成通知內容",
            **overrides,
        }
    )


@pytest.fixture
def signer(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )

    async def public(_kid):
        return pub

    async def revocation(*args, **kwargs):
        pass

    monkeypatch.setattr(auth.jwks_client, "get_public_key", public)
    monkeypatch.setattr(auth.jwks_client, "cached_key_allows", lambda *_: True)
    monkeypatch.setattr(auth, "_check_revocation", revocation)

    def token(payload, **overrides):
        now = int(datetime.now(timezone.utc).timestamp())
        digest = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        claims = {
            "iss": "anila-csp",
            "aud": "studio-quick-document",
            "type": "studio_quick_document",
            "purpose": "quick_document",
            "sub": "7",
            "user_id": 7,
            "tv": 0,
            "job_id": payload.job_id,
            "classification": "無機密",
            "iat": now,
            "exp": now + 120,
            "request_sha256": digest,
        }
        claims.update(overrides)
        return jwt.encode(
            claims, priv, algorithm="RS256", headers={"kid": "synthetic-render"}
        )

    return token


async def test_signed_job_renders_existing_template_without_interpreting_data(signer):
    payload = request()
    payload.text = (
        "<script>alert(1)</script>\n![外網](https://outside.invalid/x)\n{{7*7}}"
    )
    identity = await require_render_identity("Bearer " + signer(payload))
    response = await render_quick_document(payload, identity)
    assert response.status_code == 200
    assert b"preset-key-summary" in response.body
    assert b"<script>" not in response.body and b"<img" not in response.body
    assert b"{{7*7}}" in response.body
    assert response.headers["x-anila-job-id"] == payload.job_id
    assert (
        hashlib.sha256(response.body).hexdigest()
        == response.headers["x-content-sha256"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "ordinary_studio_job"},
        {"aud": "anila-agent"},
        {"sub": "8"},
        {"user_id": True},
        {"exp": 1},
        {"request_sha256": "bad"},
        {"type": "studio_job", "aud": "studio"},
    ],
)
async def test_wrong_expired_or_unscoped_token_fails_with_typed_error(signer, changes):
    payload = request()
    with pytest.raises(HTTPException) as caught:
        await require_render_identity("Bearer " + signer(payload, **changes))
    assert caught.value.status_code == 401
    assert caught.value.detail["type"] == "DOCUMENT_IDENTITY_INVALID"


async def test_render_credential_is_not_a_general_studio_login(signer):
    with pytest.raises(HTTPException) as caught:
        await auth.get_current_user_identity(
            authorization="Bearer " + signer(request()), anila_access_token=None
        )
    assert caught.value.status_code == 401


async def test_expiry_and_future_issue_time_are_checked_locally(signer, monkeypatch):
    now = int(datetime.now(timezone.utc).timestamp())

    # Simulate a shared verifier's clock tolerance without weakening this route.
    async def tolerated(token):
        return jwt.get_unverified_claims(token)

    monkeypatch.setattr(auth, "_verify_jwt", tolerated)
    for dates in (
        {"iat": now - 121, "exp": now - 1},
        {"iat": now + 6, "exp": now + 120},
    ):
        with pytest.raises(HTTPException) as caught:
            await require_render_identity("Bearer " + signer(request(), **dates))
        assert caught.value.status_code == 401


@pytest.mark.parametrize(
    "changes", [{"text": None}, {"format": "exe"}, {"template": "機敏錯誤輸入"}]
)
async def test_route_input_errors_are_typed_and_never_echo_document_text(
    signer, changes
):
    app = FastAPI()
    app.include_router(quick_documents.router)
    payload = request(text="機敏文件內容不可回顯")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://local.test"
    ) as client:
        response = await client.post(
            "/api/quick-documents/render",
            json={**payload.model_dump(mode="json"), **changes},
            headers={"Authorization": "Bearer " + signer(payload)},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["type"] == "DOCUMENT_INPUT"
    assert "機敏" not in response.text


async def test_malformed_bearer_and_classified_token_are_denied(signer):
    for header in (None, "Bearer ", "Basic abc"):
        with pytest.raises(HTTPException) as caught:
            await require_render_identity(header)
        assert caught.value.status_code == 401
    with pytest.raises(HTTPException) as caught:
        await require_render_identity(
            "Bearer " + signer(request(), classification="機密")
        )
    assert caught.value.status_code == 403


async def test_job_or_content_cannot_be_changed_after_signing(signer):
    payload = request()
    identity = await require_render_identity("Bearer " + signer(payload))
    for changes in (
        {"text": "修改內容"},
        {"preset": "external_comms"},
        {"job_id": "quick-elsewhere"},
    ):
        changed = payload.model_copy(update=changes)
        with pytest.raises(HTTPException) as caught:
            await render_quick_document(changed, identity)
        assert caught.value.status_code == 403
        assert caught.value.detail["type"].startswith("DOCUMENT_")


@pytest.mark.parametrize(
    "preset", ["deep_tech_review", "key_summary", "teaching_handout", "external_comms"]
)
@pytest.mark.parametrize("format", ["html", "pdf", "docx"])
async def test_fixed_presets_and_formats_use_existing_renderers(
    monkeypatch, preset, format
):
    calls = []

    async def pdf(html, path, *, block_network=False):
        calls.append(("pdf", block_network))
        path.write_bytes(b"%PDF-synthetic")

    async def docx(html, path):
        calls.append(("docx", True))
        path.write_bytes(b"PK-synthetic")

    monkeypatch.setattr(renderer.report_renderer, "write_pdf_file", pdf)
    monkeypatch.setattr(renderer.report_renderer, "write_docx_file", docx)
    payload = request().model_copy(update={"preset": preset, "format": format})
    content = await renderer.render_document_bytes(payload)
    assert content
    if format != "html":
        assert calls == [(format, True)]


def test_template_paths_and_arbitrary_code_fields_are_not_supported():
    data = request().model_dump(mode="json")
    for changes in (
        {"preset": "../../outside"},
        {"template": "{{ arbitrary_code() }}"},
        {"format": "exe"},
    ):
        with pytest.raises(ValidationError):
            RenderQuickDocument(**{**data, **changes})
