"""治理頁 API 與即時生效契約。"""

from __future__ import annotations

from datetime import datetime, timezone

from jose import jwt

from app.models.audit_log import AuditLog
from app.models.platform_setting import PlatformSetting
from app.services.settings_registry import SETTINGS
from app.utils.security import create_access_token
from tests.conftest import login, make_user


OVERVIEW_URL = "/api/platform-settings/overview"
KEEP_KEYS = [
    "institutional_kb.score_threshold",
    "memory.retrieve_min_cosine",
    "memory.retrieve_top_k",
    "proxy.llm_timeout",
    "proxy.embedding_timeout",
    "auth.access_token_expire_minutes",
    "auth.refresh_token_expire_days",
    "limits.department_max_depth",
    "limits.action_invoke_per_min",
    "limits.attachment_budget_ratio",
    "intl.zh_normalize",
    "intl.query_expansion",
]


def _admin_headers(client, db) -> dict[str, str]:
    make_user(db, username="settings-admin", role="admin")
    return {"Authorization": f"Bearer {login(client, 'settings-admin')}"}


def test_overview_is_exactly_twelve_immediate_c_rows(client, db):
    response = client.get(OVERVIEW_URL, headers=_admin_headers(client, db))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 12
    assert [item["key"] for item in body["items"]] == KEEP_KEYS
    assert all(item["class"] == "C" and item["editable"] for item in body["items"])
    assert all("restart_required" not in item for item in body["items"])
    assert "boot_override" not in body
    assert {key for key in body} == {"total", "items"}


def test_update_persists_immediately_and_audits(client, db):
    headers = _admin_headers(client, db)
    response = client.put(
        "/api/platform-settings/proxy.llm_timeout",
        json={"value": "77"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["effective"] == 77
    assert body["stored"] == "77"
    assert body["source"] == "db"
    assert db.get(PlatformSetting, "proxy.llm_timeout").value == "77"
    audit = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "platform_setting_set",
            AuditLog.resource_id == "proxy.llm_timeout",
        )
        .one()
    )
    assert audit.status == "success"


def test_update_uses_one_boolean_contract(client, db):
    headers = _admin_headers(client, db)
    response = client.put(
        "/api/platform-settings/intl.zh_normalize",
        json={"value": "false"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["effective"] is False


def test_invalid_value_is_rejected_without_persisting(client, db):
    headers = _admin_headers(client, db)
    response = client.put(
        "/api/platform-settings/proxy.llm_timeout",
        json={"value": "0"},
        headers=headers,
    )
    assert response.status_code == 400
    assert db.get(PlatformSetting, "proxy.llm_timeout") is None


def test_unknown_key_is_not_a_hidden_new_setting(client, db):
    response = client.put(
        "/api/platform-settings/not-a-setting",
        json={"value": "1"},
        headers=_admin_headers(client, db),
    )
    assert response.status_code == 404


def test_non_admin_cannot_read_or_write(client, db):
    make_user(db, username="settings-user", role="developer")
    headers = {"Authorization": f"Bearer {login(client, 'settings-user')}"}
    assert client.get(OVERVIEW_URL, headers=headers).status_code == 403
    assert client.put(
        "/api/platform-settings/proxy.llm_timeout",
        json={"value": "77"},
        headers=headers,
    ).status_code == 403


def test_token_lifetime_is_read_at_issuance_and_old_exp_is_embedded(db):
    now = datetime.now(timezone.utc).timestamp()
    db.add(PlatformSetting(key="auth.access_token_expire_minutes", value="7"))
    db.commit()
    first = jwt.get_unverified_claims(create_access_token({"sub": "1"}, db=db))
    assert 6 * 60 < first["exp"] - now < 8 * 60

    db.get(PlatformSetting, "auth.access_token_expire_minutes").value = "13"
    db.commit()
    second = jwt.get_unverified_claims(create_access_token({"sub": "1"}, db=db))
    # jose serialises datetimes to whole seconds. The two calls can straddle
    # either edge of a second, so allow that encoding jitter while preserving
    # the six-minute change: an old-value mutation produces ~0 seconds here.
    assert 6 * 60 - 1 <= second["exp"] - first["exp"] <= 6 * 60 + 1
    # Verification uses the exp claim already present in first; changing the
    # setting does not rewrite an already-issued token.
    assert first["exp"] < second["exp"]


def test_registry_has_same_count_as_api_contract():
    assert len(SETTINGS) == len(KEEP_KEYS) == 12
