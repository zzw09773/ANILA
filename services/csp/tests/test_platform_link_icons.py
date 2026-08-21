"""Homepage / platform-link icon allow-list.

Copies the message-actions pattern: server-owned allow-list + GET /icons
for the governance picker. Write paths (compat façade and registry) reject
unknown keys; null/empty stays allowed (column is nullable).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import login, make_user

# Picker keys advertised by the old free-text hint / PlatformCard glyphs.
# Server allow-list must at least cover these so existing stored values keep
# working; GET /icons must return exactly this set (sorted).
EXPECTED_SERVICE_ICONS = {
    "workflow",
    "git",
    "notebook",
    "chat",
    "monitor",
    "database",
    "api",
    "docs",
    "cpu",
}


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _auth(client: TestClient, db: Session, username: str, role: str = "user"):
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def test_icons_endpoint_returns_sorted_allowlist(client: TestClient, db: Session):
    user_headers = _auth(client, db, "pli_ico_u", role="user")
    admin_headers = _auth(client, db, "pli_ico_a", role="admin")

    denied = client.get("/api/platform-links/icons", headers=user_headers)
    assert denied.status_code == 403

    r = client.get("/api/platform-links/icons", headers=admin_headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, dict)
    from app.schemas.service_icon import ALLOWED_SERVICE_ICONS

    assert sorted(data["icons"]) == sorted(EXPECTED_SERVICE_ICONS)
    assert sorted(data["icons"]) == sorted(ALLOWED_SERVICE_ICONS)


def test_icons_endpoint_requires_auth(client: TestClient):
    r = client.get("/api/platform-links/icons")
    assert r.status_code in (401, 403)


def test_create_platform_link_rejects_unknown_icon(client: TestClient, db: Session):
    headers = _auth(client, db, "pli_bad_a", role="admin")
    resp = client.post(
        "/api/platform-links",
        json={
            "name": "壞圖示",
            "url": "https://bad.local",
            "icon": "not-a-real-icon",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert "未知的圖示" in resp.text


def test_create_platform_link_accepts_allowlisted_and_empty_icon(
    client: TestClient, db: Session
):
    headers = _auth(client, db, "pli_ok_a", role="admin")
    ok = client.post(
        "/api/platform-links",
        json={
            "name": "聊天入口",
            "url": "https://chat.local",
            "icon": "chat",
        },
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["icon"] == "chat"

    empty = client.post(
        "/api/platform-links",
        json={
            "name": "無圖示入口",
            "url": "https://plain.local",
            "icon": None,
        },
        headers=headers,
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["icon"] is None


def test_create_service_rejects_unknown_icon(client: TestClient, db: Session):
    headers = _auth(client, db, "pli_svc_a", role="admin")
    resp = client.post(
        "/api/services",
        json={
            "name": "壞圖示服務",
            "entry_url": "https://bad.local/app",
            "icon": "not-a-real-icon",
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert "未知的圖示" in resp.text


_REPO = Path(__file__).resolve().parents[3]


def _brace_block_after(text: str, needle: str) -> str:
    idx = text.index(needle)
    brace = text.index("{", idx)
    depth = 0
    for i, ch in enumerate(text[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace : i + 1]
    raise AssertionError(f"unclosed block after {needle}")


def _quoted_strings(block: str) -> set[str]:
    return set(re.findall(r"""["']([A-Za-z][\w-]*)["']""", block))


def _js_ident_keys(block: str) -> set[str]:
    return set(re.findall(r"(?m)^\s*([A-Za-z_][\w-]*)\s*:", block))


def test_service_icon_key_sets_stay_in_lockstep():
    """Adding a key to the backend only must go RED and name the lagging files.

    Consumers: allow-list, SPA map, dashboard GLYPHS, this file's EXPECTED set.
    """
    sources = {
        "services/csp/app/schemas/service_icon.py": _quoted_strings(
            _brace_block_after(
                (_REPO / "services/csp/app/schemas/service_icon.py").read_text(
                    encoding="utf-8"
                ),
                "ALLOWED_SERVICE_ICONS",
            )
        ),
        "apps/anila-shell/src/services.jsx": _js_ident_keys(
            _brace_block_after(
                (_REPO / "apps/anila-shell/src/services.jsx").read_text(encoding="utf-8"),
                "const SERVICE_ICONS",
            )
        ),
        "apps/csp-governance-ui/src/components/dashboard/PlatformCard.vue": _js_ident_keys(
            _brace_block_after(
                (
                    _REPO
                    / "apps/csp-governance-ui/src/components/dashboard/PlatformCard.vue"
                ).read_text(encoding="utf-8"),
                "const GLYPHS",
            )
        ),
        "services/csp/tests/test_platform_link_icons.py": _quoted_strings(
            _brace_block_after(
                Path(__file__).read_text(encoding="utf-8"),
                "EXPECTED_SERVICE_ICONS",
            )
        ),
    }
    canonical = sources["services/csp/app/schemas/service_icon.py"]
    assert canonical, "failed to parse ALLOWED_SERVICE_ICONS"

    lines: list[str] = []
    for path, keys in sources.items():
        assert keys, f"failed to parse keys from {path}"
        missing = sorted(canonical - keys)
        extra = sorted(keys - canonical)
        if missing:
            lines.append(f"{path} missing {missing}")
        if extra:
            lines.append(f"{path} extra {extra}")
    assert not lines, "icon key-set drift (lagging files named):\n" + "\n".join(lines)


def test_lockstep_failure_names_lagging_files():
    """Contract: backend-only add must name SPA / dashboard / expected-test."""
    canonical = {"workflow", "git", "new_key"}
    consumers = {
        "apps/anila-shell/src/services.jsx": {"workflow", "git"},
        "apps/csp-governance-ui/src/components/dashboard/PlatformCard.vue": {
            "workflow",
            "git",
        },
        "services/csp/tests/test_platform_link_icons.py": {"workflow", "git"},
    }
    lines = []
    for path, keys in consumers.items():
        missing = sorted(canonical - keys)
        if missing:
            lines.append(f"{path} missing {missing}")
    blob = "\n".join(lines)
    assert "apps/anila-shell/src/services.jsx missing ['new_key']" in blob
    assert (
        "apps/csp-governance-ui/src/components/dashboard/PlatformCard.vue missing ['new_key']"
        in blob
    )
    assert "services/csp/tests/test_platform_link_icons.py missing ['new_key']" in blob
