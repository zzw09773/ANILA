"""anila-core status — inspect registration / approval status on CSP."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

from .register_cmd import _env, _extract_detail, _login


_ANILA_YAML = "anila.yaml"


def run(args: list[str]) -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="anila-core status",
        description="Check this agent's registration / approval status on the ANILA CSP platform.",
    )
    parser.add_argument(
        "--csp",
        metavar="URL",
        default="",
        help="CSP base URL (e.g. http://localhost:8000). Overrides anila.yaml / env.",
    )
    parser.add_argument(
        "--username",
        "-u",
        default="",
        help="CSP username (developer or admin account). Prompted if omitted.",
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=_ANILA_YAML,
        help=f"Path to agent manifest file (default: {_ANILA_YAML}).",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Agent name to look up. Defaults to the manifest's name.",
    )
    parser.add_argument(
        "--id",
        type=int,
        default=0,
        help="Agent ID to look up directly.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="List all visible agents for this developer/admin account.",
    )
    parsed = parser.parse_args(args)

    csp_url = (parsed.csp or _env("CSP_BASE_URL") or "http://localhost:8000").rstrip("/")
    if not parsed.csp and not _env("CSP_BASE_URL"):
        entered = input(f"CSP URL [{csp_url}]: ").strip()
        if entered:
            csp_url = entered.rstrip("/")

    username = parsed.username or input("CSP username: ").strip()
    if not username:
        print("error: username is required", file=sys.stderr)
        sys.exit(1)

    import getpass

    password = getpass.getpass(f"Password for {username}: ")
    jwt_token = _login(csp_url, username, password)

    if parsed.all:
        _print_agents(_list_agents(csp_url, jwt_token))
        return

    if parsed.id:
        _print_agent(_get_agent(csp_url, jwt_token, parsed.id))
        return

    agent_name = parsed.name or _load_manifest_name(parsed.manifest)
    if not agent_name:
        print(
            "error: agent name is required. Use --name, --id, --all, or provide a manifest.",
            file=sys.stderr,
        )
        sys.exit(1)

    agents = _list_agents(csp_url, jwt_token)
    for agent in agents:
        if agent.get("name") == agent_name:
            _print_agent(agent)
            return

    print(f"error: agent '{agent_name}' not found", file=sys.stderr)
    sys.exit(1)


def _load_manifest_name(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    return str(data.get("name", "")).strip()


def _list_agents(csp_url: str, token: str) -> list[dict[str, Any]]:
    try:
        resp = httpx.get(
            f"{csp_url}/api/agents",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return list(resp.json())
    except httpx.HTTPStatusError as e:
        detail = _extract_detail(e.response)
        print(f"error: status lookup failed — {detail}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"error: cannot reach CSP at {csp_url} — {e}", file=sys.stderr)
        sys.exit(1)


def _get_agent(csp_url: str, token: str, agent_id: int) -> dict[str, Any]:
    try:
        resp = httpx.get(
            f"{csp_url}/api/agents/{agent_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return dict(resp.json())
    except httpx.HTTPStatusError as e:
        detail = _extract_detail(e.response)
        print(f"error: status lookup failed — {detail}", file=sys.stderr)
        sys.exit(1)
    except httpx.RequestError as e:
        print(f"error: cannot reach CSP at {csp_url} — {e}", file=sys.stderr)
        sys.exit(1)


def _print_agents(agents: list[dict[str, Any]]) -> None:
    if not agents:
        print("No agents found.")
        return
    for agent in agents:
        print(
            f"[{agent['id']}] {agent['name']} "
            f"status={agent['approval_status']} health={agent.get('health_status', 'unknown')}"
        )


def _print_agent(agent: dict[str, Any]) -> None:
    print(f"ID              : {agent['id']}")
    print(f"Name            : {agent['name']}")
    print(f"Approval status : {agent['approval_status']}")
    print(f"Health status   : {agent.get('health_status', 'unknown')}")
    print(f"Endpoint        : {agent['endpoint_url']}")
    if agent.get("api_version"):
        print(f"API version     : {agent['api_version']}")
