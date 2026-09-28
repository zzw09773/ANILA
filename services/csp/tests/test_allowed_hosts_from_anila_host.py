"""入向 Host 白名單由 ANILA_HOST 衍生，預設不含實驗室 IP。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_PLATFORM = _REPO / "infra" / "compose" / "platform.yml"
_NGINX = _REPO / "infra" / "nginx" / "anila.conf"

_INTERNAL_CALLERS = (
    "localhost",
    "127.0.0.1",
    "::1",
    "csp",
    "router",
    "anila-studio",
    "asr-gateway",
    "ingestion-worker",
    "ip-literal",
)
_LAB = ("10.53.", "172.16.", "ncsist.org.tw", "gemma4", "gpt-oss", "nv-embed")


def _csp_env() -> dict:
    doc = yaml.safe_load(_PLATFORM.read_text(encoding="utf-8"))
    return doc["services"]["csp"]["environment"]


def test_allowed_hosts_is_derived_from_anila_host_not_a_site_list():
    value = str(_csp_env()["ALLOWED_HOSTS"])
    assert "${ALLOWED_HOSTS" not in value
    assert value.startswith("${ANILA_HOST:?")
    rest = value.split("},", 1)[1]
    assert rest.split(",") == list(_INTERNAL_CALLERS)
    for banned in _LAB:
        assert banned not in value


def test_nginx_map_uses_anila_host_and_has_no_lab_entries():
    text = _NGINX.read_text(encoding="utf-8")
    match = re.search(
        r"map \$host \$is_anila_host \{(?P<body>.*?)\n\}",
        text,
        re.S,
    )
    assert match, "nginx map $is_anila_host disappeared"
    body = match.group("body")
    assert "${ANILA_HOST}" in body
    assert "localhost" in body
    assert "127.0.0.1" in body
    assert '"::1"' in body
    assert "ip-literal" not in body
    assert "~^(?:(?:25[0-5]" in body
    assert "([0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}" in body
    assert "::ffff:" in body
    for name in ("csp", "router", "anila-studio", "asr-gateway", "ingestion-worker"):
        assert f'"{name}"' in body
    for banned in ("10.53.", "172.16.", "ncsist"):
        assert banned not in body

    doc = yaml.safe_load(_PLATFORM.read_text(encoding="utf-8"))
    nginx = doc["services"]["nginx"]
    assert str(nginx["environment"]["ANILA_HOST"]).startswith("${ANILA_HOST:?")
    mounts = nginx["volumes"]
    assert any(
        "anila.conf:/etc/nginx/templates/default.conf.template" in str(item)
        for item in mounts
    )


def test_nginx_envsubst_renders_anila_host_into_the_map():
    """Same substitution the official nginx entrypoint performs at start."""
    script = _REPO / "infra" / "nginx" / "check-anila-host-subst.sh"
    proc = subprocess.run(
        ["sh", str(script), "anila.example.test"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok anila.example.test" in proc.stdout
