"""Import ``anila_verify.py`` BY FILE PATH — proves air-gap copy-paste works.

Kill-proof negative set: signature / exp / iss / aud / RS256 / unknown kid /
non-finite exp. (Standalone has no JWKS cache refetch — unknown kid is a
hard miss in the provided key map.)
"""

from __future__ import annotations

import base64
import importlib.util
import json
import math
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

VERIFY_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/anila_core/contrib/anila_verify.py"
)


def _load_standalone():
    spec = importlib.util.spec_from_file_location(
        "anila_verify_standalone", VERIFY_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _sign(mod, priv, kid: str, *, header_extra=None, **claims_extra):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    if header_extra:
        header.update(header_extra)
    payload = {
        "iss": mod.DISPATCH_TOKEN_ISSUER,
        "aud": mod.DISPATCH_TOKEN_AUDIENCE,
        "sub": "7",
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": now,
        "exp": now + 300,
        "jti": "standalone",
    }
    payload.update(claims_extra)
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = priv.sign(
        f"{h}.{p}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return f"{h}.{p}.{_b64url(sig)}"


def _jwks(mod, priv, kid: str):
    nums = priv.public_key().public_numbers()
    return mod.parse_jwks(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": kid,
                    "n": _int_b64(nums.n),
                    "e": _int_b64(nums.e),
                }
            ]
        }
    )


def test_standalone_file_exists():
    assert VERIFY_PATH.is_file()


def test_standalone_verify_with_cached_jwks():
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    token = _sign(mod, priv, kid)
    claims = mod.verify_authorization(f"Bearer {token}", jwks=jwks)
    assert claims["user_id"] == 7
    assert claims["agent_id"] == 42


def test_standalone_rejects_missing_authorization():
    mod = _load_standalone()
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(None, jwks={})


def test_standalone_rejects_tamper():
    """Kill: remove signature check."""
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    token = _sign(mod, priv, kid)
    h, p, s = token.split(".")
    pad = "=" * (-len(p) % 4)
    payload = json.loads(base64.urlsafe_b64decode(p + pad))
    payload["user_id"] = 999
    p2 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {h}.{p2}.{s}", jwks=jwks)


def test_standalone_rejects_expired():
    """Kill: remove exp check."""
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    past = int(time.time()) - 600
    token = _sign(mod, priv, kid, iat=past, exp=past + 300)
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {token}", jwks=jwks)


def test_standalone_rejects_wrong_iss():
    """Kill: remove iss check."""
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    token = _sign(mod, priv, kid, iss="evil")
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {token}", jwks=jwks)


def test_standalone_rejects_wrong_aud():
    """Kill: remove aud check."""
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    token = _sign(mod, priv, kid, aud="other")
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {token}", jwks=jwks)


def test_standalone_rejects_non_rs256_alg():
    """Kill: remove RS256 alg pin."""
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    for bad in ("none", "HS256", "PS256", "rs256"):
        token = _sign(mod, priv, kid, header_extra={"alg": bad})
        with pytest.raises(mod.AnilaVerifyError):
            mod.verify_authorization(f"Bearer {token}", jwks=jwks)


def test_standalone_rejects_unknown_kid():
    """Kill: accept any kid by falling back to another key → this must go red.

    Token is signed by the published key but claims a kid absent from JWKS.
    If the verifier silently uses any cached key, signature would still pass.
    Standalone has no cache/refetch; unknown kid is a hard miss.
    """
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = _jwks(mod, priv, "known")
    token = _sign(mod, priv, "missing")  # valid sig, wrong kid label
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {token}", jwks=jwks)


def test_standalone_rejects_wrong_key_same_kid():
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = _jwks(mod, priv, "k1")
    token = _sign(mod, other, "k1")
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {token}", jwks=jwks)


def test_standalone_rejects_non_finite_exp():
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "k1"
    jwks = _jwks(mod, priv, kid)
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    payload = {
        "iss": mod.DISPATCH_TOKEN_ISSUER,
        "aud": mod.DISPATCH_TOKEN_AUDIENCE,
        "user_id": 7,
        "department": 3,
        "agent_id": 42,
        "iat": now,
        "exp": 1e999,
    }
    assert math.isinf(float(payload["exp"]))
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    sig = priv.sign(
        f"{h}.{p}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    with pytest.raises(mod.AnilaVerifyError):
        mod.verify_authorization(f"Bearer {h}.{p}.{_b64url(sig)}", jwks=jwks)


def test_standalone_source_has_no_ssl_cert_file_getenv():
    text = VERIFY_PATH.read_text(encoding="utf-8")
    assert 'getenv("SSL_CERT_FILE"' not in text
    assert "environ[\"SSL_CERT_FILE\"]" not in text
    assert "import jwt" not in text
    assert "import jose" not in text
    assert "from jose" not in text
