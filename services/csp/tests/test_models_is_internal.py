"""``is_internal`` flag end-to-end behaviour (unit-level).

Phase 1 (模型 stack 解耦) 加的欄位。三件事要驗:
1. ModelCreate schema 預設 ``is_internal=True`` — 新模型自動標 internal
2. ModelUpdate / ModelResponse 帶 `is_internal` (round-trip safe)
3. _build_response 對 ``is_internal=True and not owner viewer`` 回
   ``<internal>`` sentinel,不再是 ``<owner-only>``

走 unit-level test (用 dataclass-shaped stub) 不過 DB,避開既有
JSONB-on-SQLite infra debt。HTTP 路徑 end-to-end 在 cutover runbook
上手動驗 (plan §1.5)。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.schemas.model_registry import ModelCreate, ModelUpdate, ModelResponse
from app.api.models import ENDPOINT_INTERNAL, ENDPOINT_REDACTED, _build_response


# ── schema layer ───────────────────────────────────────────────────────────────


def test_model_create_defaults_is_internal_true():
    """新模型透過 API 註冊時預設 is_internal=True。
    對應 schemas/model_registry.py 的 ModelCreate.is_internal = True。"""
    payload = ModelCreate(
        name="m1",
        display_name="M1",
        model_type="llm",
        endpoint_url="https://api.example.com/v1",
    )
    assert payload.is_internal is True


def test_model_create_admin_can_untick():
    """Admin 顯式設 False (例如外網 hosted 模型)。"""
    payload = ModelCreate(
        name="m1",
        display_name="M1",
        model_type="llm",
        endpoint_url="https://api.example.com/v1",
        is_internal=False,
    )
    assert payload.is_internal is False


def test_model_update_is_internal_optional():
    """ModelUpdate 的 is_internal 是 Optional — 不提就不動到既有值。"""
    upd = ModelUpdate(display_name="renamed only")
    data = upd.model_dump(exclude_unset=True)
    assert "is_internal" not in data

    upd = ModelUpdate(is_internal=False)
    data = upd.model_dump(exclude_unset=True)
    assert data == {"is_internal": False}


# ── _build_response sentinel ───────────────────────────────────────────────────


def _row(is_internal: bool):
    """模仿 ModelRegistry ORM row 但不過 DB — _build_response 只用 attribute
    access,沒 SQLAlchemy-only API。SimpleNamespace 就夠。"""
    from datetime import datetime, timezone
    return SimpleNamespace(
        id=99,
        name="test",
        display_name="Test",
        model_type="llm",
        endpoint_url="http://gemma4:8000/v1",
        api_version="v1",
        is_active=True,
        is_router_primary=False,
        health_status="offline",
        health_checked_at=None,
        description=None,
        context_window=None,
        base_model_id=None,
        base_model=None,
        is_internal=is_internal,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def test_build_response_internal_redacts_to_internal_sentinel():
    """non-owner viewer + is_internal=True → <internal>,不是 <owner-only>。"""
    row = _row(is_internal=True)
    admin = SimpleNamespace(role="admin")  # admin 也看不到真 URL,owner 才行
    data = _build_response(row, caller=admin)
    assert data["endpoint_url"] == ENDPOINT_INTERNAL
    assert data["is_internal"] is True


def test_build_response_external_redacts_to_owner_only():
    """non-owner viewer + is_internal=False → 維持既有 <owner-only>。"""
    row = _row(is_internal=False)
    admin = SimpleNamespace(role="admin")
    data = _build_response(row, caller=admin)
    assert data["endpoint_url"] == ENDPOINT_REDACTED
    assert data["is_internal"] is False


def test_build_response_owner_sees_real_url_either_way():
    """owner 不管 is_internal 都看得到真 URL — 它是 deployment topology owner。"""
    owner = SimpleNamespace(role="owner")
    for flag in (True, False):
        row = _row(is_internal=flag)
        data = _build_response(row, caller=owner)
        assert data["endpoint_url"] == "http://gemma4:8000/v1"
        assert data["is_internal"] is flag


def test_build_response_no_caller_redacts():
    """caller=None (service-token path) 走預設 redact;若 internal 仍標明。"""
    row = _row(is_internal=True)
    data = _build_response(row, caller=None)
    assert data["endpoint_url"] == ENDPOINT_INTERNAL


def test_build_response_includes_is_internal_field():
    """ModelResponse contract: is_internal 出現在 payload,且型別 bool。"""
    row = _row(is_internal=True)
    data = _build_response(row, caller=SimpleNamespace(role="admin"))
    assert "is_internal" in data
    assert isinstance(data["is_internal"], bool)
