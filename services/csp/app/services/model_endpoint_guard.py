"""模型端點位址與協定檢查。API 與設定匯入共用，不放在 router 層。"""
from __future__ import annotations

from urllib.parse import urlparse

from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import HTTPException


def _enforce_protocol_endpoint(protocol: str, endpoint_url: str) -> None:
    """Refuse protocol/URL pairs that cannot work.

    ``triton_grpc`` requires ``grpc://`` / ``grpcs://`` (no HTTP path join).
    ``openai_compatible`` requires ``http://`` / ``https://``.
    """
    scheme = (urlparse((endpoint_url or "").strip()).scheme or "").lower()
    if protocol == "triton_grpc":
        if scheme not in ("grpc", "grpcs"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "protocol=triton_grpc 的端點必須為 grpc:// 或 grpcs:// "
                    "（例如 grpc://host:9001），不可填 http(s) URL"
                ),
            )
        return
    if protocol == "openai_compatible":
        if scheme not in ("http", "https"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "protocol=openai_compatible 的端點必須為 http:// 或 https://"
                ),
            )


def _enforce_endpoint_url(url: str) -> None:
    """SSRF guard parity with agents.py / ingestion credentials.

    帳密不能掛在網址上（與外部服務 enforce_base_url 同一道）。拒絕訊息
    不含網址，避免把 userinfo 帶回回應。

    Mirrors agents.py _enforce_endpoint_url. Same env-driven overrides
    apply (ANILA_ALLOW_PRIVATE_ENDPOINT for RFC1918, ANILA_ALLOW_HTTP_ENDPOINT
    for http://, ANILA_TRUSTED_HOSTS comma-list + DB-backed admin allow-list
    via trusted_host_service). Previously model endpoints registered without
    any guard so admins could point at internal services like csp-db; closing
    that parity gap so all three places (agents / ingestion / models) share
    the same validator.

    When the failure is fixable (single-label hostname or internal-zone
    suffix — i.e. admin could legitimately want this host trusted), we
    surface a *structured* detail dict instead of a plain message string,
    so the frontend can render an actionable confirm modal ("Add
    'foobar' to trusted hosts?") instead of just an opaque alert.
    A private IP whose env switch is already on, but whose host is not
    trusted, is ``host_not_trusted`` — the console can add that host.
    The switch being off, and loopback / metadata, stay plain strings.

    Slice 6a: validated with ``endpoint_kind="model"`` — http:// model
    endpoints are rejected unless ``ANILA_ALLOW_HTTP_ENDPOINT=1`` is set
    (2026-07-29 決策: flag-gated uniformly, production included;
    default posture still rejects http).
    """
    from app.services.endpoint_rejection import unsafe_endpoint_http_detail

    parsed = urlparse((url or "").strip())
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(
            status_code=400,
            detail="位址不能帶帳號或密碼，請把憑證填在憑證欄",
        )
    try:
        validate_outbound_url(url, endpoint_kind="model")
    except UnsafeEndpointError as exc:
        hint = None
        if exc.fixable_by_trust_host:
            hint = (
                f"hostname {exc.host!r} 不在受信任清單。"
                f"若該主機在內部 docker network 上(例如 anila-models-net "
                f"內的推論服務),管理員可在 /trusted-hosts 加入後再試。"
            )
        raise HTTPException(
            status_code=400,
            detail=unsafe_endpoint_http_detail(exc, hint=hint),
        ) from exc
