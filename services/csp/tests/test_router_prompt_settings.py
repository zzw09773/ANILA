"""Router's three system prompts are platform settings (owner ruling 2026-08-22).

queued-fix-router-prompt-ui-knob invariants covered on the csp side:
  ② shipped default = the verbatim text in anila-core; overview shows the
     effective full text; resetting to default is one PUT with that value;
  ③ every change leaves an audit row with the previous text;
  ⑥ only platform admins can read/write the full text through the admin API;
     the router reads it through the service-to-service endpoint with a
     router-kind service token, and nothing else can.
  ⑦ this package does not change a single character of the prompts —
     asserted by comparing the default with anila-core's constant.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from anila_core.api import router_prompts as rp

from app.models.audit_log import AuditLog
from app.models.platform_setting import PlatformSetting
from app.models.service_client import ServiceClient
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from app.services.settings_registry import REGISTRY, SETTINGS
from tests.conftest import login, make_user

OVERVIEW = "/api/platform-settings/overview"
S2S = "/api/router-prompts"


def _admin(client, db, name="prompt-admin"):
    make_user(db, username=name, role="admin")
    return {"Authorization": f"Bearer {login(client, name)}"}


def _user(client, db, name="prompt-user"):
    make_user(db, username=name, role="user")
    return {"Authorization": f"Bearer {login(client, name)}"}


def _router_token(db) -> str:
    token = generate_service_token()
    db.add(
        ServiceClient(
            client_name="prompt-test-router",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
            service_token_issued_at=datetime.now(timezone.utc),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()
    return token


# ── registry ───────────────────────────────────────────────────────────────


def test_three_prompt_settings_are_registered_with_verbatim_defaults():
    keys = [s.key for s in SETTINGS]
    for key in (rp.KEY_SYSTEM, rp.KEY_PLAIN, rp.KEY_FORCED):
        assert key in keys, key
        assert REGISTRY[key].value_type.name == "text"
        assert REGISTRY[key].env_name is None
    # ⑦ — not one character changed: the default IS anila-core's constant.
    assert REGISTRY[rp.KEY_SYSTEM].default == rp.DEFAULT_ROUTER_SYSTEM
    assert REGISTRY[rp.KEY_PLAIN].default == rp.DEFAULT_PLAIN_ASSISTANT
    assert REGISTRY[rp.KEY_FORCED].default == rp.DEFAULT_FORCED_ANSWER


def test_prompt_descriptions_answer_the_settings_page_threshold():
    """⑤ — who / when / why-not-wait must be written next to the field."""
    for key in (rp.KEY_SYSTEM, rp.KEY_PLAIN, rp.KEY_FORCED):
        desc = REGISTRY[key].description
        assert "平台管理員" in desc
        assert "營運" in desc
        assert "不能等改版" in desc or "高頻" in desc


# ── admin API ───────────────────────────────────────────────────────────────


def test_overview_shows_effective_full_text_and_default(client, db):
    resp = client.get(OVERVIEW, headers=_admin(client, db))
    assert resp.status_code == 200, resp.text
    items = {i["key"]: i for i in resp.json()["items"]}
    item = items[rp.KEY_SYSTEM]
    assert item["effective"] == rp.DEFAULT_ROUTER_SYSTEM
    assert item["default"] == rp.DEFAULT_ROUTER_SYSTEM
    assert item["source"] == "default"
    assert item["value_type"] == "text"


def test_admin_update_persists_and_audits_previous_text(client, db):
    headers = _admin(client, db)
    new_text = rp.DEFAULT_PLAIN_ASSISTANT + "\n（本次修改）"
    resp = client.put(f"/api/platform-settings/{rp.KEY_PLAIN}", json={"value": new_text}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective"] == new_text
    assert resp.json()["source"] == "db"
    row = db.get(PlatformSetting, rp.KEY_PLAIN)
    assert row is not None and row.value == new_text
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "platform_setting_set", AuditLog.resource_id == rp.KEY_PLAIN)
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    # ③ — the previous full text is in the audit row, so it can be restored.
    import json
    meta = json.loads(audit.metadata_json or "{}")
    assert meta.get("from") == rp.DEFAULT_PLAIN_ASSISTANT
    assert meta.get("to") == new_text


def test_reset_to_shipped_default_is_a_put_of_the_default(client, db):
    headers = _admin(client, db)
    client.put(f"/api/platform-settings/{rp.KEY_FORCED}", json={"value": "changed"}, headers=headers)
    resp = client.put(
        f"/api/platform-settings/{rp.KEY_FORCED}",
        json={"value": rp.DEFAULT_FORCED_ANSWER},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective"] == rp.DEFAULT_FORCED_ANSWER


def test_system_prompt_must_keep_the_agent_list_placeholder(client, db):
    headers = _admin(client, db)
    resp = client.put(
        f"/api/platform-settings/{rp.KEY_SYSTEM}",
        json={"value": "沒有代理清單佔位的模板"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text
    assert "{agent_list}" in resp.json()["detail"]
    # a stray brace would make str.format explode per request → refused too
    resp = client.put(
        f"/api/platform-settings/{rp.KEY_SYSTEM}",
        json={"value": "{agent_list} and {unknown}"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


def test_empty_prompt_is_refused(client, db):
    headers = _admin(client, db)
    resp = client.put(f"/api/platform-settings/{rp.KEY_PLAIN}", json={"value": "   "}, headers=headers)
    assert resp.status_code == 400, resp.text


def test_non_admin_cannot_read_or_write_prompt_text(client, db):
    headers = _user(client, db)
    assert client.get(OVERVIEW, headers=headers).status_code == 403
    resp = client.put(f"/api/platform-settings/{rp.KEY_PLAIN}", json={"value": "x"}, headers=headers)
    assert resp.status_code == 403


# ── service-to-service endpoint (what the router reads) ────────────────────


def test_router_service_token_reads_effective_prompts(client, db):
    token = _router_token(db)
    resp = client.get(S2S, headers={"X-CSP-Service-Token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["prompts"][rp.KEY_SYSTEM] == rp.DEFAULT_ROUTER_SYSTEM
    assert body["source"][rp.KEY_SYSTEM] == "default"

    headers = _admin(client, db)
    client.put(f"/api/platform-settings/{rp.KEY_PLAIN}", json={"value": "營運期口氣"}, headers=headers)
    resp = client.get(S2S, headers={"X-CSP-Service-Token": token})
    assert resp.json()["prompts"][rp.KEY_PLAIN] == "營運期口氣"
    assert resp.json()["source"][rp.KEY_PLAIN] == "db"


def test_s2s_endpoint_refuses_missing_token_and_admin_jwt(client, db):
    assert client.get(S2S).status_code == 401
    # an admin browser session is not a service principal
    assert client.get(S2S, headers=_admin(client, db)).status_code in (401, 403)


def test_s2s_endpoint_refuses_non_router_service_client(client, db):
    token = generate_service_token()
    db.add(
        ServiceClient(
            client_name="prompt-test-worker",
            client_type="worker",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
            service_token_issued_at=datetime.now(timezone.utc),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()
    assert client.get(S2S, headers={"X-CSP-Service-Token": token}).status_code == 403
