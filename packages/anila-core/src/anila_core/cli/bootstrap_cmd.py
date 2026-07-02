"""anila-core agent bootstrap — exchange a bsk- for a long-lived csk- token.

Sprint 8 X / Phase B. Used once on agent first start (or after admin
rotation) to populate the per-agent service-token state file that
``RotatingServiceTokenMiddleware`` reads.

Usage
=====

::

    anila-core agent bootstrap \\
        --csp-url http://csp:8000 \\
        --bootstrap-token bsk-XXXXXX \\
        --agent-id 2 \\
        --endpoint-url http://my-rag:24786 \\
        [--label pod-1] \\
        [--state-dir /var/lib/anila-agent]

Behaviour
=========

* POSTs to ``{csp_url}/api/agents/{agent_id}/bootstrap`` with the bsk-
  and the agent's own ``endpoint_url`` (CSP verifies the URL matches
  its registered record — anti-replay-against-other-agent).
* Writes the returned csk- + metadata to
  ``{state_dir}/service_token.json`` with mode ``0600`` (best effort
  on Windows — the chmod call is no-op there).
* Returns exit code 0 on success, non-zero with a clear error on
  failure (expired token, endpoint mismatch, network error).

Bootstrap tokens are single-use; running this twice with the same
``--bootstrap-token`` is expected to fail on the second call.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx


DEFAULT_STATE_DIR = "/var/lib/anila-agent"
STATE_FILE_NAME = "service_token.json"


def run(args: list[str]) -> None:
    """Subcommand dispatcher.

    Currently only ``bootstrap`` is meaningful, but using a sub-noun
    parser keeps the door open for ``anila-core agent rotate`` and
    ``anila-core agent show`` later without breaking the CLI surface.
    """
    parser = argparse.ArgumentParser(
        prog="anila-core agent",
        description="Manage agent service-token credentials.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    bs = sub.add_parser(
        "bootstrap",
        help="Exchange an admin-issued bsk- for a long-lived csk- token.",
    )
    bs.add_argument(
        "--csp-url",
        required=True,
        metavar="URL",
        help="CSP base URL (must match what admin issued the bsk- against).",
    )
    bs.add_argument(
        "--bootstrap-token",
        metavar="bsk-...",
        default=None,
        help=(
            "The bsk- token from CSP admin UI. Read from "
            "$ANILA_BOOTSTRAP_TOKEN if omitted."
        ),
    )
    bs.add_argument(
        "--agent-id",
        type=int,
        required=True,
        help="The agent's id in CSP (shown next to the bsk- in admin UI).",
    )
    bs.add_argument(
        "--endpoint-url",
        required=True,
        metavar="URL",
        help=(
            "This agent's endpoint URL. Must match the URL the admin "
            "registered for this agent — CSP verifies."
        ),
    )
    bs.add_argument(
        "--label",
        default=None,
        help="Optional label for multi-replica deployments (e.g. pod-1).",
    )
    bs.add_argument(
        "--state-dir",
        default=os.environ.get("ANILA_AGENT_STATE_DIR", DEFAULT_STATE_DIR),
        help=(
            "Directory to write service_token.json (default: "
            f"{DEFAULT_STATE_DIR} or $ANILA_AGENT_STATE_DIR)."
        ),
    )
    bs.add_argument(
        "--ca-bundle",
        default=None,
        help="Path to a CA bundle for TLS verification (httpx default if unset).",
    )

    parsed = parser.parse_args(args)
    if parsed.action != "bootstrap":
        parser.error(f"unsupported action '{parsed.action}'")
    _bootstrap(parsed)


def _bootstrap(args: argparse.Namespace) -> None:
    bsk = args.bootstrap_token or os.environ.get("ANILA_BOOTSTRAP_TOKEN", "")
    if not bsk:
        print(
            "error: --bootstrap-token (or $ANILA_BOOTSTRAP_TOKEN) is required",
            file=sys.stderr,
        )
        sys.exit(2)

    csp = args.csp_url.rstrip("/")
    target = f"{csp}/api/agents/{args.agent_id}/bootstrap"
    payload = {
        "bootstrap_token": bsk,
        "endpoint_url": args.endpoint_url,
    }
    if args.label:
        payload["label"] = args.label

    verify: Any = True if args.ca_bundle is None else args.ca_bundle
    try:
        with httpx.Client(timeout=30.0, verify=verify) as client:
            resp = client.post(target, json=payload)
    except httpx.RequestError as exc:
        print(f"error: failed to reach CSP at {target}: {exc}", file=sys.stderr)
        sys.exit(3)

    if resp.status_code == 401:
        # CSP differentiates the failure reason in the body; surface it.
        detail = _detail(resp)
        print(f"error: bootstrap rejected by CSP — {detail}", file=sys.stderr)
        sys.exit(4)
    if resp.status_code == 404:
        print(
            f"error: agent_id={args.agent_id} not found on CSP {csp}",
            file=sys.stderr,
        )
        sys.exit(5)
    if resp.status_code >= 400:
        print(
            f"error: CSP returned {resp.status_code}: {resp.text[:200]}",
            file=sys.stderr,
        )
        sys.exit(6)

    data = resp.json()
    csk = data.get("service_token", "")
    credential_id = data.get("credential_id")
    issued_at = data.get("issued_at")
    label = data.get("label")
    if not csk:
        print("error: CSP response missing service_token", file=sys.stderr)
        sys.exit(7)

    state_path = _write_state_file(
        state_dir=Path(args.state_dir),
        token=csk,
        agent_id=args.agent_id,
        agent_endpoint=args.endpoint_url,
        csp_url=csp,
        label=label,
        credential_id=credential_id,
        issued_at=issued_at,
    )
    print(
        f"OK: service token written to {state_path} "
        f"(credential_id={credential_id}, label={label or '-'})"
    )


def _write_state_file(
    *,
    state_dir: Path,
    token: str,
    agent_id: int,
    agent_endpoint: str,
    csp_url: str,
    label: Optional[str],
    credential_id: Optional[int],
    issued_at: Optional[str],
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / STATE_FILE_NAME

    payload = {
        "token": token,
        "previous_token": None,
        "previous_expires_at": None,
        "agent_id": agent_id,
        "endpoint_url": agent_endpoint,
        "csp_url": csp_url,
        "label": label,
        "credential_id": credential_id,
        "issued_at": issued_at or datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
    }

    # Write to temp + rename for atomic update — partial writes during
    # rotation would otherwise leave the agent with an unparseable file.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 0600 — only owner reads. On Windows ``os.chmod`` ignores POSIX
    # bits but still doesn't error; the volume mount usually delivers
    # whatever ACL the host filesystem provides.
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    tmp.replace(path)
    return path


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except Exception:  # noqa: BLE001
        return resp.text[:200]
