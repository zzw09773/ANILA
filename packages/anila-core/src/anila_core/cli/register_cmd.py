"""anila-core register — register an agent on the ANILA CSP platform.

Reads anila.yaml from the current directory, authenticates with CSP
using a JWT login, and calls POST /api/agents/register.
"""

from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml


_ANILA_YAML = "anila.yaml"


def run(args: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="anila-core register",
        description="Register this agent on the ANILA CSP platform.",
    )
    parser.add_argument(
        "--csp", metavar="URL",
        default="",
        help="CSP base URL (e.g. http://localhost:8000). Overrides anila.yaml / env.",
    )
    parser.add_argument(
        "--endpoint", metavar="URL",
        default="",
        help="Agent endpoint URL. Overrides anila.yaml endpoint_url.",
    )
    parser.add_argument(
        "--username", "-u",
        default="",
        help="CSP username (developer or admin account). Prompted if omitted.",
    )
    parser.add_argument(
        "--manifest", metavar="PATH",
        default=_ANILA_YAML,
        help=f"Path to agent manifest file (default: {_ANILA_YAML}).",
    )
    parsed = parser.parse_args(args)

    manifest = _load_manifest(parsed.manifest)

    csp_url = (parsed.csp or _env("CSP_BASE_URL") or "http://localhost:8000").rstrip("/")
    if not parsed.csp and not _env("CSP_BASE_URL"):
        entered = input(f"CSP URL [{csp_url}]: ").strip()
        if entered:
            csp_url = entered.rstrip("/")

    endpoint_url = parsed.endpoint or manifest.get("endpoint_url", "")
    if not endpoint_url:
        endpoint_url = input("Agent endpoint URL (e.g. http://your-host:9100): ").strip()
        if not endpoint_url:
            print("error: endpoint_url is required", file=sys.stderr)
            sys.exit(1)
    manifest["endpoint_url"] = endpoint_url

    username = parsed.username or input("CSP username: ").strip()
    if not username:
        print("error: username is required", file=sys.stderr)
        sys.exit(1)
    password = getpass.getpass(f"Password for {username}: ")

    jwt_token = _login(csp_url, username, password)
    result = _register(csp_url, jwt_token, manifest)

    print(f"\n✓ Agent registered successfully")
    print(f"  ID              : {result['id']}")
    print(f"  Name            : {result['name']}")
    print(f"  Approval status : {result['approval_status']}")
    print(f"\nNext: ask an admin to approve via CSP console or:")
    print(f"  curl -X POST {csp_url}/api/agents/{result['id']}/approve \\")
    print(f"       -H 'Authorization: Bearer <ADMIN_JWT>'")


def _load_manifest(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        print(
            f"error: '{path}' not found. Run `anila-core init` first, or specify --manifest.",
            file=sys.stderr,
        )
        sys.exit(1)
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    _require(data, "name", path)
    _require(data, "description_for_router", path)
    return data


def _require(data: dict, key: str, source: str) -> None:
    if not data.get(key):
        print(f"error: '{key}' is required in {source}", file=sys.stderr)
        sys.exit(1)


def _login(csp_url: str, username: str, password: str) -> str:
    try:
        resp = httpx.post(
            f"{csp_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except httpx.HTTPStatusError as e:
        detail = _extract_detail(e.response)
        print(f"error: login failed — {detail}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"error: cannot reach CSP at {csp_url} — {e}", file=sys.stderr)
        sys.exit(1)


def _register(csp_url: str, token: str, manifest: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "name": manifest["name"],
        "endpoint_url": manifest["endpoint_url"],
        "description_for_router": manifest["description_for_router"],
        "api_version": manifest.get("api_version", "v1"),
    }
    if manifest.get("base_model"):
        payload["base_model_name"] = manifest["base_model"]
    if manifest.get("capabilities"):
        payload["capabilities"] = manifest["capabilities"]
    if manifest.get("input_schema"):
        payload["input_schema"] = manifest["input_schema"]

    try:
        resp = httpx.post(
            f"{csp_url}/api/agents/register",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        detail = _extract_detail(e.response)
        print(f"error: registration failed — {detail}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"error: cannot reach CSP at {csp_url} — {e}", file=sys.stderr)
        sys.exit(1)


def _extract_detail(response: httpx.Response) -> str:
    try:
        return response.json().get("detail", response.text)
    except Exception:
        return response.text


def _env(key: str) -> str:
    import os
    return os.environ.get(key, "")
