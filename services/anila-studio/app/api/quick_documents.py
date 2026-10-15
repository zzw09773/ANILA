"""Job-scoped internal render endpoint for quick builder. No arbitrary template code."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute
from jose import jwt

from app import auth
from app.schemas.quick_document import RenderQuickDocument
from app.services.quick_document_render import MEDIA_TYPES, render_document_bytes

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{3,80}$")


def _fail(status: int, kind: str, message: str):
    raise HTTPException(status, detail={"type": kind, "message": message})


class _DocumentRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def checked(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                # Pydantic's default errors include rejected input, which may
                # contain sensitive document text. Keep this route's error local.
                _fail(
                    422,
                    "DOCUMENT_INPUT",
                    "文件輸入不完整或格式不符，請確認範本、格式、標題與內容",
                )

        return checked


router = APIRouter(
    prefix="/api/quick-documents",
    tags=["Studio / Quick documents"],
    route_class=_DocumentRoute,
)


@dataclass(frozen=True)
class RenderIdentity:
    user_id: int
    job_id: str
    classification_level: str
    request_sha256: str


async def require_render_identity(
    authorization: str | None = Header(default=None),
) -> RenderIdentity:
    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        _fail(401, "DOCUMENT_IDENTITY_REQUIRED", "缺少文件排版工作憑證")
    token = parts[1]
    try:
        claims = await auth._verify_jwt(token)
    except HTTPException as exc:
        _fail(
            exc.status_code,
            "DOCUMENT_IDENTITY_INVALID",
            "文件排版工作憑證無效或驗證服務暫時無法使用",
        )
    user_id, tv = claims.get("user_id"), claims.get("tv")
    job_id, level = claims.get("job_id"), claims.get("classification")
    now = time.time()
    if (
        claims.get("type") != "studio_quick_document"
        or claims.get("aud") != "studio-quick-document"
        or claims.get("iss") != "anila-csp"
        or claims.get("purpose") != "quick_document"
        or not isinstance(claims.get("request_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", claims["request_sha256"]) is None
        or type(user_id) is not int
        or user_id <= 0
        or claims.get("sub") != str(user_id)
        or type(tv) is not int
        or tv < 0
        or type(claims.get("exp")) is not int
        or type(claims.get("iat")) is not int
        or not 0 < claims["exp"] - claims["iat"] <= 120
        or claims["exp"] <= now
        or claims["iat"] > now + 5
        or not isinstance(job_id, str)
        or _JOB_ID.fullmatch(job_id) is None
        or level not in ("無機密", "營業秘密", "密", "機密")
    ):
        _fail(401, "DOCUMENT_IDENTITY_INVALID", "文件排版工作憑證無效")
    try:
        await auth._check_revocation(
            user_id, tv, kid=jwt.get_unverified_header(token).get("kid", "")
        )
    except HTTPException as exc:
        _fail(
            exc.status_code,
            "DOCUMENT_IDENTITY_REVOKED",
            "文件排版工作憑證已撤銷或驗證服務暫時無法使用",
        )
    if level not in ("無機密", "營業秘密"):
        _fail(403, "EXPORT_CLASSIFICATION", "這個密等的內容不能匯出文件")
    return RenderIdentity(user_id, job_id, level, claims["request_sha256"])


@router.post("/render")
async def render_quick_document(
    request: RenderQuickDocument,
    identity: RenderIdentity = Depends(require_render_identity),
) -> Response:
    if request.job_id != identity.job_id:
        _fail(403, "DOCUMENT_JOB_MISMATCH", "工作憑證不適用這筆文件")
    digest = hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if not hmac.compare_digest(digest, identity.request_sha256):
        _fail(403, "DOCUMENT_REQUEST_MISMATCH", "文件內容或範本與工作憑證不符")
    try:
        content = await asyncio.wait_for(render_document_bytes(request), timeout=60)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        _fail(504, "DOCUMENT_RENDER_TIMEOUT", "文件排版逾時，請稍後再試")
    except Exception:
        _fail(
            502, "DOCUMENT_RENDER_FAILED", "文件排版失敗，請通知管理員確認離線轉檔服務"
        )
    return Response(
        content,
        media_type=MEDIA_TYPES[request.format],
        headers={
            "Content-Disposition": f'attachment; filename="assistant-draft.{request.format}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-ANILA-Job-Id": identity.job_id,
            "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        },
    )
