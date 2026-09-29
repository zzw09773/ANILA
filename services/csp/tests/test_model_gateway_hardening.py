# -*- coding: utf-8 -*-
"""Slice 6a — Model Gateway Hardening(doc 04 §2/§3/§5/§8/§9)。

覆蓋:
1. alembic 單一 head = r1_0005(regex 掃描,不執行遷移)。
2. per-model secret ref write-only round trip:encrypt→resolve→GET 永不外露;
   無 ref 退回全域 env。
3. health 五態映射 + POST /test + legacy /health-check alias + GET /health。
4. url_guard kind split 在 model 註冊層:http 由旗標明確放行(PLAN.md P0.2)。
5. 出向前 ceiling 檢查:deny(不發出向,respx 零呼叫)、allow 落 decision 列
   (task-linked,或 OE-4 G4:task-less 且 level ≥ 營業秘密)、legacy 無機密
   allow 不落列、legacy latched conversation deny。

SECRET_KEY 只在本模組的測試期間以 monkeypatch 注入(function-scoped 自動還原),
不寫進 module 級 os.environ —— 避免洩漏到別的測試模組(如 test_agent_credentials
的 SECRET_KEY 缺失基準失敗)。此值非 credential_crypto 的 known-dev-secret,
故不需 ANILA_ALLOW_DEV_SECRET。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
from fastapi import HTTPException

from app.api import models as models_api
from app.models.conversation import Conversation
from app.models.model_registry import ModelRegistry
from app.models.policy_decision import PolicyDecision
from app.models.task import Task
from app.schemas.model_registry import ModelCreate
from app.services.health_checker import (
    HEALTH_DEGRADED,
    HEALTH_DISABLED,
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
    HEALTH_UNKNOWN,
    normalize_health_status,
)
from app.services.proxy.ceiling import enforce_agent_ceiling, enforce_model_ceiling
from app.services.proxy.headers import resolve_model_gateway_key
from app.services.proxy.task_link import TaskRunContext
from app.services.service_token_envelope import (
    ENVELOPE_PREFIX,
    decode_service_token_envelope,
    encode_service_token_envelope,
)
from tests.conftest import login, make_agent, make_model, make_user


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    """Function-scoped SECRET_KEY for credential_crypto; reverted after each
    test so it never leaks into other test modules' environments."""
    monkeypatch.setenv("SECRET_KEY", "slice6a-model-gateway-hardening-test-secret")
    yield


# ── 1. alembic single head, r1_ namespace (invariant, not head-pinned) ──────

def test_alembic_single_head_in_r1_namespace():
    # 不釘死特定 head id(每加一個 migration 就過期,如 Slice 8a r1_0007);
    # 守住兩個不變量:恰一個 head + head 屬 r1_ 命名空間(對齊
    # tests/test_task_trace_schema.py 的 invariant 風格)。
    versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revisions: set[str] = set()
    downs: set[str] = set()
    rev_re = re.compile(r'^revision(?::\s*str)?\s*=\s*["\']([^"\']+)["\']', re.M)
    down_re = re.compile(
        r'^down_revision(?::[^=]+)?\s*=\s*(?:["\']([^"\']+)["\']|None)', re.M
    )
    for path in versions.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = rev_re.search(text)
        assert rev, f"{path.name} 缺 revision"
        revisions.add(rev.group(1))
        down = down_re.search(text)
        if down and down.group(1):
            downs.add(down.group(1))
    heads = revisions - downs
    assert len(heads) == 1, f"alembic head 應唯一,實得 {heads}"
    assert next(iter(heads)).startswith("r1_"), (
        f"head 應屬 r1_ 命名空間,實得 {heads}"
    )


def test_r1_0005_revises_r1_0004():
    mod = (
        Path(__file__).resolve().parents[1]
        / "migrations" / "versions" / "r1_0005_model_gateway_hardening.py"
    ).read_text(encoding="utf-8")
    assert re.search(r'down_revision[^=]*=\s*["\']r1_0004["\']', mod)
    # allowed_task_types 明確不加(doc 04 §2/§11 內部不一致的拍板)。
    assert "allowed_task_types" not in mod.split("def upgrade")[1]


# ── 2. per-model secret ref write-only round trip ───────────────────────────

def test_resolve_uses_per_model_secret_ref():
    ref = encode_service_token_envelope("sk-model-key-123")
    model = SimpleNamespace(id=1, api_key_secret_ref=ref)
    assert resolve_model_gateway_key(model) == "sk-model-key-123"


def test_resolve_falls_back_to_global_env(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-global-fallback")
    model = SimpleNamespace(id=2, api_key_secret_ref=None)
    assert resolve_model_gateway_key(model) == "sk-global-fallback"


def test_resolve_none_when_no_key(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "")
    model = SimpleNamespace(id=3, api_key_secret_ref=None)
    assert resolve_model_gateway_key(model) is None


def test_unreadable_per_model_secret_does_not_use_global_key(monkeypatch, caplog):
    from fastapi import HTTPException

    from app.config import settings
    from app.database import SessionLocal
    from app.models.alert import Alert

    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-global-fallback")
    caplog.set_level("ERROR")
    model = SimpleNamespace(
        id=77,
        name="broken-model",
        api_key_secret_ref="enc::v1::not-a-real-envelope",
    )
    fingerprint = "health:model:77"
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as excinfo:
            resolve_model_gateway_key(model)
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail == "模型暫時無法使用：憑證無法讀取，請通知管理員"
        assert "sk-global-fallback" not in caplog.text

        with pytest.raises(HTTPException):
            resolve_model_gateway_key(model)
        rows = db.query(Alert).filter(Alert.fingerprint == fingerprint).all()
        assert len(rows) == 1
        assert "broken-model" in rows[0].message
        # 與健康迴圈同一筆、同一句標題，恢復時由健康迴圈結案。
        assert rows[0].title == "模型 broken-model 憑證無法讀取"
        for row in rows:
            db.delete(row)
        db.commit()
    finally:
        db.close()


def test_build_response_never_leaks_secret():
    ref = encode_service_token_envelope("sk-secret")
    model = SimpleNamespace(
        id=9, name="m", display_name="M", model_type="llm",
        endpoint_url="http://x/v1", api_version="v1", is_active=True,
        is_router_primary=False, health_status="online", health_checked_at=None,
        description=None, context_window=None, base_model_id=None, base_model=None,
        is_internal=True, api_key_secret_ref=ref, created_at=None, updated_at=None,
    )
    data = models_api._build_response(model, caller=SimpleNamespace(role="owner"))
    assert data["has_api_key"] is True
    assert "api_key" not in data
    assert "api_key_secret_ref" not in data
    assert ENVELOPE_PREFIX not in str(data)


def test_create_model_encrypts_write_only_api_key(db):
    # P4.6b: create (sets an address) is owner / designated developer only.
    owner = make_user(db, "owner6a", role="owner")
    req = ModelCreate(
        name="gw-model", display_name="GW", model_type="llm",
        endpoint_url="https://api.example.com/v1", api_key="sk-write-only",
        classification_ceiling="機密",
    )
    resp = asyncio.run(models_api.create_model(req, owner, db))
    assert resp["has_api_key"] is True
    assert resp["classification_ceiling"] == "機密"
    assert resp["protocol"] == "openai_compatible"
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "gw-model").first()
    assert row.api_key_secret_ref.startswith(ENVELOPE_PREFIX)
    assert decode_service_token_envelope(row.api_key_secret_ref) == "sk-write-only"


# ── 3. health five-state mapping + endpoints ────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("online", HEALTH_HEALTHY),
    ("connecting", HEALTH_DEGRADED),
    ("offline", HEALTH_UNHEALTHY),
    ("", HEALTH_UNKNOWN),
    (None, HEALTH_UNKNOWN),
    ("nonsense", HEALTH_UNKNOWN),
    ("healthy", HEALTH_HEALTHY),
    ("disabled", HEALTH_DISABLED),
])
def test_normalize_health_status_maps_legacy(raw, expect):
    assert normalize_health_status(raw, is_active=True) == expect


def test_normalize_disabled_when_inactive():
    assert normalize_health_status("online", is_active=False) == HEALTH_DISABLED
    assert normalize_health_status("healthy", is_active=False) == HEALTH_DISABLED


def test_get_health_maps_legacy_and_disabled(db):
    admin = make_user(db, "admin_gh", role="admin")
    m = make_model(db, name="hm")
    m.health_status = "online"
    db.commit()
    res = models_api.get_model_health(m.id, admin, db)
    assert res["status"] == HEALTH_HEALTHY
    m.is_active = False
    db.commit()
    res2 = models_api.get_model_health(m.id, admin, db)
    assert res2["status"] == HEALTH_DISABLED


def test_test_endpoint_probes_and_persists_five_state(db, monkeypatch):
    admin = make_user(db, "admin_tp", role="admin")
    m = make_model(db, name="tm")

    async def fake_probe(url, **_kwargs):
        return (HEALTH_HEALTHY, 42)

    monkeypatch.setattr(models_api, "probe_model_health_detailed", fake_probe)
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    res = asyncio.run(models_api.test_model(m.id, req, admin, db))
    assert res["status"] == HEALTH_HEALTHY
    assert res["latency_ms"] == 42
    db.refresh(m)
    assert m.health_status == HEALTH_HEALTHY


def test_legacy_health_check_alias_is_deprecated(db, monkeypatch):
    admin = make_user(db, "admin_la", role="admin")
    m = make_model(db, name="lm")

    async def fake_probe(url, **_kwargs):
        return (HEALTH_UNHEALTHY, 99)

    monkeypatch.setattr(models_api, "probe_model_health_detailed", fake_probe)
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    res = asyncio.run(models_api.trigger_health_check(m.id, req, admin, db))
    assert res["deprecated"] is True
    assert res["status"] == HEALTH_UNHEALTHY
    assert "/test" in res["detail"]
    db.refresh(m)
    assert m.health_status == HEALTH_UNHEALTHY


# ── 4. url_guard kind split @ model registration (flag-gated http) ───────────
# PLAN.md P0.2 (2026-07-29): model http is gated by ANILA_ALLOW_HTTP_ENDPOINT
# uniformly — production no longer rejects unconditionally.

def test_model_registration_allows_http_in_production_with_flag(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ENV", "production")
    models_api._enforce_endpoint_url("http://api.example.com/v1")  # no raise


def test_model_registration_rejects_http_in_production_without_flag(monkeypatch):
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    monkeypatch.setenv("ANILA_ENV", "production")
    with pytest.raises(HTTPException) as exc:
        models_api._enforce_endpoint_url("http://api.example.com/v1")
    assert exc.value.status_code == 400


def test_model_registration_allows_http_in_dev(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ENV", raising=False)
    models_api._enforce_endpoint_url("http://api.example.com/v1")  # no raise


def test_model_registration_https_ok_in_production(monkeypatch):
    monkeypatch.setenv("ANILA_ENV", "production")
    models_api._enforce_endpoint_url("https://api.example.com/v1")  # no raise


# ── 5. classification ceiling check before outbound model call ──────────────

def _caller(user):
    return SimpleNamespace(user=user, api_key_id=None)


def _model_decisions(db, model_id):
    return (
        db.query(PolicyDecision)
        .filter(
            PolicyDecision.action == "model.invoke",
            PolicyDecision.resource_id == str(model_id),
        )
        .all()
    )


def _agent_decisions(db, agent_id):
    return (
        db.query(PolicyDecision)
        .filter(
            PolicyDecision.action == "agent.invoke",
            PolicyDecision.resource_id == str(agent_id),
        )
        .all()
    )


def test_ceiling_deny_blocks_and_no_upstream_call(db):
    user = make_user(db, "u_deny")
    m = make_model(db, name="ceil_deny")
    m.classification_ceiling = "無機密"
    db.commit()
    conv = Conversation(user_id=user.id, title="latched")
    db.add(conv)
    db.commit()
    conv.classification_level = "機密"  # latched above ceiling
    db.commit()

    with respx.mock:  # any outbound httpx call would be recorded here
        with pytest.raises(HTTPException) as exc:
            enforce_model_ceiling(
                db, model=m, caller=_caller(user),
                task_ctx=None, conv_id_int=conv.id,
            )
        assert respx.calls.call_count == 0  # 出向零呼叫
    assert exc.value.status_code == 403

    denies = [d for d in _model_decisions(db, m.id) if d.decision == "deny"]
    assert len(denies) == 1
    assert denies[0].reason and "機密" in denies[0].reason


def test_ceiling_allow_task_linked_records_allow(db):
    user = make_user(db, "u_allow")
    m = make_model(db, name="ceil_allow")
    m.classification_ceiling = "機密"
    db.commit()
    task = Task(title="t", task_type="query", requester_user_id=user.id)
    db.add(task)
    db.commit()  # task defaults classification_level = 無機密
    ctx = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)

    enforce_model_ceiling(
        db, model=m, caller=_caller(user), task_ctx=ctx, conv_id_int=None,
    )
    allows = [d for d in _model_decisions(db, m.id) if d.decision == "allow"]
    assert len(allows) == 1
    assert allows[0].task_id == task.id


def test_ceiling_allow_legacy_records_no_decision_row(db):
    user = make_user(db, "u_legacy")
    m = make_model(db, name="ceil_legacy")
    m.classification_ceiling = "機密"
    db.commit()
    # legacy: no task, unclassified conversation → allow but NO decision row.
    enforce_model_ceiling(
        db, model=m, caller=_caller(user), task_ctx=None, conv_id_int=None,
    )
    assert _model_decisions(db, m.id) == []


def test_ceiling_allow_legacy_trade_secret_records_decision(db):
    """OE-4 G4:task-less allow at ≥ 營業秘密 still writes PolicyDecision."""
    user = make_user(db, "u_legacy_ts")
    m = make_model(db, name="ceil_legacy_ts")
    m.classification_ceiling = "機密"
    db.commit()
    conv = Conversation(user_id=user.id, title="ts")
    db.add(conv)
    db.commit()
    conv.classification_level = "營業秘密"
    db.commit()
    enforce_model_ceiling(
        db, model=m, caller=_caller(user), task_ctx=None, conv_id_int=conv.id,
    )
    allows = [d for d in _model_decisions(db, m.id) if d.decision == "allow"]
    assert len(allows) == 1
    assert allows[0].task_id is None


def test_ceiling_none_is_noop(db):
    user = make_user(db, "u_noceil")
    m = make_model(db, name="ceil_none")  # classification_ceiling defaults None
    db.commit()
    enforce_model_ceiling(
        db, model=m, caller=_caller(user), task_ctx=None, conv_id_int=None,
    )
    assert _model_decisions(db, m.id) == []


def test_ceiling_legacy_latched_conversation_deny(db):
    """legacy(無 task)但 conversation 已 latched 超過 ceiling → deny。"""
    user = make_user(db, "u_ll")
    m = make_model(db, name="ceil_ll")
    m.classification_ceiling = "營業秘密"
    db.commit()
    conv = Conversation(user_id=user.id, title="c")
    db.add(conv)
    db.commit()
    conv.classification_level = "機密"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        enforce_model_ceiling(
            db, model=m, caller=_caller(user), task_ctx=None, conv_id_int=conv.id,
        )
    assert exc.value.status_code == 403
    assert any(d.decision == "deny" for d in _model_decisions(db, m.id))


def test_agent_ceiling_deny_blocks_before_dispatch(db):
    owner = make_user(db, "agent_owner", role="developer")
    user = make_user(db, "agent_caller")
    agent = make_agent(db, owner, name="ceil_agent", approval_status="approved")
    agent.classification_ceiling = "無機密"
    conv = Conversation(user_id=user.id, title="classified")
    db.add(conv)
    db.commit()
    conv.classification_level = "機密"
    db.commit()

    with respx.mock:
        with pytest.raises(HTTPException) as exc:
            enforce_agent_ceiling(
                db, agent=agent, caller=_caller(user),
                task_ctx=None, conv_id_int=conv.id,
            )
        assert respx.calls.call_count == 0
    assert exc.value.status_code == 403
    denies = [d for d in _agent_decisions(db, agent.id) if d.decision == "deny"]
    assert len(denies) == 1
    assert "機密" in (denies[0].reason or "")


_UNREADABLE = "模型暫時無法使用：憑證無法讀取，請通知管理員"


def _broken_secret_model(db, name: str) -> ModelRegistry:
    model = make_model(db, name=name)
    model.endpoint_url = "https://gateway.example.com/v1"
    model.api_key_secret_ref = "enc::v1::not-a-real-envelope"
    db.commit()
    db.refresh(model)
    return model


def test_admin_probe_and_import_share_unreadable_credential_message(client, db):
    admin = make_user(db, "cred_unread_admin", role="admin")
    token = login(client, admin.username)
    headers = {"Authorization": f"Bearer {token}"}
    model = _broken_secret_model(db, "cred-unread-probe")

    probed = client.post(f"/api/models/{model.id}/test", headers=headers)
    assert probed.status_code == 503, probed.text
    assert probed.json()["detail"] == _UNREADABLE

    checked = client.post(f"/api/models/{model.id}/health-check", headers=headers)
    assert checked.status_code == 503, checked.text
    assert checked.json()["detail"] == _UNREADABLE

    imported = client.post(
        "/api/models/import",
        headers=headers,
        json={"source_model_id": model.id},
    )
    assert imported.status_code == 503, imported.text
    assert imported.json()["detail"] == _UNREADABLE


def test_health_checker_records_unreadable_credential(db):
    from app.models.alert import Alert
    from app.services.health_checker import (
        _model_probe_targets,
        apply_model_health_results,
    )

    model = _broken_secret_model(db, "cred-unread-health")
    targets = [t for t in _model_probe_targets(db) if t["model_id"] == model.id]
    assert len(targets) == 1
    assert targets[0]["credential_error"] == _UNREADABLE
    target = targets[0]
    apply_model_health_results(
        db,
        [
            (
                target["model_id"],
                target["endpoint_url"],
                target["name"],
                target["display_name"],
                target["prev_status"],
                HEALTH_UNHEALTHY,
                target["credential_error"],
            )
        ],
    )
    db.flush()
    db.refresh(model)
    assert model.health_status == HEALTH_UNHEALTHY
    alert = (
        db.query(Alert)
        .filter(Alert.fingerprint == f"health:model:{model.id}")
        .one()
    )
    assert alert.title == f"模型 {model.display_name} 憑證無法讀取"
    assert model.name in alert.message
    assert "金鑰無法解密" in alert.message


def test_streaming_chat_uses_unreadable_credential_message(client, db, monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    admin = make_user(db, "cred_unread_stream", role="admin")
    token = login(client, admin.username)
    model = make_model(db, name="cred-unread-stream")
    model.endpoint_url = "http://mock-llm:8080/v1"
    model.api_key_secret_ref = "enc::v1::not-a-real-envelope"
    db.commit()

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": model.name,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200, response.text
    assert "anila.error" in response.text
    assert _UNREADABLE in response.text
    assert "暫時無法使用" in response.text


def test_agent_ceiling_allow_task_linked_records_allow(db):
    owner = make_user(db, "agent_owner_allow", role="developer")
    user = make_user(db, "agent_caller_allow")
    agent = make_agent(db, owner, name="ceil_agent_allow", approval_status="approved")
    agent.classification_ceiling = "機密"
    task = Task(title="t", task_type="query", requester_user_id=user.id)
    db.add(task)
    db.commit()
    ctx = TaskRunContext(task_id=task.id, trace_id=task.trace_id, task_run_id=1)

    enforce_agent_ceiling(
        db, agent=agent, caller=_caller(user), task_ctx=ctx, conv_id_int=None,
    )
    allows = [d for d in _agent_decisions(db, agent.id) if d.decision == "allow"]
    assert len(allows) == 1
    assert allows[0].task_id == task.id


def test_unreadable_credential_alert_leaves_the_event_loop(monkeypatch):
    """在事件迴圈上觸發時，寫警報與寄信交給背景執行緒，且 60 秒內同一模型只送一次。"""
    import asyncio
    import threading

    from fastapi import HTTPException

    from app.services.proxy import headers as headers_mod

    written = []
    done = threading.Event()

    def fake_write(kwargs):
        written.append((threading.current_thread().name, kwargs["fingerprint"]))
        done.set()

    monkeypatch.setattr(headers_mod, "_write_credential_alert", fake_write)
    model = SimpleNamespace(
        id=78,
        name="broken-loop-model",
        api_key_secret_ref="enc::v1::not-a-real-envelope",
    )

    async def fail_twice():
        for _ in range(2):
            with pytest.raises(HTTPException):
                resolve_model_gateway_key(model)

    asyncio.run(fail_twice())
    assert done.wait(5)
    assert written == [("model-credential-alert", "health:model:78")]


def test_failed_credential_alert_write_is_retried_by_the_next_request(monkeypatch):
    """寫入失敗不佔 60 秒節流，下一個失敗的請求會再送一次。"""
    from fastapi import HTTPException

    from app.services import alert_detectors

    calls = []

    def broken_emit(**kwargs):
        calls.append(kwargs["fingerprint"])
        raise RuntimeError("ledger down")

    monkeypatch.setattr(alert_detectors, "_emit_alert_autocommit", broken_emit)
    model = SimpleNamespace(
        id=79,
        name="broken-ledger-model",
        api_key_secret_ref="enc::v1::not-a-real-envelope",
    )
    for _ in range(2):
        with pytest.raises(HTTPException):
            resolve_model_gateway_key(model)
    assert calls == ["health:model:79", "health:model:79"]
