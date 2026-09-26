# -*- coding: utf-8 -*-
"""把 SECRET_KEY 密封的憑證從舊密鑰轉封到新密鑰。不自己 commit。"""
from __future__ import annotations

import base64
from dataclasses import dataclass

from sqlalchemy.orm import Session

from anila_core.security.credential_crypto import decrypt_credential, encrypt_credential

_ENVELOPE_PREFIX = "enc::v1::"


@dataclass
class ResealReport:
    jwt_signing_keys: int = 0
    user_llm_credentials: int = 0
    agent_credentials: int = 0
    service_clients: int = 0
    auth_providers: int = 0


def _reseal_triple(
    ciphertext: bytes,
    nonce: bytes,
    tag: bytes,
    *,
    old_secret: str,
    new_secret: str,
) -> tuple[bytes, bytes, bytes]:
    plaintext = decrypt_credential(
        bytes(ciphertext), bytes(nonce), bytes(tag), secret=old_secret
    )
    new_ct, new_nonce, new_tag = encrypt_credential(plaintext, secret=new_secret)
    check = decrypt_credential(new_ct, new_nonce, new_tag, secret=new_secret)
    if check != plaintext:
        raise RuntimeError("轉封後無法用新 SECRET_KEY 解密")
    return new_ct, new_nonce, new_tag


def _reseal_envelope(stored: str | None, *, old_secret: str, new_secret: str) -> str | None:
    if not stored or not isinstance(stored, str) or not stored.startswith(_ENVELOPE_PREFIX):
        return stored
    raw = base64.urlsafe_b64decode(stored[len(_ENVELOPE_PREFIX) :].encode("ascii"))
    if len(raw) < 12 + 16:
        raise ValueError("密封 envelope 過短")
    nonce, tag, ciphertext = raw[:12], raw[12:28], raw[28:]
    new_ct, new_nonce, new_tag = _reseal_triple(
        ciphertext, nonce, tag, old_secret=old_secret, new_secret=new_secret
    )
    blob = base64.urlsafe_b64encode(new_nonce + new_tag + new_ct).decode("ascii")
    return f"{_ENVELOPE_PREFIX}{blob}"


def reseal_credentials(db: Session, *, old_secret: str, new_secret: str) -> ResealReport:
    """用舊密鑰解開、用新密鑰寫回。呼叫端決定要不要 commit。"""
    if not old_secret or not new_secret:
        raise ValueError("old_secret 與 new_secret 都不可為空")
    if old_secret == new_secret:
        raise ValueError("old_secret 與 new_secret 相同，沒有東西要轉封")

    from app.models.agent_credential import AgentCredential
    from app.models.auth_provider import AuthProvider
    from app.models.ingestion import UserLlmCredential
    from app.models.jwt_signing_key import JwtSigningKey
    from app.models.service_client import ServiceClient
    from app.services import jwt_keyring

    report = ResealReport()
    for row in db.query(JwtSigningKey).order_by(JwtSigningKey.id).all():
        ct, nonce, tag = _reseal_triple(
            row.private_ciphertext,
            row.private_nonce,
            row.private_tag,
            old_secret=old_secret,
            new_secret=new_secret,
        )
        row.private_ciphertext = ct
        row.private_nonce = nonce
        row.private_tag = tag
        report.jwt_signing_keys += 1

    for row in db.query(UserLlmCredential).order_by(UserLlmCredential.id).all():
        ct, nonce, tag = _reseal_triple(
            row.api_key_encrypted,
            row.api_key_nonce,
            row.api_key_tag,
            old_secret=old_secret,
            new_secret=new_secret,
        )
        row.api_key_encrypted = ct
        row.api_key_nonce = nonce
        row.api_key_tag = tag
        report.user_llm_credentials += 1

    for row in db.query(AgentCredential).order_by(AgentCredential.id).all():
        row.service_token_envelope = _reseal_envelope(
            row.service_token_envelope, old_secret=old_secret, new_secret=new_secret
        )
        row.service_token_previous_envelope = _reseal_envelope(
            row.service_token_previous_envelope,
            old_secret=old_secret,
            new_secret=new_secret,
        )
        report.agent_credentials += 1

    for row in db.query(ServiceClient).order_by(ServiceClient.id).all():
        row.service_token_envelope = _reseal_envelope(
            row.service_token_envelope, old_secret=old_secret, new_secret=new_secret
        )
        row.service_token_previous_envelope = _reseal_envelope(
            row.service_token_previous_envelope,
            old_secret=old_secret,
            new_secret=new_secret,
        )
        report.service_clients += 1

    for row in db.query(AuthProvider).order_by(AuthProvider.id).all():
        stored = row.oidc_client_secret
        if not isinstance(stored, str) or not stored.startswith(_ENVELOPE_PREFIX):
            continue
        row.oidc_client_secret = _reseal_envelope(
            stored, old_secret=old_secret, new_secret=new_secret
        )
        report.auth_providers += 1

    jwt_keyring._opened.clear()
    jwt_keyring._seal_cache.clear()
    db.flush()
    return report
