"""HTTP middleware for anila-core agent APIs.

P2.1 inbound auth surface (preferred)::

    from anila_core.api.middleware import DispatchIdentityMiddleware

Legacy static ``CspServiceTokenMiddleware`` /
``RotatingServiceTokenMiddleware`` are **not** re-exported here — import
them from ``anila_core.api.middleware.auth`` only if a call site has not
migrated yet. Those classes are fail-closed when the token is blank.
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
