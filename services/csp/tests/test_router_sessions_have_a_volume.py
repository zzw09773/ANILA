"""Router session state must outlive a container recreate (harness M2).

docker inspect on the live router showed Mounts=[]: the multi-turn dispatch
state (/v1/sessions/{id}/state and /answer) lived in ./.anila/sessions.db
inside the container and vanished on every rebuild. Pins the three pieces
that make it durable, the same way test_deploy_scripts_use_project_image_name
pins deployment facts: compose declares the volume and the path, the image
creates the directory before chown so the named volume inherits ownership.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = (ROOT / "infra" / "compose" / "platform.yml").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "services" / "anila-core-router" / "Dockerfile").read_text(encoding="utf-8")


def _router_block() -> str:
    m = re.search(r"\n  router:\n(.*?)\n  [a-z]", COMPOSE, re.S)
    assert m, "router service block not found"
    return m.group(1)


def test_router_mounts_a_named_volume_for_sessions():
    block = _router_block()
    assert re.search(r"volumes:\s*\n\s*- router-sessions:/app/\.anila", block), block
    assert "ANILA_SESSION_DB_PATH: /app/.anila/sessions.db" in block


def test_named_volume_is_declared_at_top_level():
    assert re.search(r"^volumes:\n(?:.*\n)*?  router-sessions:\n", COMPOSE, re.M), "router-sessions volume missing"


def test_image_creates_the_directory_before_chown():
    """Kill: drop the mkdir → the named volume is created root-owned and the
    router (uid 1000) cannot write its SQLite file."""
    mk = DOCKERFILE.find("mkdir -p /app/.anila")
    ch = DOCKERFILE.find("chown -R anila:anila /app")
    assert mk != -1 and ch != -1 and mk < ch
