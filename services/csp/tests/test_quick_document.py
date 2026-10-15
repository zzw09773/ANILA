"""Document delegation uses synthetic JWTs, isolated DB, and HTTP substitutes."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
import httpx
from jose import jwt
import pytest

from app.models.artifact import Artifact, ExportRecord
from app.models.quick_builder import QuickRun
from app.models.registered_service import RegisteredService
from app.models.task import Task
from app.services import quick_document as documents
from tests.test_quick_builder_review import review_rows as _review_rows, _assistant

review_rows = _review_rows


@pytest.fixture
def render_setup(db, review_rows, monkeypatch):
    from app.services import jwt_keyring, quick_mcp

    _, user, _, _, _ = review_rows
    _, version, _ = _assistant(db, review_rows)
    run = QuickRun(version_id=version.id, user_id=user.id, status="running", result={})
    studio = RegisteredService(
        slug="studio",
        name="合成 Studio",
        entry_url="https://studio.internal",
        is_active=True,
        is_public=True,
        required_roles=[],
        classification_ceiling="營業秘密",
    )
    db.add_all([run, studio])
    db.commit()
    monkeypatch.setattr(quick_mcp, "_guard_endpoint", lambda _url: None)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    pub = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    monkeypatch.setattr(
        jwt_keyring,
        "active_signing_material",
        lambda db=None: SimpleNamespace(kid="synthetic-document", private_pem=priv),
    )
    monkeypatch.setattr(
        jwt_keyring,
        "verification_material",
        lambda kid, db=None: jwt_keyring.VerificationMaterial(
            public_pem=pub, state="active", retiring_at=None, accept_missing_iat=False
        ),
    )
    return user, run, studio, pub


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def transport(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        documents.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


def render(db, setup, *, before_send=None):
    user, run, _, _ = setup
    return asyncio.run(
        documents.render_document(
            db,
            user,
            studio_url="https://studio.internal",
            run_id=run.id,
            text="合成公文內容",
            preset="key_summary",
            output_format="docx",
            title="合成公文",
            classification_level="無機密",
            before_send=before_send or (lambda: (user, "無機密")),
        )
    )


def test_scoped_delegation_returns_bytes_and_existing_artifact_export(
    db, render_setup, monkeypatch
):
    user, run, _, pub = render_setup
    content = b"PK-synthetic-docx"

    async def handler(request):
        payload = json.loads(request.content)
        claims = jwt.decode(
            request.headers["authorization"].split()[1],
            pub,
            algorithms=["RS256"],
            audience="studio-quick-document",
            issuer="anila-csp",
        )
        assert claims["type"] == "studio_quick_document"
        assert claims["user_id"] == user.id
        assert claims["purpose"] == "quick_document"
        assert claims["job_id"] == payload["job_id"]
        assert claims["request_sha256"] == documents._request_hash(payload)
        assert claims["exp"] - claims["iat"] == 120
        assert request.headers["accept-encoding"] == "identity"
        assert str(request.url) == "https://studio.internal/api/quick-documents/render"
        return httpx.Response(
            200,
            headers={
                "Content-Type": documents.MEDIA_TYPES["docx"],
                "X-ANILA-Job-Id": payload["job_id"],
                "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
            stream=BytesStream(content),
        )

    transport(monkeypatch, handler)
    rendered = render(db, render_setup)
    assert rendered["content"] == content
    metadata = documents.register_document_artifact(
        db, user, run_id=run.id, rendered=rendered, classification_level="營業秘密"
    )
    artifact = db.get(Artifact, metadata["artifact_id"])
    assert artifact.owner_user_id == user.id
    assert artifact.classification_level == "營業秘密"
    assert db.get(ExportRecord, metadata["export_id"]).decision == "allow"
    assert metadata["artifact_version_id"] and metadata["task_id"]


def test_render_credential_cannot_be_used_for_general_csp_or_studio_jobs(
    db, render_setup
):
    from app.services.auth_service import resolve_presented_user
    from app.services.studio_job_token import verify_studio_job_token
    from app.utils.security import _claims_if_issuance_allowed

    user, _, _, _ = render_setup
    token = documents._render_token(
        db,
        user,
        {"job_id": "quick-synthetic", "text": "合成資料"},
        documents._level("無機密"),
    )
    # Genuine signature/keyring verification succeeds for its dedicated audience.
    claims = _claims_if_issuance_allowed(
        token, db, audience="studio-quick-document", issuer="anila-csp"
    )
    assert claims["type"] == "studio_quick_document"
    assert verify_studio_job_token(token, db) is None
    with pytest.raises(HTTPException) as caught:
        resolve_presented_user(token, db)
    assert caught.value.status_code == 401


@pytest.mark.parametrize(
    "failure", ["redirect", "gzip", "wrong_job", "wrong_hash", "too_large"]
)
def test_renderer_responses_cannot_redirect_decompress_or_exceed_limits(
    db, render_setup, monkeypatch, failure
):
    async def handler(request):
        payload = json.loads(request.content)
        content = b"synthetic"
        headers = {
            "Content-Type": documents.MEDIA_TYPES["docx"],
            "X-ANILA-Job-Id": payload["job_id"],
            "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        }
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://outside.invalid"})
        if failure == "gzip":
            headers["Content-Encoding"] = "gzip"
        if failure == "wrong_job":
            headers["X-ANILA-Job-Id"] = "foreign-job"
        if failure == "wrong_hash":
            headers["X-Content-SHA256"] = "0" * 64
        if failure == "too_large":
            headers["Content-Length"] = str(documents.MAX_DOCUMENT_BYTES + 1)
        return httpx.Response(200, headers=headers, stream=BytesStream(content))

    transport(monkeypatch, handler)
    with pytest.raises(HTTPException) as caught:
        render(db, render_setup)
    assert caught.value.status_code == 502


def test_registered_service_access_ceiling_and_post_await_revocation(
    db, render_setup, monkeypatch
):
    user, _, studio, _ = render_setup
    studio.is_public = False
    db.commit()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "無機密"))
    assert caught.value.status_code == 403
    studio.is_public = True
    db.commit()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(documents.resolve_studio_url(db, user, "機密"))
    assert caught.value.status_code == 403

    async def handler(request):
        payload = json.loads(request.content)
        studio.is_active = False
        db.commit()
        content = b"synthetic"
        return httpx.Response(
            200,
            headers={
                "Content-Type": documents.MEDIA_TYPES["docx"],
                "X-ANILA-Job-Id": payload["job_id"],
                "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
            },
            stream=BytesStream(content),
        )

    transport(monkeypatch, handler)
    with pytest.raises(HTTPException) as caught:
        render(db, render_setup)
    assert caught.value.detail["type"] == "DOCUMENT_RENDERER_NOT_CONFIGURED"


@pytest.mark.parametrize(
    "change", ["classification", "service_ceiling", "grant", "endpoint"]
)
def test_last_pre_send_check_blocks_changes_during_endpoint_validation(
    db, render_setup, monkeypatch, change
):
    user, _, studio, _ = render_setup
    calls = []

    def before_send():
        if change == "classification":
            return user, "機密"
        if change == "service_ceiling":
            studio.classification_ceiling = "無機密"
            db.commit()
            return user, "營業秘密"
        if change == "grant":
            raise HTTPException(403, detail={"type": "AGENT_GRANT_REVOKED"})
        studio.entry_url = "https://changed.internal"
        db.commit()
        return user, "無機密"

    async def handler(request):
        calls.append(request)
        raise AssertionError("preflight must block before any bytes are sent")

    transport(monkeypatch, handler)
    with pytest.raises(HTTPException) as caught:
        render(db, render_setup, before_send=before_send)
    assert caught.value.status_code in (403, 409)
    assert calls == []


def test_foreign_task_or_classification_upgrade_cannot_register_export(
    db, render_setup, review_rows
):
    owner, user, _, _, _ = review_rows
    _, run, _, _ = render_setup
    task = Task(title="他人的合成任務", task_type="draft", requester_user_id=owner.id)
    db.add(task)
    db.commit()
    data = b"synthetic"
    rendered = {
        "content": data,
        "content_hash": hashlib.sha256(data).hexdigest(),
        "title": "草稿",
        "preset": "key_summary",
        "format": "docx",
        "job_id": "quick-synthetic",
    }
    with pytest.raises(HTTPException) as caught:
        documents.register_document_artifact(
            db,
            user,
            run_id=run.id,
            rendered=rendered,
            classification_level="無機密",
            task_id=task.id,
        )
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        documents.register_document_artifact(
            db, user, run_id=run.id, rendered=rendered, classification_level="機密"
        )
    assert caught.value.detail["type"] == "EXPORT_CLASSIFICATION"
