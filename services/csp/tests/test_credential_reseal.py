"""SECRET_KEY 更換前要把既有密封憑證轉封；解不開 active 金鑰就不要啟動。"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.models.ingestion import UserLlmCredential
from tests.conftest import make_user


@pytest.fixture
def pem_pair(tmp_path, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path = tmp_path / "jwt-private.pem"
    pub_path = tmp_path / "jwt-public.pem"
    priv_path.write_bytes(private_pem)
    pub_path.write_bytes(public_pem)
    from app.utils import security as sec

    monkeypatch.setattr(sec, "JWT_PRIVATE_KEY_PATH", str(priv_path))
    monkeypatch.setattr(sec, "JWT_PUBLIC_KEY_PATH", str(pub_path))
    sec._load_keys.cache_clear()
    yield private_key, priv_path, pub_path, private_pem, public_pem
    sec._load_keys.cache_clear()

OLD_SECRET = "pytest-fixed-not-a-real-secret-0123456789abcdef"
NEW_SECRET = "reseal-new-secret-key-0123456789abcdef"


def test_lifespan_refuses_startup_when_active_signing_key_cannot_be_opened():
    from app.main import lifespan

    source = inspect.getsource(lifespan)
    assert "assert_active_key_decryptable" in source


def test_undecryptable_active_key_names_the_reseal_command(db, pem_pair, monkeypatch):
    from app.services.jwt_keyring import ensure_bootstrapped
    from app.utils.security import JwtKeyLoadError

    ensure_bootstrapped(db, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    db.flush()
    monkeypatch.setenv("SECRET_KEY", NEW_SECRET)
    from app.services import jwt_keyring

    jwt_keyring._opened.clear()
    with pytest.raises(JwtKeyLoadError, match="reseal-credentials"):
        jwt_keyring.assert_active_key_decryptable(db)


def test_reseal_command_rewrites_secret_key_sealed_rows(db, pem_pair, monkeypatch):
    from anila_core.security.credential_crypto import decrypt_credential, encrypt_credential
    from app.services.credential_reseal import reseal_credentials
    from app.services.jwt_keyring import ensure_bootstrapped, _opened

    monkeypatch.setenv("SECRET_KEY", OLD_SECRET)
    moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ensure_bootstrapped(db, now=moment)
    user = make_user(db, username="reseal-user")
    ciphertext, nonce, tag = encrypt_credential("sk-user-llm")
    db.add(
        UserLlmCredential(
            created_by=user.id,
            name="judge",
            endpoint_url="https://llm.example/v1",
            model_name="judge-model",
            api_key_encrypted=ciphertext,
            api_key_nonce=nonce,
            api_key_tag=tag,
            created_at=moment,
        )
    )
    db.flush()
    from app.models.jwt_signing_key import JwtSigningKey

    signing = db.query(JwtSigningKey).filter_by(state="active").one()
    credential = db.query(UserLlmCredential).one()

    report = reseal_credentials(db, old_secret=OLD_SECRET, new_secret=NEW_SECRET)
    db.flush()
    assert report.jwt_signing_keys == 1
    assert report.user_llm_credentials == 1

    monkeypatch.setenv("SECRET_KEY", NEW_SECRET)
    _opened.clear()
    assert (
        decrypt_credential(
            signing.private_ciphertext,
            signing.private_nonce,
            signing.private_tag,
        )
        .encode("utf-8")
        .startswith(b"-----BEGIN")
    )
    assert (
        decrypt_credential(
            credential.api_key_encrypted,
            credential.api_key_nonce,
            credential.api_key_tag,
        )
        == "sk-user-llm"
    )
    monkeypatch.setenv("SECRET_KEY", OLD_SECRET)
    with pytest.raises(InvalidTag):
        decrypt_credential(
            signing.private_ciphertext,
            signing.private_nonce,
            signing.private_tag,
        )


def test_reseal_script_is_documented_and_runnable():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    status = (root / "docs/CURRENT-STATUS.md").read_text(encoding="utf-8")
    script = root / "infra/deployment/scripts/reseal-credentials.py"
    assert script.is_file()
    assert "reseal-credentials.py" in status
    text = script.read_text(encoding="utf-8")
    assert "--apply" in text
    assert "OLD_SECRET" in text or "old-secret" in text or "old_secret" in text
