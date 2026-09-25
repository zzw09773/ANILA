"""Test-only helpers. Not part of the downloadable runtime."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = (
    ROOT.parents[1]
    / "packages"
    / "anila-core"
    / "src"
    / "anila_core"
    / "contrib"
    / "anila_verify.py"
)

# Import the canonical file under the name the zip will use, without
# writing a second copy into the scaffold tree.
import importlib.util

_spec = importlib.util.spec_from_file_location("anila_verify", CANONICAL)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"canonical verifier missing: {CANONICAL}")
_module = importlib.util.module_from_spec(_spec)
sys.modules["anila_verify"] = _module
_spec.loader.exec_module(_module)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    numbers = key.public_key().public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {"kty": "RSA", "kid": kid, "alg": "RS256", "n": b64url(n), "e": b64url(e)}


def sign_jwt(key: rsa.RSAPrivateKey, kid: str, claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    header_b64 = b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{b64url(signature)}"


def dispatch_claims(
    *,
    user_id: int = 7,
    department: int | None = 3,
    agent_id: int = 42,
    exp: float | None = None,
) -> dict:
    now = int(time.time())
    return {
        "iss": "anila-csp",
        "aud": "anila-agent",
        "sub": str(user_id),
        "user_id": user_id,
        "department": department,
        "agent_id": agent_id,
        "iat": now,
        "exp": int(exp if exp is not None else now + 300),
        "jti": "test-jti",
    }


def pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
