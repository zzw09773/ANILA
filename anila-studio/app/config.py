"""anila-studio runtime settings.

All values come from environment variables with sensible dev defaults.
Production deployments MUST override CSP_BASE_URL / REDIS_URL.

Wave-1 subagents will extend this file with their own additions; this is the
base shape they import against. Anything added later must keep the
pydantic-settings compatibility.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Service identity
    APP_NAME: str = "anila-studio"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"

    # csp (control plane) HTTP base — used by csp_client for RAG, model
    # registry, LLM proxy, and revocations cold-start sync.
    CSP_BASE_URL: str = "http://csp:8000"
    # Service-to-service shared secret (legacy) — eventually agent-credential
    # bearer will replace this. Same env name as csp side.
    CSP_SERVICE_TOKEN: str = ""

    # FLUX backend (raw image generation HTTP) — same as csp's FLUX_BACKEND_URL.
    FLUX_BACKEND_URL: str = "http://flux2-dev:8000"

    # pptx-renderer HTTP base (Node.js service)
    RENDERER_BASE_URL: str = "http://pptx-renderer:7100"

    # Redis URL — same instance csp publishes token-revoke events on.
    REDIS_URL: str = "redis://redis:6379/0"
    REDIS_REVOCATION_CHANNEL: str = "anila:auth:token-revoke"

    # JWT verify — only public key needed (csp signs with private).
    JWT_KID: str = "anila-v1"  # default until JWKS fetch overrides
    JWT_ALGORITHMS: tuple[str, ...] = ("RS256",)
    # Sub-second drift tolerance for the iat/exp checks.
    JWT_LEEWAY_SECONDS: int = 60

    # HTTP timeouts for csp_client.
    INTERNAL_TIMEOUT_SECONDS: float = 30.0
    INTERNAL_TIMEOUT_CONNECT: float = 5.0

    # FLUX cache dir — local volume on the anila-studio container.
    FLUX_CACHE_DIR: str = "/var/anila/anila-studio-flux-cache"

    # Behavioural toggles
    # In tests / dev we may want JWKS cache to refresh faster; default 1hr.
    JWKS_REFRESH_SECONDS: int = 3600
    # Revocation cache TTL (matches csp /api/auth/revocations retention).
    REVOCATION_CACHE_TTL_SECONDS: int = 30 * 24 * 3600

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
