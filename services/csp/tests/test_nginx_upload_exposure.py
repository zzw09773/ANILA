"""Static regressions for nginx's retained intranet-service boundary.

These tests deliberately inspect both HTTPS ``server`` blocks.  A previous
check stopped at the first occurrence, which allowed port 4443 to drift back
to an unauthenticated route while port 443 remained protected.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF = REPO_ROOT / "infra" / "nginx" / "anila.conf"
PLATFORM_COMPOSE = REPO_ROOT / "infra" / "compose" / "platform.yml"


def _balanced_block(text: str, start: int) -> str:
    """Return one brace-balanced nginx block beginning at ``start``."""

    opening = text.find("{", start)
    assert opening != -1
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError("unterminated nginx block")


def _https_servers(conf: str) -> list[str]:
    starts = [m.start() for m in re.finditer(r"(?m)^server\s*\{", conf)]
    blocks = [_balanced_block(conf, start) for start in starts]
    https = [
        block
        for block in blocks
        if "listen 443 ssl default_server;" in block
        or "listen 4443 ssl;" in block
    ]
    assert len(https) == 2, "expected exactly the 443 and 4443 HTTPS servers"
    return https


def _server_by_name(conf: str, name: str) -> str:
    starts = [m.start() for m in re.finditer(r"(?m)^server\s*\{", conf)]
    matches = [
        block
        for block in (_balanced_block(conf, start) for start in starts)
        if f"server_name {name};" in block
    ]
    assert len(matches) == 1, f"expected one nginx server for {name}"
    return matches[0]


def _location(server: str, declaration: str) -> str:
    marker = f"location {declaration} {{"
    start = server.find(marker)
    assert start != -1, f"missing nginx location: {marker}"
    return _balanced_block(server, start)


def _compact(block: str) -> str:
    return " ".join(block.split())


def test_platform_https_origins_do_not_serve_developer_tool_content() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    for server in _https_servers(conf):
        denied = _compact(
            _location(server, "~ ^/(?:codeserver|n8n|gitlab)(?:/|$)")
        )
        assert "return 404;" in denied


def test_each_developer_tool_has_a_distinct_native_auth_origin() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    compose = PLATFORM_COMPOSE.read_text(encoding="utf-8")

    expected = {
        "n8n.ai.ncsist.org.tw": "n8n:5678",
        "gitlab.ai.ncsist.org.tw": "gitlab:8181",
        "code.ai.ncsist.org.tw": "codeserver:8080",
    }
    for hostname, upstream in expected.items():
        server = _server_by_name(conf, hostname)
        root = _compact(_location(server, "/"))
        assert f'"{upstream}"' in root
        assert "proxy_pass" in root
        # CSP auth_request/cookies would recreate shared ambient authority.
        # Each isolated service must enforce its own native login instead.
        assert "auth_request" not in server
        assert "proxy_set_header Cookie" not in server

    for hostname, old_path in (
        ("n8n.ai.ncsist.org.tw", "n8n"),
        ("gitlab.ai.ncsist.org.tw", "gitlab"),
        ("code.ai.ncsist.org.tw", "codeserver"),
    ):
        server = _server_by_name(conf, hostname)
        legacy = _compact(_location(server, f"~ ^/{old_path}(?:/|$)"))
        assert "return 404;" in legacy

    assert "PASSWORD: ${CODESERVER_PASSWORD:?" in compose
    assert "N8N_USER_MANAGEMENT_DISABLED" not in compose
    assert "gitlab_rails['gitlab_signup_enabled'] = false" in compose


def test_optional_codeserver_hostname_is_resolved_only_when_requested() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    server = _server_by_name(conf, "code.ai.ncsist.org.tw")
    root = _compact(_location(server, "/"))

    assert "resolver 127.0.0.11" in conf
    assert 'set $codeserver_addr "codeserver:8080";' in root
    assert "proxy_pass http://$codeserver_addr;" in root
    assert "proxy_pass http://codeserver:8080" not in root


def test_n8n_isolated_origin_fails_closed_for_all_unauthenticated_inputs() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    server = _server_by_name(conf, "n8n.ai.ncsist.org.tw")

    callback = _compact(
        _location(
            server,
            "~* ^/(?:webhook|webhook-test|webhook-waiting|form|form-test|form-waiting|mcp|mcp-test|mcp-server)(?:/|$)",
        )
    )
    assert "return 404;" in callback
    for declaration in (
        "~* ^/(?:mcp-oauth|oauth)(?:/|$)",
        "~* ^/\\.well-known/(?:oauth-authorization-server|oauth-protected-resource)(?:/|$)",
    ):
        assert "return 404;" in _compact(_location(server, declaration))


def test_anila_ui_port_rejects_all_tool_hostnames() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    assert "map $host $is_anila_ui_host" in conf
    assert '"anila.ai.ncsist.org.tw" 1;' in conf
    for tool_host in (
        "n8n.ai.ncsist.org.tw",
        "gitlab.ai.ncsist.org.tw",
        "code.ai.ncsist.org.tw",
    ):
        assert f'"{tool_host}" 1;' not in conf.split("map $host $is_anila_ui_host", 1)[1].split("}", 1)[0]
    assert "if ($is_anila_ui_host = 0) { return 444; }" in conf


def test_both_https_servers_deny_ingestion_and_protect_all_other_uploads() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")

    for server in _https_servers(conf):
        ingestion_start = server.index("location ^~ /uploads/ingestion/ {")
        flux_start = server.index("location ^~ /uploads/flux/ {")
        generic_start = server.index("location /uploads/ {")
        assert ingestion_start < flux_start < generic_start

        ingestion = _compact(_location(server, "^~ /uploads/ingestion/"))
        assert "return 404;" in ingestion

        for declaration in ("^~ /uploads/flux/", "/uploads/"):
            upload = _compact(_location(server, declaration))
            assert "auth_request /_anila_auth/card-session;" in upload
            assert 'add_header Cache-Control "private, no-store, max-age=0" always;' in upload
            assert 'add_header Referrer-Policy "no-referrer" always;' in upload
            assert 'add_header X-Content-Type-Options "nosniff" always;' in upload
            assert "expires off;" in upload


def test_n8n_declares_exactly_one_trusted_reverse_proxy_hop() -> None:
    compose = PLATFORM_COMPOSE.read_text(encoding="utf-8")
    assert compose.count('N8N_PROXY_HOPS: "1"') == 1
    assert compose.count('N8N_SECURE_COOKIE: "true"') == 1


def test_gitlab_machine_git_uses_explicit_ssh_ingress_not_https_bypass() -> None:
    compose = PLATFORM_COMPOSE.read_text(encoding="utf-8")
    assert "${GITLAB_SSH_BIND_IP:?" in compose
    assert "${GITLAB_SSH_PORT:-2222}:22" in compose
    assert "gitlab_shell_ssh_port'] = ${GITLAB_SSH_PORT:-2222}" in compose
