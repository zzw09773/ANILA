"""部署能力旗標端點 + 密等鎖定措辭(W1-3)。

三件事各自的機械判準:

1. ``GET /api/capabilities`` 回應**恰好**含 ``enable_memory`` 與
   ``enable_public_share``,且值跟著 ``settings`` 走。
2. 回應**只**有那兩個鍵 —— 白名單以外的部署資訊(profile、DB URL、版本、
   pilot 旗標…)一個都不准漏。這條是驗收會逐欄看的紅線。
3. agent 的 ``requires_encryption`` 切換所寫的稽核訊息與 API 回應,不再把
   單向密等鎖定講成「加密」——全 repo 零 at-rest 加密
   (``pgcrypto|LUKS|dm-crypt|TDE`` grep=0),舊措辭是字面不實。
   ⚠ 欄位名 ``requires_encryption`` 與 audit ``action="set_encryption"``
   **刻意不動**:前者改名是 schema 事務,後者是既有稽核列的穩定識別字,
   改了會讓歷史查詢對不上。改的只有人看的字。
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.config import settings
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.services.auth_service import create_tokens
from tests.conftest import make_agent, make_user


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


# ── 1 + 2:capabilities 端點 ────────────────────────────────────────────────

def test_capabilities_reports_both_flags(client, db: Session, monkeypatch):
    user = make_user(db, username="caps-user")
    monkeypatch.setattr(settings, "ENABLE_MEMORY", True)
    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", False)

    resp = client.get("/api/capabilities", headers=_bearer(user))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enable_memory": True, "enable_public_share": False}


def test_capabilities_follows_settings_when_both_off(client, db: Session, monkeypatch):
    user = make_user(db, username="caps-off")
    monkeypatch.setattr(settings, "ENABLE_MEMORY", False)
    monkeypatch.setattr(settings, "ENABLE_PUBLIC_SHARE", False)

    body = client.get("/api/capabilities", headers=_bearer(user)).json()

    assert body["enable_memory"] is False
    assert body["enable_public_share"] is False


def test_capabilities_leaks_nothing_outside_the_whitelist(client, db: Session):
    """白名單紅線:回應鍵集合必須**恰好**是那兩個。"""
    user = make_user(db, username="caps-whitelist")

    body = client.get("/api/capabilities", headers=_bearer(user)).json()

    assert sorted(body.keys()) == ["enable_memory", "enable_public_share"]
    serialized = repr(body)
    # 抽查幾個絕不該出現的部署事實(名稱或值)。
    for forbidden in (
        "DATABASE_URL",
        "SECRET_KEY",
        "profile",
        "ANILA_DEPLOYMENT_PROFILE",
        "version",
        "pilot",
    ):
        assert forbidden.lower() not in serialized.lower(), forbidden


def test_capabilities_requires_authentication(client):
    """未登入不得探測部署姿態。"""
    resp = client.get("/api/capabilities")
    assert resp.status_code in (401, 403), resp.text


def test_capabilities_flag_names_match_the_frontend_contract(client, db: Session):
    """前端 `runtime/capabilities.js` 的白名單與後端必須逐字一致。"""
    from app.api import capabilities as capabilities_api

    assert set(capabilities_api.CAPABILITY_FLAGS) == {
        "enable_memory",
        "enable_public_share",
    }
    user = make_user(db, username="caps-contract")
    body = client.get("/api/capabilities", headers=_bearer(user)).json()
    assert set(body.keys()) == set(capabilities_api.CAPABILITY_FLAGS)


# ── 3:密等鎖定措辭 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("requires", [True, False])
def test_set_encryption_audit_detail_uses_latch_wording(
    client, db: Session, requires: bool
):
    admin = make_user(db, username="latch-admin", role="admin")
    agent = make_agent(db, admin, name="latch-agent", approval_status="approved")

    resp = client.post(
        f"/api/agents/{agent.id}/encryption",
        json={"requires_encryption": requires},
        headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text

    # 使用者/稽核員看得到的字:不得再稱「加密」。
    assert "加密" not in resp.json()["message"], resp.json()
    assert "密等鎖定" in resp.json()["message"], resp.json()

    row = (
        db.query(AuditLog)
        .filter(AuditLog.action == "set_encryption")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None, "set_encryption 未落稽核列"
    assert "加密" not in row.detail, row.detail
    assert "密等鎖定" in row.detail, row.detail

    # 欄位語意不變 —— 這包只改字。
    db.refresh(agent)
    assert agent.requires_encryption is requires
    assert db.query(Agent).filter(Agent.id == agent.id).first().requires_encryption is requires
