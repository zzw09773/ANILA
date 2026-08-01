"""JwksClient must not fetch keys over transports that ignore ca_file.

INVARIANT: with a configured CA pin, http (and non-https schemes) must
fail closed before any key enters the cache. Kill: delete
``_require_https_jwks_url`` / its call in ``_fetch_jwks``.

Escape hatch for offline/tests: ``fetch_fn=`` (zero network). Not the
platform ``ANILA_ALLOW_HTTP_*`` flags — those gate outbound model/agent
dials, not agent-side JWKS trust.
"""

from __future__ import annotations

import base64
import datetime
import http.server
import ssl
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from anila_core.api.middleware.jwks_client import JwksClient, JwksFetchError


ATTACKER_KID = "attacker-kid"
LEGIT_KID = "scheme-test"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _int_b64(value: int) -> str:
    length = (value.bit_length() + 7) // 8 or 1
    return _b64url(value.to_bytes(length, "big"))


def _jwks_body_for_key(priv: rsa.RSAPrivateKey, *, kid: str) -> bytes:
    nums = priv.public_key().public_numbers()
    return (
        '{"keys":[{"kty":"RSA","kid":"%s","n":"%s","e":"%s"}]}'
        % (kid, _int_b64(nums.n), _int_b64(nums.e))
    ).encode("utf-8")


def _write_test_ca_and_server_cert(tmp_path: Path):
    """Return (ca_path, cert_path, key_path, server_private_key)."""
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jwks-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=30))
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
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = tmp_path / "ca.pem"
    cert_path = tmp_path / "server.pem"
    key_path = tmp_path / "server-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        srv_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return ca_path, cert_path, key_path, srv_key


def _serve_once(body: bytes, *, tls: tuple[Path, Path] | None = None) -> tuple[http.server.HTTPServer, int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    if tls is not None:
        cert_path, key_path = tls
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    threading.Thread(target=server.handle_request, daemon=True).start()
    return server, port


@pytest.mark.asyncio
async def test_jwks_client_rejects_http_when_ca_file_supplied(tmp_path):
    """http never consults verify=/ca_file — must fail closed, no attacker kid."""
    ca_path, _cert, _key, _srv_key = _write_test_ca_and_server_cert(tmp_path)
    # Serve an unrelated attacker key over plain http.
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    body = _jwks_body_for_key(attacker, kid=ATTACKER_KID)
    server, port = _serve_once(body)
    client = JwksClient(
        f"http://127.0.0.1:{port}/.well-known/jwks.json",
        ca_file=str(ca_path),
    )
    try:
        with pytest.raises(JwksFetchError, match=r"must be https|ca_file"):
            await client.get_public_key(ATTACKER_KID)
        assert ATTACKER_KID not in client._cache
        assert client.fetch_count == 1  # attempted, then refused before GET body use
    finally:
        server.server_close()


@pytest.mark.asyncio
async def test_jwks_client_rejects_file_scheme(tmp_path):
    ca_path, _cert, _key, srv_key = _write_test_ca_and_server_cert(tmp_path)
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_bytes(_jwks_body_for_key(srv_key, kid=LEGIT_KID))
    client = JwksClient(jwks_path.as_uri(), ca_file=str(ca_path))
    with pytest.raises(JwksFetchError, match=r"https only|unsupported scheme"):
        await client.get_public_key(LEGIT_KID)
    assert LEGIT_KID not in client._cache


@pytest.mark.asyncio
async def test_jwks_client_https_with_ca_file_still_works(tmp_path):
    """Legitimate path: https + ca_file must keep fetching keys."""
    ca_path, cert_path, key_path, srv_key = _write_test_ca_and_server_cert(tmp_path)
    body = _jwks_body_for_key(srv_key, kid=LEGIT_KID)
    server, port = _serve_once(body, tls=(cert_path, key_path))
    client = JwksClient(
        f"https://127.0.0.1:{port}/.well-known/jwks.json",
        ca_file=str(ca_path),
    )
    try:
        key = await client.get_public_key(LEGIT_KID)
    finally:
        server.server_close()
    assert key is not None
    assert LEGIT_KID in client._cache


@pytest.mark.asyncio
async def test_jwks_client_fetch_fn_bypasses_scheme_guard():
    """Offline escape hatch: fetch_fn= makes zero network calls."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from anila_core.api.middleware.jwks_client import parse_jwks

    doc = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "offline",
                "n": _int_b64(priv.public_key().public_numbers().n),
                "e": _int_b64(priv.public_key().public_numbers().e),
            }
        ]
    }

    async def fetch():
        return parse_jwks(doc)

    # http URL would be refused on the network path; fetch_fn short-circuits.
    client = JwksClient("http://attacker.example/.well-known/jwks.json", fetch_fn=fetch)
    key = await client.get_public_key("offline")
    assert key is not None
    assert "offline" in client._cache
