"""HTTP middleware for anila-core agent APIs.

P2.1 inbound auth surface (preferred)::

    from anila_core.api.middleware import DispatchIdentityMiddleware

The legacy static / rotating service-token middleware (``auth.py``) was
removed 2026-09-02: nothing in the platform imported it since P2.1.
"""

from .dispatch_auth import DispatchIdentityMiddleware, claims_from_request
from .dispatch_jwt import (
    DISPATCH_TOKEN_AUDIENCE,
    DISPATCH_TOKEN_ISSUER,
    DispatchTokenError,
    verify_dispatch_jwt,
)
from .jwks_client import JwksClient, derive_jwks_url

__all__ = [
    "DISPATCH_TOKEN_AUDIENCE",
    "DISPATCH_TOKEN_ISSUER",
    "DispatchIdentityMiddleware",
    "DispatchTokenError",
    "JwksClient",
    "claims_from_request",
    "derive_jwks_url",
    "verify_dispatch_jwt",
]
