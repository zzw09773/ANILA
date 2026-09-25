"""部署設定、JWKS 快取、一次 collection 搜尋。

驗章演算法只呼叫發行包裡的 anila_verify（CSP 原樣複製）。
本目錄不存放第二份驗證器。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

JWKS_TTL_SECONDS = 300
JWKS_REFRESH_SECONDS = 240
JWKS_RETRY_SECONDS = 10
JWKS_TIMEOUT_SECONDS = 5
UNKNOWN_KID_COOLDOWN_SECONDS = 2
RAG_TIMEOUT_SECONDS = 20
CONTEXT_CHAR_LIMIT = 6000
SEARCH_TOP_K = 5


class ConfigError(Exception):
    """部署設定不完整。health 不得宣告 ready。"""

    code = "config_missing"


class NotReady(Exception):
    """JWKS 尚無有效快取。受保護端點回 503。"""

    code = "jwks_unavailable"


@dataclass(frozen=True)
class Settings:
    csp_base_url: str | None
    ca_file: str | None
    agent_id: int | None
    llm_base_url: str | None
    llm_model: str | None
    llm_auth_required: bool | None
    llm_api_key: str | None
    llm_ca_file: str | None
    gaps: tuple[str, ...]

    @property
    def ready_config(self) -> bool:
        """True when nothing the developer still has to fill is missing.

        Platform fields and developer placeholders are both gaps until set.
        ``readiness_reason`` is what ``/health`` reports; this flag stays
        "fully filled in" for callers that mean that.
        """
        return not self.gaps

    def readiness_reason(self) -> str | None:
        """Why ``/health`` is not ready, or None when only JWKS can still block.

        Developer placeholders are allowed at process start. The first gap
        in the lab flow wins: model and key, then agent id. Platform fields
        (CSP origin, CA, LLM base, auth flag) are ``config_missing`` — run.sh
        already refuses to start without the first three.
        """
        names = set(self.gaps)
        if names & _PLATFORM_GAPS:
            return "config_missing"
        if names & _LLM_GAPS:
            return "llm_not_configured"
        if "ANILA_AGENT_ID" in names:
            return "not_registered"
        return None

    @property
    def jwks_url(self) -> str:
        return f"{self.csp_base_url.rstrip('/')}/.well-known/jwks.json"


_PLATFORM_GAPS = frozenset(
    {"CSP_BASE_URL", "ANILA_CA_FILE", "LLM_BASE_URL", "LLM_AUTH_REQUIRED"}
)
_LLM_GAPS = frozenset({"LLM_MODEL", "LLM_API_KEY"})


def _flag(value: str | None) -> bool | None:
    if value is None or value.strip() == "":
        return None
    text = value.strip().lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """讀程序環境。不解析 dotenv。缺項列入 gaps，不猜預設祕密。"""
    import os

    source = env if env is not None else dict(os.environ)
    gaps: list[str] = []
    csp = (source.get("CSP_BASE_URL") or "").strip().rstrip("/") or None
    if not csp or not csp.startswith("https://"):
        gaps.append("CSP_BASE_URL")
        csp = None
    ca = (source.get("ANILA_CA_FILE") or "").strip() or None
    if not ca:
        gaps.append("ANILA_CA_FILE")
    raw_id = (source.get("ANILA_AGENT_ID") or "").strip()
    agent_id: int | None
    try:
        agent_id = int(raw_id)
        if agent_id <= 0:
            raise ValueError
    except ValueError:
        gaps.append("ANILA_AGENT_ID")
        agent_id = None
    llm = (source.get("LLM_BASE_URL") or "").strip().rstrip("/") or None
    if not llm or not llm.startswith("https://") or not llm.endswith("/v1"):
        gaps.append("LLM_BASE_URL")
        llm = None
    model = (source.get("LLM_MODEL") or "").strip() or None
    if not model:
        gaps.append("LLM_MODEL")
    auth = _flag(source.get("LLM_AUTH_REQUIRED"))
    if auth is None:
        gaps.append("LLM_AUTH_REQUIRED")
    key = source.get("LLM_API_KEY") or None
    if auth is True and not key:
        gaps.append("LLM_API_KEY")
    return Settings(
        csp_base_url=csp,
        ca_file=ca,
        agent_id=agent_id,
        llm_base_url=llm,
        llm_model=model,
        llm_auth_required=auth,
        llm_api_key=key if auth else None,
        llm_ca_file=(source.get("LLM_CA_FILE") or "").strip() or None,
        gaps=tuple(gaps),
    )


class JwksCache:
    """固定 HTTPS origin 的 JWKS。完整取代，不永久合併消失的 key。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._keys: dict | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        self._last_forced = 0.0
        self._verifier = None

    def install_for_tests(self, document_keys: dict) -> None:
        """測試夾具：kid → JWK 物件，不打網路。"""
        verifier = self._load_verifier()
        self._keys = {
            kid: verifier.jwk_to_public_key(jwk) for kid, jwk in document_keys.items()
        }
        self._fetched_at = time.monotonic()

    def _load_verifier(self):
        if self._verifier is None:
            import anila_verify

            self._verifier = anila_verify
        return self._verifier

    def _parse(self, by_kid: dict):
        verifier = self._load_verifier()
        return verifier.parse_jwks({"keys": list(by_kid.values())})

    @property
    def fresh(self) -> bool:
        if self._keys is None:
            return False
        return (time.monotonic() - self._fetched_at) < JWKS_TTL_SECONDS

    async def refresh(self, *, force: bool = False) -> bool:
        if not self.settings.csp_base_url or not self.settings.ca_file:
            return False
        async with self._lock:
            age = time.monotonic() - self._fetched_at
            if not force and self._keys is not None and age < JWKS_REFRESH_SECONDS:
                return True
            if force and (time.monotonic() - self._last_forced) < UNKNOWN_KID_COOLDOWN_SECONDS:
                return self.fresh
            if force:
                self._last_forced = time.monotonic()
            try:
                keys = await asyncio.to_thread(self._fetch)
            except Exception:
                return False
            self._keys = keys
            self._fetched_at = time.monotonic()
            return True

    def _fetch(self):
        verifier = self._load_verifier()
        return verifier.fetch_jwks(
            self.settings.jwks_url,
            ca_file=self.settings.ca_file,
            timeout=JWKS_TIMEOUT_SECONDS,
        )

    async def verify(self, authorization: str | None) -> dict:
        if not self.fresh:
            await self.refresh(force=True)
        if not self.fresh or self._keys is None:
            raise NotReady("jwks")
        verifier = self._load_verifier()
        kid = _peek_kid(authorization)
        try:
            return verifier.verify_authorization(authorization, jwks=self._keys)
        except verifier.AnilaVerifyError as exc:
            if kid and kid not in self._keys:
                await self.refresh(force=True)
                if self._keys is not None and kid in self._keys:
                    return verifier.verify_authorization(authorization, jwks=self._keys)
            raise exc


def _peek_kid(authorization: str | None) -> str | None:
    """只讀未驗證 header 的 kid，不當認證結果。"""
    if not authorization or " " not in authorization:
        return None
    token = authorization.split(None, 1)[1]
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        import base64

        pad = "=" * (-len(parts[0]) % 4)
        header = json.loads(base64.urlsafe_b64decode(parts[0] + pad))
    except (ValueError, json.JSONDecodeError):
        return None
    kid = header.get("kid") if isinstance(header, dict) else None
    return kid if isinstance(kid, str) else None


def check_identity(claims: dict, agent_id: int) -> None:
    """外殼補做型別檢查，並比對部署的 ANILA_AGENT_ID。"""
    for name in ("user_id", "agent_id"):
        value = claims.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise IdentityError(name)
    if claims["agent_id"] != agent_id:
        raise AgentMismatch(claims["agent_id"])
    department = claims.get("department")
    if department is not None and (isinstance(department, bool) or not isinstance(department, int)):
        raise IdentityError("department")


class IdentityError(Exception):
    code = "invalid_claims"


class AgentMismatch(Exception):
    code = "agent_mismatch"


class SearchError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"search {status}")


async def search_once(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    collection_id: int,
    query: str,
    authorization: str,
    deadline: float,
) -> str:
    """用本輪派工 JWT 查一次。空結果是空字串；401/403/逾時不是查無資料。"""
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise SearchError(504)
    url = (
        f"{settings.csp_base_url}/api/ingestion/collections/{collection_id}/search"
    )
    try:
        response = await client.post(
            url,
            headers={"Authorization": authorization},
            json={"query": query[:4000], "top_k": SEARCH_TOP_K},
            timeout=min(RAG_TIMEOUT_SECONDS, remaining),
        )
    except httpx.TimeoutException as exc:
        raise SearchError(504) from exc
    if response.status_code in {401, 403}:
        raise SearchError(response.status_code)
    if response.status_code != 200:
        raise SearchError(502)
    body = response.json()
    results = body.get("results") if isinstance(body, dict) else None
    if not isinstance(results, list) or not results:
        return ""
    lines: list[str] = []
    used = 0
    for index, hit in enumerate(results, start=1):
        if not isinstance(hit, dict):
            continue
        filename = str(hit.get("filename") or "")
        document_id = hit.get("document_id")
        chunk_id = hit.get("chunk_id")
        content = str(hit.get("content") or "").strip()
        line = f"[{index}] {filename} doc={document_id} chunk={chunk_id}\n{content}"
        if used + len(line) > CONTEXT_CHAR_LIMIT:
            break
        lines.append(line)
        used += len(line)
    return "\n\n".join(lines)
