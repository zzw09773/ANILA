from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[5]
BASE_COMPOSE = ROOT / "infra/models/docker-compose.yml"
EXTERNAL_COMPOSE = ROOT / "infra/models/docker-compose.external-embed.yml"
EXTERNAL_WRAPPER = ROOT / "infra/models/external-embed-serve.sh"


def test_base_compose_cannot_inherit_external_grpc_target() -> None:
    payload = yaml.safe_load(BASE_COMPOSE.read_text(encoding="utf-8"))
    environment = payload["services"]["nv-embed-proxy"]["environment"]
    proxy = payload["services"]["nv-embed-proxy"]

    assert environment["TRITON_URL"] == "http://nv-embed-triton:8000"
    assert environment["TRITON_GRPC_URL"] == ""
    assert environment["REQUEST_TIMEOUT"] == "60"
    assert environment["READINESS_TIMEOUT"] == "5"
    assert proxy["healthcheck"]["test"][1].find("/health") >= 0
    healthcheck_command = proxy["healthcheck"]["test"][1]
    urllib_timeout_match = re.search(
        r"urlopen\(.*timeout=([0-9.]+)\)", healthcheck_command
    )
    assert urllib_timeout_match is not None
    readiness_timeout = float(environment["READINESS_TIMEOUT"])
    urllib_timeout = float(urllib_timeout_match.group(1))
    compose_timeout = float(str(proxy["healthcheck"]["timeout"]).removesuffix("s"))
    assert urllib_timeout > 10
    assert urllib_timeout > 2 * readiness_timeout
    assert compose_timeout > urllib_timeout


def test_external_compose_has_no_local_triton_dependency() -> None:
    payload = yaml.safe_load(EXTERNAL_COMPOSE.read_text(encoding="utf-8"))
    services = payload["services"]

    assert payload["name"] == "anila-external-embed"
    assert set(services) == {"nv-embed-proxy"}
    proxy = services["nv-embed-proxy"]
    assert proxy["container_name"] == "anila-external-embed-nv-embed-proxy"
    assert "ports" not in proxy
    assert "depends_on" not in proxy
    assert "nv-embed-triton" not in services
    assert proxy["user"] == "10001:10001"
    assert proxy["read_only"] is True
    assert proxy["tmpfs"] == ["/tmp"]
    assert proxy["cap_drop"] == ["ALL"]
    assert proxy["security_opt"] == ["no-new-privileges:true"]
    environment = proxy["environment"]
    assert environment["TRITON_GRPC_URL"].startswith("${TRITON_GRPC_URL:?")
    assert "TRITON_URL" not in environment
    assert environment["REQUEST_TIMEOUT"] == "60"
    assert environment["READINESS_TIMEOUT"] == "5"
    assert proxy["labels"] == {
        "com.anila.inference-role": "external-shim",
        "com.anila.provider-locality": "internal_shim",
        "com.anila.upstream-locality": "external_governed",
        "com.anila.upstream-transport": "triton-grpc",
        "com.anila.egress-network": "embedding-egress",
        "com.anila.egress-target": "${TRITON_GRPC_URL:?TRITON_GRPC_URL must be set to host:port}",
        "com.anila.upstream-egress-policy-id": "${ANILA_EXTERNAL_EMBED_UPSTREAM_EGRESS_POLICY_ID:?use the signed external embedding upstream egress policy id}",
        "com.anila.standalone-marker": "${ANILA_EXTERNAL_EMBED_STANDALONE:?use external-embed-serve.sh; this overlay must be standalone}",
    }
    assert proxy["healthcheck"]["test"][1].find("/health") >= 0
    healthcheck_command = proxy["healthcheck"]["test"][1]
    urllib_timeout_match = re.search(r"urlopen\(.*timeout=([0-9.]+)\)", healthcheck_command)
    assert urllib_timeout_match is not None
    readiness_timeout = float(environment["READINESS_TIMEOUT"])
    urllib_timeout = float(urllib_timeout_match.group(1))
    compose_timeout = float(str(proxy["healthcheck"]["timeout"]).removesuffix("s"))
    assert urllib_timeout > 10
    assert urllib_timeout > 2 * readiness_timeout
    assert compose_timeout > urllib_timeout
    assert set(proxy["networks"]) == {"models", "embedding-egress"}
    assert proxy["networks"]["models"] == {}
    assert proxy["networks"]["embedding-egress"] == {}
    assert payload["networks"]["models"] == {
        "external": True,
        "name": "${ANILA_MODELS_NETWORK:-anila-models-net}",
    }
    assert payload["networks"]["embedding-egress"] == {
        "driver": "bridge",
        "internal": False,
    }


def _compose_test_env() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            # An explicit empty value takes precedence over a developer's
            # repo .env, so the negative test really exercises no marker.
            "ANILA_EXTERNAL_EMBED_STANDALONE": "",
            "ANILA_EXTERNAL_EMBED_UPSTREAM_EGRESS_POLICY_ID": "egress.embedding",
            "INTERNAL_PLATFORM_API_KEY": "test-only",
            "TRITON_GRPC_URL": "172.16.120.35:9001",
        }
    )
    return environment


def _require_docker_compose() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker compose is required for resolved-compose guard coverage")


@pytest.mark.parametrize(
    "target",
    ["172.16.120.35:9001", "embed.example.internal:9001"],
)
def test_external_wrapper_renders_the_standalone_compose_artifact(
    target: str,
) -> None:
    _require_docker_compose()
    environment = _compose_test_env()
    environment["TRITON_GRPC_URL"] = target
    result = subprocess.run(
        ["bash", str(EXTERNAL_WRAPPER), "render"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["name"] == "anila-external-embed"
    assert set(payload["services"]) == {"nv-embed-proxy"}
    proxy = payload["services"]["nv-embed-proxy"]
    assert proxy["environment"]["TRITON_GRPC_URL"] == target
    assert proxy["labels"]["com.anila.egress-target"] == target
    assert proxy["labels"]["com.anila.upstream-egress-policy-id"] == "egress.embedding"
    assert (
        proxy["labels"]["com.anila.standalone-marker"]
        == "external-embed-only-v1"
    )
    assert proxy["user"] == "10001:10001"
    assert proxy["read_only"] is True
    assert "ALL" in proxy["cap_drop"]
    assert "no-new-privileges:true" in proxy["security_opt"]
    assert "/tmp" in proxy["tmpfs"]


@pytest.mark.parametrize(
    "target",
    [
        "127.1:9001",
        "127.000.000.001:9001",
        "2130706433:9001",
        "0x7f000001:9001",
        "0177.0.0.1:9001",
        "127.0.1:9001",
        "0x7f.0.0.1:9001",
    ],
)
def test_external_wrapper_rejects_ambiguous_numeric_ipv4_target(target: str) -> None:
    environment = _compose_test_env()
    environment["TRITON_GRPC_URL"] = target
    result = subprocess.run(
        ["bash", str(EXTERNAL_WRAPPER), "verify"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "canonical host:port" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "target",
    [
        "",
        "172.16.120.35",
        "http://172.16.120.35:9001",
        "172.16.120.35:9001/v2/models",
        "operator@172.16.120.35:9001",
        "EMBED.EXAMPLE.INTERNAL:9001",
        "embed.example.internal.:9001",
    ],
)
def test_external_wrapper_rejects_missing_or_noncanonical_target(
    target: str,
) -> None:
    environment = _compose_test_env()
    environment["TRITON_GRPC_URL"] = target
    result = subprocess.run(
        ["bash", str(EXTERNAL_WRAPPER), "render"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "TRITON_GRPC_URL" in (result.stdout + result.stderr)


@pytest.mark.parametrize("action", ["render", "verify"])
@pytest.mark.parametrize(
    "target",
    ["172.16.120.35:0", "embed.example.internal:0"],
)
def test_external_wrapper_rejects_zero_port_target(action: str, target: str) -> None:
    environment = _compose_test_env()
    environment["TRITON_GRPC_URL"] = target
    result = subprocess.run(
        ["bash", str(EXTERNAL_WRAPPER), action],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "canonical host:port" in (result.stdout + result.stderr)


def test_base_plus_external_overlay_fails_without_standalone_marker() -> None:
    _require_docker_compose()
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(EXTERNAL_COMPOSE),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=_compose_test_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "ANILA_EXTERNAL_EMBED_STANDALONE" in (result.stdout + result.stderr)
