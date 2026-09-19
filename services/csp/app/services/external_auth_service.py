"""External authentication service.

LDAP 已自系統移除（將以 SSO 取代）；本模組僅保留 OIDC 流程。

Sprint 6 X / A6 security upgrades on top of Sprint 5 X:
- PKCE (RFC 7636, S256)：authorization request 帶 code_challenge，
  token exchange 帶 code_verifier — 即便授權碼被擷取，沒有 verifier
  也無法兌換 token。
- nonce：authorization request 帶 nonce → IdP 把它放進 id_token →
  callback 驗。擋 token replay / IdP-mixup attacks。
- id_token 簽章驗證：拿 token endpoint 回的 id_token，用 IdP 的 JWKS
  驗簽 + 驗 iss / aud / exp / nonce — userinfo endpoint 不再是唯一信任源。

state JWT 同時夾帶 ``code_verifier`` 與 ``nonce`` 的明文（state 是我們
用 SECRET_KEY 簽的 JWT，IdP 看不懂內容，使用者也無法竄改），把所有
single-use credential 集中在一條 server-side state 上，避免引入 cookie
或 session store。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from jose import jwt as jose_jwt
from jose.exceptions import JWTError
from sqlalchemy.orm import Session

from anila_core.security import UnsafeEndpointError, validate_outbound_url

from app.config import settings
from app.models.auth_provider import AuthProvider
from app.models.department import Department
from app.models.external_identity import ExternalIdentity
from app.models.user import User

EXTERNAL_STATE_JWT_ALGORITHM = "HS256"
from app.utils.security import hash_password


logger = logging.getLogger(__name__)

# 別名給原本以 ``jwt`` 名稱呼叫的程式碼用。
jwt = jose_jwt


EXTERNAL_STATE_AUDIENCE = "external-auth"

_NEXT_PATH_MAX_LEN = 200

# OIDC id_token signature algorithms accepted on this path.
# Pin of the set already effective via python-jose's JWT signature algorithms
# plus the HS* → client_secret / else → JWKS routing below — not a policy
# narrowing. Keep this frozenset and the docstring in lockstep.
_OIDC_ID_TOKEN_ALGS = frozenset(
    {
        "HS256",
        "HS384",
        "HS512",
        "RS256",
        "RS384",
        "RS512",
        "ES256",
        "ES384",
        "ES512",
    }
)


def sanitize_next_path(raw: str | None) -> str:
    """Sprint 6 X / B3: open-redirect 防護。

    OIDC ``next_path`` 是 query param + 由我們在 callback HTML 用
    ``meta http-equiv="refresh" content="0; url=..."`` 跳轉。攻擊者若能
    控制這個值，可以把使用者送到外部 phishing 站。allow-list 規則：

    - 必須以 ``/`` 開頭（同 origin）。
    - 第二字元不能是 ``/`` 或 ``\\``（擋 ``//evil.com`` /
      ``/\\evil.com`` protocol-relative bypass）。
    - 不允許 control char / NUL / CR / LF（擋 header injection）。
    - 長度上限 200 字（避免 query-string 爆量 + 簡化稽核）。

    任何不合法值都 fallback 為 ``/`` — 比 raise 友善（callback 仍能
    完成登入），但攻擊者拿不到任何重定向控制權。
    """
    if not raw or not isinstance(raw, str):
        return "/"
    if len(raw) > _NEXT_PATH_MAX_LEN:
        return "/"
    if not raw.startswith("/"):
        return "/"
    # 擋 //, /\, /\x00 等 protocol-relative / control-char 偽裝。
    if len(raw) >= 2 and raw[1] in ("/", "\\"):
        return "/"
    # CR / LF / NUL 一律拒絕（即便已 startswith "/"）。
    if any(ch in raw for ch in ("\r", "\n", "\x00")):
        return "/"
    return raw


def list_public_auth_providers(db: Session) -> list[AuthProvider]:
    return (
        db.query(AuthProvider)
        .filter(
            AuthProvider.is_active == True,
            AuthProvider.provider_type == "oidc",
        )
        .order_by(AuthProvider.provider_type, AuthProvider.name)
        .all()
    )


def _generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` — RFC 7636 §4.1/4.2.

    verifier 是 43–128 字 URL-safe random；challenge 是 verifier 的
    SHA-256 經 base64url（不含 padding）。
    """
    verifier = secrets.token_urlsafe(64)  # 64 bytes → ~86 chars，符合上限
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _generate_nonce() -> str:
    """OIDC nonce — 32 byte random，與 PKCE verifier 長度量級相當。"""
    return secrets.token_urlsafe(32)


def issue_external_state(
    provider: AuthProvider,
    next_path: str = "/",
    *,
    nonce: str | None = None,
    code_verifier: str | None = None,
) -> str:
    """Sign a single-use state JWT carrying redirect target + OIDC binding.

    ``nonce`` / ``code_verifier`` 由 caller 在發 authorization request 之
    前生成，傳進來一同夾在 state 裡，callback 拿回 state 後就能驗。
    """
    # B3：在簽進 JWT 前先 sanitize，避免 callback 端再讀回時又得做一次。
    safe_next = sanitize_next_path(next_path)
    payload: dict = {
        "aud": EXTERNAL_STATE_AUDIENCE,
        "provider_id": provider.id,
        "next_path": safe_next,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
    }
    if nonce is not None:
        payload["nonce"] = nonce
    if code_verifier is not None:
        payload["pkce"] = code_verifier
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=EXTERNAL_STATE_JWT_ALGORITHM)


def decode_external_state(state: str) -> dict:
    return jwt.decode(
        state,
        settings.SECRET_KEY,
        algorithms=[EXTERNAL_STATE_JWT_ALGORITHM],
        audience=EXTERNAL_STATE_AUDIENCE,
    )


def _resolve_oidc_secret(provider: AuthProvider) -> str:
    """Decrypt the stored OIDC client_secret. Empty string when unset."""
    from app.services.auth_provider_secret import load_oidc_client_secret

    return load_oidc_client_secret(provider) or ""


async def build_oidc_authorization_url(provider: AuthProvider, next_path: str = "/") -> str:
    """Compose the IdP authorization URL with PKCE (S256) + nonce.

    The ``code_verifier`` and ``nonce`` are serialised into the ``state``
    JWT so the callback can verify both without needing a session store.
    State is signed with ``SECRET_KEY`` and audience-scoped, so an
    attacker who replays / mints a state is rejected.
    """
    metadata = await _resolve_oidc_metadata(provider)
    redirect_uri = f"{settings.SITE_URL.rstrip('/')}/api/auth/oidc/{provider.id}/callback"

    code_verifier, code_challenge = _generate_pkce_pair()
    nonce = _generate_nonce()

    params = {
        "response_type": "code",
        "client_id": provider.oidc_client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.oidc_scopes or "openid profile email",
        "state": issue_external_state(
            provider,
            next_path=next_path,
            nonce=nonce,
            code_verifier=code_verifier,
        ),
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{metadata['authorization_endpoint']}?{urlencode(params)}"


async def authenticate_oidc_code(
    db: Session,
    provider: AuthProvider,
    code: str,
    state_payload: dict,
) -> User:
    """Exchange ``code`` for tokens, verify id_token, provision the user.

    ``state_payload`` is the already-decoded ``state`` JWT — caller
    (``api.auth.oidc_callback``) validates the audience + provider_id
    match before passing it in. We pull ``pkce`` (verifier) and ``nonce``
    out of it for token exchange and id_token verification.
    """
    redirect_uri = f"{settings.SITE_URL.rstrip('/')}/api/auth/oidc/{provider.id}/callback"
    client_secret = _resolve_oidc_secret(provider)
    code_verifier = state_payload.get("pkce")
    expected_nonce = state_payload.get("nonce")

    if not code_verifier:
        raise ValueError("state 缺少 PKCE verifier — 拒絕兌換 token")
    if not expected_nonce:
        raise ValueError("state 缺少 nonce — 拒絕兌換 token")

    # Release pooled connection before IdP discovery / token / userinfo (15s).
    # Provisioning below re-checkouts; nothing pending must survive this commit.
    db.commit()

    metadata = await _resolve_oidc_metadata(provider)

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": provider.oidc_client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    async with _oidc_http_client() as client:
        token_resp = await client.post(
            metadata["token_endpoint"],
            data=token_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_resp.raise_for_status()
        token_payload = token_resp.json()
        access_token = token_payload.get("access_token")
        id_token = token_payload.get("id_token")
        if not access_token:
            raise ValueError("OIDC 回應缺少 access_token")

        # 驗 id_token：簽章 + iss + aud + exp + nonce。任何一項失敗都拒絕。
        id_claims = await _verify_id_token(
            client, provider, metadata, id_token, expected_nonce=expected_nonce,
        )

        userinfo_resp = await client.get(
            metadata["userinfo_endpoint"],
            headers={"Authorization": f"Bearer {access_token}"},
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()

    # id_token 的 sub 必須和 userinfo 的 sub 一致 — RFC 7519 + OIDC §5.3.2。
    # 不一致代表 IdP 串接出問題，或攻擊者拿不同帳號的 access_token 餵給
    # userinfo，必須拒絕。
    if id_claims.get("sub") and userinfo.get("sub") and id_claims["sub"] != userinfo["sub"]:
        raise ValueError("OIDC id_token.sub 與 userinfo.sub 不一致，拒絕登入")

    subject_claim = provider.oidc_subject_claim or "sub"
    username_claim = provider.oidc_username_claim or "preferred_username"
    email_claim = provider.oidc_email_claim or "email"
    subject = str(userinfo.get(subject_claim) or id_claims.get(subject_claim) or "")
    if not subject:
        raise ValueError("OIDC userinfo 缺少 subject claim")
    username = (
        userinfo.get(username_claim)
        or userinfo.get("preferred_username")
        or userinfo.get("name")
        or userinfo.get(email_claim)
        or subject
    )
    email = userinfo.get(email_claim) or id_claims.get(email_claim)
    # email_verified 接受 userinfo 或 id_token 任一邊宣告為 true 即可。
    email_verified = bool(
        userinfo.get("email_verified") or id_claims.get("email_verified")
    )

    return _provision_external_user(
        db,
        provider=provider,
        subject=subject,
        username=str(username),
        email=email,
        email_verified=email_verified,
    )


async def _verify_id_token(
    client: httpx.AsyncClient,
    provider: AuthProvider,
    metadata: dict,
    id_token: str | None,
    *,
    expected_nonce: str,
) -> dict:
    """Validate an OIDC id_token. Raises ``ValueError`` on any mismatch.

    Verification steps (OIDC Core §3.1.3.7):

    1. Header ``alg`` must be in ``_OIDC_ID_TOKEN_ALGS``
       (``HS256``/``HS384``/``HS512``/``RS256``/``RS384``/``RS512``/
       ``ES256``/``ES384``/``ES512``). ``none`` and any other value are
       rejected *before* signature verification.
    2. Signature verified against the IdP's JWKS (``metadata['jwks_uri']``
       with discovery fallback). ``HS*`` falls back to client_secret.
    3. ``iss`` must match the configured issuer URL (or its discovery
       form).
    4. ``aud`` must contain our ``client_id``; ``azp`` if present must
       equal it (OIDC §3.1.3.7 step 4).
    5. ``exp`` not past.
    6. ``nonce`` equals the value we sent in the authorization request.

    Returns the verified claims dict.
    """
    if not id_token:
        raise ValueError("OIDC token endpoint 未回傳 id_token；本系統強制驗 id_token")

    # 1. header / alg
    try:
        header = jose_jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise ValueError(f"id_token header 無法解析: {exc}") from exc
    alg = header.get("alg")
    if not alg or alg.lower() == "none":
        raise ValueError("id_token alg 不可為 none")
    if alg not in _OIDC_ID_TOKEN_ALGS:
        allowed = ", ".join(sorted(_OIDC_ID_TOKEN_ALGS))
        raise ValueError(f"id_token alg={alg!r} 不在允許清單 ({allowed})")

    # 2. signature verification
    issuer = (provider.oidc_issuer_url or "").rstrip("/") or metadata.get("issuer", "")
    if alg.startswith("HS"):
        # symmetric — use the client_secret as HMAC key (RFC 7518)
        client_secret = _resolve_oidc_secret(provider)
        if not client_secret:
            raise ValueError(
                "id_token 使用對稱演算法 (HS*)，但本機未保存 client_secret"
            )
        key = client_secret
    else:
        # asymmetric — fetch JWKS
        kid = header.get("kid")
        jwks = await _fetch_jwks(client, provider, metadata)
        key = _select_jwk(jwks, kid=kid, alg=alg)
        if key is None:
            raise ValueError(f"找不到匹配 kid={kid!r} alg={alg!r} 的 JWKS key")

    try:
        claims = jose_jwt.decode(
            id_token,
            key=key,
            algorithms=[alg],
            audience=provider.oidc_client_id,
            issuer=issuer or None,  # None 時 jose 跳過 iss 驗
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        raise ValueError(f"id_token 驗證失敗: {exc}") from exc

    # 3 / 4 已由 jose 驗（issuer + aud）；補驗 azp。
    azp = claims.get("azp")
    if azp and azp != provider.oidc_client_id:
        raise ValueError("id_token azp 不等於本服務 client_id")

    # 5. nonce
    actual_nonce = claims.get("nonce")
    if not actual_nonce or actual_nonce != expected_nonce:
        raise ValueError("id_token nonce 不符 — 疑似 replay 或 IdP-mixup")

    return claims


async def _fetch_jwks(
    client: httpx.AsyncClient, provider: AuthProvider, metadata: dict
) -> list[dict]:
    """Get the IdP's JWKS keys. Tries metadata first, then discovery."""
    jwks_uri = metadata.get("jwks_uri")
    if not jwks_uri:
        # 補打一次 discovery 取 jwks_uri；_resolve_oidc_metadata 只挑了
        # authorization / token / userinfo 三個 endpoint，沒帶 jwks_uri。
        issuer = (provider.oidc_issuer_url or "").rstrip("/")
        if not issuer:
            raise ValueError("OIDC Provider 缺 issuer_url，無法取得 JWKS")
        _require_safe_idp_url(issuer, "issuer")
        discovery = await client.get(f"{issuer}/.well-known/openid-configuration")
        discovery.raise_for_status()
        jwks_uri = discovery.json().get("jwks_uri")
        if not jwks_uri:
            raise ValueError("IdP discovery 缺 jwks_uri")
    # 信任錨：jwks_uri 決定平台信哪把金鑰。不管它來自 metadata 還是這裡的
    # 補打 discovery，GET 之前一律過同一道守衛。
    _require_safe_idp_url(jwks_uri, "jwks_uri")
    resp = await client.get(jwks_uri)
    resp.raise_for_status()
    return resp.json().get("keys", []) or []


def _select_jwk(keys: list[dict], *, kid: str | None, alg: str) -> dict | None:
    """Pick a JWK matching kid / alg / use=sig."""
    candidates = [
        k for k in keys
        if (kid is None or k.get("kid") == kid)
        and (k.get("alg") in (None, alg))
        and (k.get("use") in (None, "sig"))
    ]
    return candidates[0] if candidates else None


_IDP_URL_FIELDS = (
    "authorization_endpoint",
    "token_endpoint",
    "userinfo_endpoint",
    "jwks_uri",
)


def _require_safe_idp_url(url, field: str) -> str:
    """Every URL the IdP flow will talk to passes here, whatever its origin.

    Two checks, deliberately independent of each other and of the env flags:

    1. ``https://`` is mandatory. ``ANILA_ALLOW_HTTP_ENDPOINT`` relaxes model /
       agent endpoints; it does **not** relax the IdP. ``token_endpoint``
       receives ``client_secret`` + ``code`` + ``code_verifier`` and
       ``jwks_uri`` decides which keys sign a valid ``id_token`` — a plaintext
       hop on either is a credential leak or a forged login, not a dev
       convenience.
    2. :func:`validate_outbound_url` (same guard as model / agent / ingestion
       endpoint rows): loopback, link-local, metadata, internal zones,
       single-label names, RFC 1918 unless ``ANILA_ALLOW_PRIVATE_ENDPOINT`` /
       ``ANILA_TRUSTED_HOSTS`` says so. An on-prem IdP lives under the same
       regime the model endpoints already do.

    Raises ``ValueError`` naming ``field`` so the callback's audit row says
    which URL was refused.
    """
    if not url or not isinstance(url, str):
        raise ValueError(f"OIDC {field} 缺少或不是字串")
    if not url.lower().startswith("https://"):
        raise ValueError(
            f"OIDC {field} 必須是 https://（收到 {url!r}）。"
            "IdP 端點不受 ANILA_ALLOW_HTTP_ENDPOINT 放寬。"
        )
    try:
        validate_outbound_url(url)
    except UnsafeEndpointError as exc:
        raise ValueError(f"OIDC {field} 被出向守衛拒絕：{exc}") from exc
    return url


def _oidc_http_client() -> httpx.AsyncClient:
    """Outbound IdP HTTP client.

    ``follow_redirects=False`` matches launch-manifest fetch
    (``app.modules.launch.manifest.fetch_service_manifest``). The
    request URL already passed ``_require_safe_idp_url``; a 302
    ``Location`` is attacker-controlled. Historical hole: https
    issuer -> 302 -> http. If an IdP later needs redirects, set
    True **and** re-run ``_require_safe_idp_url`` on ``response.url``.
    """
    return httpx.AsyncClient(timeout=15, follow_redirects=False)


def _guard_idp_response_url(response, requested_url: str, field: str) -> None:
    """If the client followed a redirect, the final URL must pass the same guard."""
    final = getattr(response, "url", None)
    if final is None:
        return
    final_s = str(final).rstrip("/")
    requested_s = requested_url.rstrip("/")
    if final_s != requested_s:
        _require_safe_idp_url(str(final), f"{field} (redirect target)")


async def _resolve_oidc_metadata(provider: AuthProvider) -> dict:
    """Resolve the IdP endpoint URLs.

    Sprint 6 X / A6: 也回傳 ``issuer`` 與 ``jwks_uri``，給 id_token 驗證用。
    若 admin 已手填 authz / token / userinfo 三個 endpoint，仍會 fallback
    discovery 來取 jwks_uri；jwks 是 _verify_id_token 不可或缺的依賴。

    🔴 安全不變式（2026-08-24 HIGH finding）：**回傳前，四個 URL 一律過
    :func:`_require_safe_idp_url`**——不分它是 admin 手填（DB 列沒有自己的
    validator）還是 discovery 回答的。能回答 discovery 的人，一次拿到
    ``client_secret``＋``code``＋``code_verifier``（token_endpoint）與
    「平台信哪把金鑰」（jwks_uri）；只守 jwks_uri 是假修。
    """
    explicit = {
        "authorization_endpoint": provider.oidc_authorization_endpoint,
        "token_endpoint": provider.oidc_token_endpoint,
        "userinfo_endpoint": provider.oidc_userinfo_endpoint,
    }
    issuer = (provider.oidc_issuer_url or "").rstrip("/")
    if all(explicit.values()) and not issuer:
        # 沒 issuer 就無法 discovery JWKS — 對 OIDC 安全模式而言不允許。
        raise ValueError(
            "OIDC Provider 缺少 issuer_url；id_token 驗簽需要 JWKS，無法降級。"
        )
    if not issuer:
        raise ValueError("OIDC Provider 缺少 issuer_url")

    # issuer 先驗再打：discovery 那一趟本身就是一次出向請求，而且它的回答
    # 決定接下來四個 URL。issuer 是 http:// 的話，連 MITM 都不必——能回答
    # DNS／ARP 的人就能接管整條登入鏈。
    _require_safe_idp_url(issuer, "issuer")
    discovery_url = f"{issuer}/.well-known/openid-configuration"
    async with _oidc_http_client() as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        _guard_idp_response_url(response, discovery_url, "issuer discovery")
        discovered = response.json()

    if all(explicit.values()):
        # 三個 endpoint 全由 admin 手填，但仍打 discovery 取 jwks_uri：
        # ① OIDC discovery 是 IdP 的標準介面，不另外要 admin 在 UI 多填一個
        #    jwks_uri 欄位（可用性那一半）；
        # ② 但這表示「平台信哪把金鑰」由 discovery 的回答者決定（安全那一半）
        #    ——所以拿到的 jwks_uri 跟手填的三個一樣，下面一律過守衛。
        metadata = {
            **explicit,
            "issuer": issuer,
            "jwks_uri": discovered.get("jwks_uri"),
        }
    else:
        metadata = {
            "authorization_endpoint": discovered.get("authorization_endpoint"),
            "token_endpoint": discovered.get("token_endpoint"),
            "userinfo_endpoint": discovered.get("userinfo_endpoint"),
            "issuer": discovered.get("issuer", issuer),
            "jwks_uri": discovered.get("jwks_uri"),
        }

    for field in _IDP_URL_FIELDS:
        if field == "jwks_uri" and metadata[field] is None:
            # 缺 jwks_uri 不是「被下毒」：_fetch_jwks 會補打一次 discovery，
            # 補到的那個 URL 在那邊過同一道守衛。
            continue
        _require_safe_idp_url(metadata[field], field)
    return metadata


def _provision_external_user(
    db: Session,
    *,
    provider: AuthProvider,
    subject: str,
    username: str,
    email: str | None,
    email_verified: bool,
) -> User:
    """Resolve / create the local user backing this external identity.

    Identity binding strategy (Sprint 5 X security review §H2):

    1. The only identity key we accept is ``(provider_id, subject)``. Any
       previously-bound identity is reused as-is.
    2. We **never** silently merge into an existing local user just because
       the ``email`` claim happens to match. That used to allow a malicious
       OIDC provider to take over the local admin account by claiming the
       admin's email; refuse the bind and surface an explicit error so an
       admin can choose whether to manually link the accounts.
    3. ``email_verified`` is required for any new auto-provisioning so an
       attacker that controls a reusable email at an unknown provider
       cannot spawn a real-user-shaped row in our DB.
    """
    identity = (
        db.query(ExternalIdentity)
        .filter(
            ExternalIdentity.provider_id == provider.id,
            ExternalIdentity.external_subject == subject,
        )
        .first()
    )
    if identity:
        identity.last_login_at = datetime.now(timezone.utc)
        if email:
            identity.external_email = email
        db.commit()
        return identity.user

    if not provider.auto_create_users:
        raise ValueError("此外部登入來源未啟用自動建立使用者")

    if email and not email_verified:
        raise ValueError(
            "OIDC userinfo 未提供 email_verified=true；拒絕自動建立帳號。"
            "請於 IdP 端確認 email 已驗證或要求 admin 手動建立。"
        )

    # Refuse to silently bind to a local account just because email matches.
    # Email collisions are surfaced as a hard error — the admin must choose
    # whether to manually merge identities (out-of-band).
    if email:
        existing = db.query(User).filter(User.email == email).first()
        if existing is not None:
            raise ValueError(
                f"OIDC 回應的 email「{email}」已綁定到本地帳號；"
                "為避免帳號接管風險，自動合併已停用，請聯絡 admin 手動處理。"
            )

    candidate_user = User(
        username=_generate_unique_username(db, username or email or subject),
        email=email,
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        role=provider.default_role or "user",
        department_id=_validate_default_department(db, provider.default_department_id),
        is_active=True,
        is_approved=True,
    )
    db.add(candidate_user)
    db.flush()

    identity = ExternalIdentity(
        user_id=candidate_user.id,
        provider_id=provider.id,
        external_subject=subject,
        external_username=username,
        external_email=email,
        last_login_at=datetime.now(timezone.utc),
    )
    db.add(identity)
    db.commit()
    db.refresh(candidate_user)
    return candidate_user


def _generate_unique_username(db: Session, base: str) -> str:
    sanitized = "".join(c for c in (base or "external") if c.isalnum() or c in {"-", "_", "."})
    sanitized = sanitized[:80] or "external"
    candidate = sanitized
    suffix = 1
    while db.query(User).filter(User.username == candidate).first():
        suffix += 1
        candidate = f"{sanitized[:70]}-{suffix}"
    return candidate


def _validate_default_department(db: Session, department_id: int | None) -> int | None:
    if department_id is None:
        return None
    department = db.query(Department).filter(Department.id == department_id).first()
    return department.id if department and department.is_active else None
