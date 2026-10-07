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
    named = re.search(
        r"map \$host \$anila_named_host \{(?P<body>.*?)\n\}",
        text,
        re.S,
    )
    builtin = re.search(
        r"map \$host \$anila_builtin_host \{(?P<body>.*?)\n\}",
        text,
        re.S,
    )
    assert named, "nginx map $anila_named_host disappeared"
    assert builtin, "nginx map $anila_builtin_host disappeared"
    assert "$is_anila_host" in text
    # 站台名稱單獨一張 map。跟 localhost 寫在一起時，dev 預設
    # ANILA_HOST=localhost 會變成重複鍵，nginx 起不來。
    body = named.group("body") + builtin.group("body")
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


def test_public_redirects_keep_a_nondefault_https_port():
    """$host 沒有埠。對外轉向要帶 NGINX_HTTPS_PORT，同一條連線的補斜線用 $http_host。"""
    text = _NGINX.read_text(encoding="utf-8")
    assert "return 301 https://$host$request_uri;" not in text
    assert "return 302 https://$host/login" not in text
    assert "return 302 $scheme://$host$request_uri;" not in text
    assert "return 302 $scheme://$host/anila$request_uri;" not in text
    assert "proxy_set_header Host $host;" not in text
    assert "proxy_set_header Host              $host;" not in text
    assert "X-Forwarded-Host    $host;" not in text
    assert "X-Forwarded-Server  $host;" not in text

    rendered = text.replace("${NGINX_HTTPS_PORT}", "8443")
    assert "return 301 https://$host:8443$request_uri;" in rendered
    assert "return 302 https://$host:8443/login$is_args$args;" in rendered
    assert "return 302 $scheme://$host:8443$request_uri;" in rendered
    assert "return 302 $scheme://$host:8443/anila$request_uri;" in rendered
    assert text.count("return 301 $scheme://$http_host$uri/$is_args$args;") >= 4

    platform = yaml.safe_load(_PLATFORM.read_text(encoding="utf-8"))
    nginx_env = platform["services"]["nginx"]["environment"]
    assert str(nginx_env["NGINX_HTTPS_PORT"]).startswith("${NGINX_HTTPS_PORT")

    dev_path = _REPO / "infra" / "compose" / "dev.yml"
    dev = yaml.safe_load(dev_path.read_text(encoding="utf-8"))
    dev_port = str(dev["services"]["nginx"]["environment"]["NGINX_HTTPS_PORT"])
    assert "NGINX_HTTPS_PORT_DEV" in dev_port
    assert "8443" in dev_port


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
