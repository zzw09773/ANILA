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


# ---------------------------------------------------------------------------
# JWKS URL scheme: ca_file must not be silently ignored (MEDIUM finding)
# ---------------------------------------------------------------------------


def _write_test_ca_and_server_cert(tmp_path: Path):
    """Return (ca_pem_path, server_cert_path, server_key_path) for 127.0.0.1."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import NameOID

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    srv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    srv_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(srv_name)
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(2)
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path, srv_key


def _jwks_body_for_key(priv) -> bytes:
    nums = priv.public_key().public_numbers()
    return json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "scheme-test",
                    "n": _int_b64(nums.n),
                    "e": _int_b64(nums.e),
                }
            ]
        }
    ).encode("utf-8")


def test_fetch_jwks_rejects_http_when_ca_file_supplied(tmp_path):
    """http never consults SSLContext — ca_file must not be silently ignored.

    Kill: remove the JWKS URL scheme allowlist / https requirement.
    """
    import http.server
    import threading

    mod = _load_standalone()
    ca_path, _cert, _key, srv_key = _write_test_ca_and_server_cert(tmp_path)
    body = _jwks_body_for_key(srv_key)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        with pytest.raises(mod.AnilaVerifyError, match=r"必須是 https|ca_file"):
            mod.fetch_jwks(
                f"http://127.0.0.1:{port}/.well-known/jwks.json",
                ca_file=str(ca_path),
            )
    finally:
        server.server_close()


def test_fetch_jwks_rejects_file_scheme(tmp_path):
    """file: reaches urlopen; must be rejected by scheme check, not status quirk."""
    mod = _load_standalone()
    ca_path, _cert, _key, srv_key = _write_test_ca_and_server_cert(tmp_path)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_bytes(_jwks_body_for_key(srv_key))
    with pytest.raises(mod.AnilaVerifyError, match=r"僅 https|不支援的 scheme"):
        mod.fetch_jwks(jwks_path.as_uri(), ca_file=str(ca_path))


def test_fetch_jwks_https_with_ca_file_still_works(tmp_path):
    """Normal case: https JWKS + ca_file must keep working after the scheme guard."""
    import http.server
    import ssl
    import threading

    mod = _load_standalone()
    ca_path, cert_path, key_path, srv_key = _write_test_ca_and_server_cert(tmp_path)
    body = _jwks_body_for_key(srv_key)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        keys = mod.fetch_jwks(
            f"https://127.0.0.1:{port}/.well-known/jwks.json",
            ca_file=str(ca_path),
        )
    finally:
        server.server_close()
    assert "scheme-test" in keys


def test_fetch_jwks_jwks_escape_hatch_makes_zero_network_calls(monkeypatch):
    """Offline path: verify_authorization(jwks=...) must not open sockets."""
    mod = _load_standalone()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "offline"
    jwks = _jwks(mod, priv, kid)
    token = _sign(mod, priv, kid)

    def _boom(*_a, **_k):
        raise AssertionError("network must not be touched when jwks= is supplied")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(mod, "_jwks_urlopen", _boom)
    claims = mod.verify_authorization(f"Bearer {token}", jwks=jwks)
    assert claims["agent_id"] == 42


# ---------------------------------------------------------------------------
# Redirect must not bypass ca_file (F-1)
# ---------------------------------------------------------------------------


def _attacker_jwks_body() -> bytes:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = priv.public_key().public_numbers()
    return json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "ATTACKER-KID",
                    "n": _int_b64(nums.n),
                    "e": _int_b64(nums.e),
                }
            ]
        }
    ).encode("utf-8")


def test_fetch_jwks_rejects_https_to_http_redirect(tmp_path):
    """AC-1a: https (ca_file OK) -> 302 Location: http://attacker must fail.

    Kill: restore urllib default redirect following (urlopen + HTTPRedirectHandler).
    """
    import http.server
    import ssl
    import threading

    mod = _load_standalone()
    ca_path, cert_path, key_path, _srv_key = _write_test_ca_and_server_cert(tmp_path)
    attacker_body = _attacker_jwks_body()

    class Attacker(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(attacker_body)

        def log_message(self, *_args):
            return

    attacker = http.server.HTTPServer(("127.0.0.1", 0), Attacker)
    attacker_port = attacker.server_address[1]
    threading.Thread(target=attacker.handle_request, daemon=True).start()

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{attacker_port}/jwks"
            )
            self.end_headers()

        def log_message(self, *_args):
            return

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    redirector.socket = ctx.wrap_socket(redirector.socket, server_side=True)
    https_port = redirector.server_address[1]
    threading.Thread(target=redirector.handle_request, daemon=True).start()

    returned_kids: list[str] = []
    try:
        with pytest.raises(mod.AnilaVerifyError, match=r"redirect|重新導向") as ei:
            keys = mod.fetch_jwks(
                f"https://127.0.0.1:{https_port}/jwks",
                ca_file=str(ca_path),
            )
            returned_kids.extend(keys.keys())
    finally:
        redirector.server_close()
        attacker.server_close()

    assert "ATTACKER-KID" not in returned_kids
    assert "ATTACKER-KID" not in str(ei.value)


def test_fetch_jwks_rejects_redirect_to_untrusted_https(tmp_path):
    """AC-1b: 302 -> https with cert NOT under ca_file must also fail."""
    import http.server
    import ssl
    import threading

    mod = _load_standalone()
    ca_path, cert_path, key_path, _ = _write_test_ca_and_server_cert(tmp_path)
    # Target is signed by a different CA — not in ca_file. Refuse-redirect
    # design fails at the 302; if redirects were followed, hop-2 TLS would fail
    # under ca_file (and must not fall back to the system trust store).
    tgt_dir = tmp_path / "tgt"
    tgt_dir.mkdir()
    _tgt_ca, tgt_cert, tgt_key, _ = _write_test_ca_and_server_cert(tgt_dir)
    attacker_body = _attacker_jwks_body()

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(attacker_body)

        def log_message(self, *_args):
            return

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    tctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tctx.load_cert_chain(certfile=str(tgt_cert), keyfile=str(tgt_key))
    target.socket = tctx.wrap_socket(target.socket, server_side=True)
    tgt_port = target.server_address[1]
    threading.Thread(target=target.handle_request, daemon=True).start()

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header(
                "Location", f"https://127.0.0.1:{tgt_port}/jwks"
            )
            self.end_headers()

        def log_message(self, *_args):
            return

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    rctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    rctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    redirector.socket = rctx.wrap_socket(redirector.socket, server_side=True)
    https_port = redirector.server_address[1]
    threading.Thread(target=redirector.handle_request, daemon=True).start()

    returned_kids: list[str] = []
    try:
        with pytest.raises(
            mod.AnilaVerifyError, match=r"redirect|重新導向"
        ) as ei:
            keys = mod.fetch_jwks(
                f"https://127.0.0.1:{https_port}/jwks",
                ca_file=str(ca_path),
            )
            returned_kids.extend(keys.keys())
    finally:
        redirector.server_close()
        target.server_close()

    assert "ATTACKER-KID" not in returned_kids
    assert "ATTACKER-KID" not in str(ei.value)
    assert "CERTIFICATE_VERIFY_FAILED" not in str(ei.value)


def test_fetch_jwks_redirect_error_names_redirect_explicitly(tmp_path):
    """AC-1c: refuse-redirects design — 302 error must name redirect, not look like a server fault."""
    import http.server
    import ssl
    import threading

    mod = _load_standalone()
    ca_path, cert_path, key_path, _ = _write_test_ca_and_server_cert(tmp_path)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "https://127.0.0.1/jwks-final")
            self.end_headers()

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        with pytest.raises(
            mod.AnilaVerifyError,
            match=r"拒絕跟隨.*redirect|redirect.*拒絕跟隨",
        ):
            mod.fetch_jwks(
                f"https://127.0.0.1:{port}/jwks",
                ca_file=str(ca_path),
            )
    finally:
        server.server_close()
