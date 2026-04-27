"""Back-compat re-export of the central credential crypto helpers.

Sprint 5 / Chunk X: the actual implementation moved to
``anila_core.security.credential_crypto`` so the ingestion-worker can
import it without depending on the CSP backend's package layout
(worker container has anila-core but not the CSP backend code).

This module stays as a thin re-export so existing CSP-side imports
(``from app.services.credential_crypto import ...``) keep working
unchanged. New code should import from anila-core directly:

    from anila_core.security import encrypt_credential, decrypt_credential
"""

from anila_core.security.credential_crypto import (
    decrypt_credential,
    encrypt_credential,
)

__all__ = ["decrypt_credential", "encrypt_credential"]
