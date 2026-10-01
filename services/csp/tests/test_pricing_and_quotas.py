"""模型單價、成本報表、用量額度。

預設沒有單價、沒有上限。成本用呼叫當時生效的價格；未計價不是 0。
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.handoff import Notification
from app.models.model_price import ModelPrice
from app.models.token_usage import TokenUsage
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.usage_quota import QuotaEvent, UsageQuota
from app.services.auth_service import create_tokens
from app.services.pricing import row_cost_micros, PriceBook
from app.services.quota_redis import (
    DownQuotaRedis,
    MemoryQuotaRedis,
    reset_quota_redis,
    set_quota_redis,
)
from app.services.quota_service import (
    bump_committed_usage,
    enforce_call_quota,
    period_window,
)
from app.services.usage_service import (
    ExportRangeError,
    export_usage_csv,
    get_usage_by_api_key,
    get_usage_by_unit,
    get_usage_summary,
    resolve_export_window,
)
from app.services import usage_writer as uw
from anila_core.api.quota_passthrough import quota_exceeded_message
from tests.conftest import login, make_api_key, make_model, make_user

_TPE = timezone(timedelta(hours=8))
_UTC = timezone.utc


@pytest.fixture(autouse=True)
def _isolated_quota_redis():
    reset_quota_redis()
    yield
    reset_quota_redis()


def _bearer(user) -> dict:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _dept(db: Session, name: str, parent_id: int | None = None) -> Department:
    dept = Department(name=name, parent_id=parent_id, is_active=True)
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


def _price(db: Session, model_id: int, when: datetime, *, input_per_million: int | None):
    db.add(
        ModelPrice(
            model_id=model_id,
            input_micros=None if input_per_million is None else input_per_million * 1_000_000,
            output_micros=None if input_per_million is None else input_per_million * 1_000_000,
            reasoning_micros=None,
            effective_at=when,
        )
    )
    db.commit()


def _usage(
    db: Session,
    *,
    user,
    model,
    when: datetime,
    prompt: int = 0,
    completion: int = 0,
    total: int | None = None,
    reasoning: int | None = None,
    api_key_id: int | None = None,
    department_id: int | None = None,
    request_type: str = "chat",
    usage_kind: str = "inference",
) -> TokenUsage:
    row = TokenUsage(
        api_key_id=api_key_id,
        user_id=user.id,
        department_id=department_id if department_id is not None else user.department_id,
        model_id=model.id,
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        total_tokens=total if total is not None else prompt + completion,
        request_timestamp=when,
        request_type=request_type,
        usage_kind=usage_kind,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _quota(
    db: Session,
    *,
    scope_type: str,
    period: str = "daily",
    metric: str = "tokens",
    limit_value: int | None = 100,
    on_limit: str = "block",
    warn_percent: int = 80,
    user_id: int | None = None,
    department_id: int | None = None,
    api_key_id: int | None = None,
) -> UsageQuota:
    row = UsageQuota(
        scope_type=scope_type,
        user_id=user_id,
        department_id=department_id,
        api_key_id=api_key_id,
        period=period,
        metric=metric,
        limit_value=limit_value,
        warn_percent=warn_percent,
        on_limit=on_limit,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _blocked(user, **kwargs) -> str | None:
    try:
        enforce_call_quota(
            kwargs.pop("db"),
            user_id=user.id,
            department_id=user.department_id,
            api_key_id=kwargs.pop("api_key_id", None),
            usage_kind=kwargs.pop("usage_kind", "inference"),
            now=kwargs.pop("now", None),
        )
    except HTTPException as exc:
        assert exc.status_code == 429
        detail = exc.detail
        assert detail["code"] == "quota_exceeded"
        return detail["message"]
    return None


def test_cost_uses_price_effective_at_request_time(db: Session):
    now = datetime.now(_UTC)
    user = make_user(db, username="price-history")
    model = make_model(db, name="priced-history")
    _price(db, model.id, now - timedelta(days=20), input_per_million=2)
    _price(db, model.id, now - timedelta(days=5), input_per_million=5)
    _usage(db, user=user, model=model, when=now - timedelta(days=10), prompt=1_000_000)
    _usage(db, user=user, model=model, when=now - timedelta(days=1), prompt=1_000_000)

    summary = get_usage_summary(db, range_key="30d")
    assert summary["cost"] == "7"
    assert summary["cost_state"] == "priced"
    assert summary["cost_currency"] == "TWD"
    assert db.query(ModelPrice).count() == 2


def test_unpriced_is_not_zero(db: Session):
    now = datetime.now(_UTC)
    user = make_user(db, username="unpriced-user")
    bare = make_model(db, name="bare-model")
    priced = make_model(db, name="half-priced")
    _usage(db, user=user, model=bare, when=now, prompt=1_000_000, total=1_000_000)
    summary = get_usage_summary(db, range_key="24h")
    assert summary["cost"] is None
    assert summary["cost_state"] == "unpriced"
    assert summary["total_tokens"] == 1_000_000

    text = export_usage_csv(db, "24h")
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert rows[0][-1] == "成本"
    assert rows[1][-1] == "未計價"
    assert rows[1][-1] != "0"

    _price(db, priced.id, now - timedelta(days=1), input_per_million=3)
    _usage(db, user=user, model=priced, when=now, prompt=1_000_000)
    mixed = get_usage_summary(db, range_key="24h")
    assert mixed["cost_state"] == "mixed"
    assert mixed["cost"] == "3"
    mixed_csv = list(csv.reader(io.StringIO(export_usage_csv(db, "24h").lstrip("\ufeff"))))
    costs = {row[-1] for row in mixed_csv[1:]}
    assert "未計價" in costs
    assert "3" in costs


def test_reasoning_price_falls_back_to_output_price(db: Session):
    now = datetime.now(_UTC)
    user = make_user(db, username="reason-price")
    model = make_model(db, name="reason-model")
    db.add(
        ModelPrice(
            model_id=model.id,
            input_micros=1_000_000,
            output_micros=4_000_000,
            reasoning_micros=None,
            effective_at=now - timedelta(days=1),
        )
    )
    db.commit()
    row = _usage(
        db,
        user=user,
        model=model,
        when=now,
        prompt=1_000_000,
        completion=1_000_000,
        reasoning=1_000_000,
        total=2_000_000,
    )
    # 輸入 1 + 思考沿用輸出價 4。輸出 token 已扣掉思考，不再加一次。
    assert row_cost_micros(row, PriceBook.load(db)) == 5_000_000


def test_user_quota_blocks_and_warns(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    now = datetime.now(_UTC)
    user = make_user(db, username="quota-user")
    model = make_model(db, name="quota-model")
    _usage(db, user=user, model=model, when=now, total=80, prompt=80)
    quota = _quota(
        db,
        scope_type="user",
        user_id=user.id,
        limit_value=100,
        on_limit="warn",
        warn_percent=80,
    )
    assert _blocked(user, db=db) is None
    note = db.query(Notification).filter(Notification.user_id == user.id).one()
    assert note.type == "quota_warn"
    assert "80%" in note.body

    db.delete(note)
    quota.limit_value = 80
    quota.on_limit = "warn"
    db.commit()
    reset_quota_redis()
    set_quota_redis(MemoryQuotaRedis())
    assert _blocked(user, db=db) is None
    limit_note = db.query(Notification).filter(Notification.type == "quota_limit").one()
    assert "已達上限" in limit_note.title

    quota.on_limit = "block"
    db.commit()
    reset_quota_redis()
    set_quota_redis(MemoryQuotaRedis())
    message = _blocked(user, db=db)
    assert message is not None
    assert message.startswith("已達使用者每日 token 上限（80）")
    assert "台北時間" in message
    assert db.query(QuotaEvent).count() == 1


def test_unit_quota_includes_descendants(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    parent = _dept(db, "資通所")
    child = _dept(db, "企劃組", parent.id)
    other = _dept(db, "別所")
    user = make_user(db, username="child-user", department_id=child.id)
    outsider = make_user(db, username="other-user", department_id=other.id)
    model = make_model(db, name="unit-model")
    now = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=now, total=30, department_id=child.id)
    _usage(db, user=outsider, model=model, when=now, total=500, department_id=other.id)
    _quota(db, scope_type="unit", department_id=parent.id, limit_value=20, on_limit="block")

    message = _blocked(user, db=db)
    assert message is not None
    assert "單位「資通所」" in message
    assert _blocked(outsider, db=db) is None

    rows = get_usage_by_unit(db, range_key="24h")
    parent_row = next(item for item in rows if item["department_id"] == parent.id)
    child_row = next(item for item in rows if item["department_id"] == child.id)
    assert parent_row["label"] == "資通所（含下層）"
    assert parent_row["total_tokens"] == 30
    assert child_row["total_tokens"] == 30


def test_api_key_quota(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="key-user")
    key = make_api_key(db, user, raw_key="sk-quota-key1")
    model = make_model(db, name="key-model")
    now = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=now, total=40, api_key_id=key.id)
    _usage(db, user=user, model=model, when=now - timedelta(minutes=1), total=90, api_key_id=None)
    _quota(db, scope_type="api_key", api_key_id=key.id, limit_value=30, on_limit="block")

    message = _blocked(user, db=db, api_key_id=key.id)
    assert message is not None
    assert "API 金鑰「test-key」" in message
    assert _blocked(user, db=db, api_key_id=None) is None

    cuts = get_usage_by_api_key(db, range_key="24h")
    labels = {item["label"]: item["total_tokens"] for item in cuts}
    assert labels["test-key"] == 40
    assert labels["網頁"] == 90


def test_any_blocking_quota_blocks(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    parent = _dept(db, "院部")
    user = make_user(db, username="multi-user", department_id=parent.id)
    model = make_model(db, name="multi-model")
    now = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=now, total=50)
    _quota(
        db,
        scope_type="user",
        user_id=user.id,
        limit_value=10,
        on_limit="warn",
    )
    _quota(
        db,
        scope_type="unit",
        department_id=parent.id,
        limit_value=40,
        on_limit="block",
    )
    message = _blocked(user, db=db)
    assert message is not None
    assert "單位「院部」" in message

    # 使用者那條改成擋下、單位改成只提醒且還沒到：仍因使用者那條擋下。
    for quota in db.query(UsageQuota).all():
        if quota.scope_type == "user":
            quota.on_limit = "block"
            quota.limit_value = 40
        else:
            quota.on_limit = "warn"
            quota.limit_value = 10_000
    db.commit()
    reset_quota_redis()
    set_quota_redis(MemoryQuotaRedis())
    message = _blocked(user, db=db)
    assert message is not None
    assert message.startswith("已達使用者")


def test_redis_down_allows(db: Session, caplog):
    set_quota_redis(DownQuotaRedis())
    user = make_user(db, username="redis-down")
    model = make_model(db, name="redis-model")
    _usage(db, user=user, model=model, when=datetime.now(_UTC), total=999)
    _quota(db, scope_type="user", user_id=user.id, limit_value=1, on_limit="block")
    with caplog.at_level("WARNING"):
        assert _blocked(user, db=db) is None
    assert "額度計數暫不可用，本次放行" in caplog.text
    assert db.query(QuotaEvent).count() == 0


def test_counter_rebuilds_when_missing(db: Session):
    redis = MemoryQuotaRedis()
    set_quota_redis(redis)
    user = make_user(db, username="rebuild-user")
    model = make_model(db, name="rebuild-model")
    when = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=when, total=70)
    quota = _quota(db, scope_type="user", user_id=user.id, limit_value=50, on_limit="block")
    assert redis.store == {}
    message = _blocked(user, db=db, now=when)
    assert message is not None
    _start, _end, period_key = period_window("daily", when)
    key = f"anila:quota:v1:daily:{period_key}:user:{user.id}:tokens"
    assert redis.store[key] == "70"

    redis.store.clear()
    bump_committed_usage(
        db,
        [
            {
                "user_id": user.id,
                "department_id": None,
                "api_key_id": None,
                "model_id": model.id,
                "total_tokens": 70,
                "prompt_tokens": 70,
                "completion_tokens": 0,
                "request_timestamp": when,
                "usage_kind": "inference",
            }
        ],
    )
    assert redis.store[key] == "70"
    assert quota.id


def test_period_rollover_uses_taipei(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="rollover-user")
    model = make_model(db, name="rollover-model")
    late = datetime(2026, 10, 1, 23, 30, tzinfo=_TPE)
    morning = datetime(2026, 10, 2, 0, 30, tzinfo=_TPE)
    _usage(db, user=user, model=model, when=late.astimezone(_UTC), total=500)
    _quota(db, scope_type="user", user_id=user.id, limit_value=100, on_limit="block", period="daily")

    blocked = _blocked(user, db=db, now=late)
    assert blocked is not None
    assert "2026-10-02 00:00（台北時間）" in blocked
    reset_quota_redis()
    set_quota_redis(MemoryQuotaRedis())
    assert _blocked(user, db=db, now=morning) is None

    daily = db.query(UsageQuota).filter(UsageQuota.period == "daily").one()
    daily.limit_value = None
    db.query(TokenUsage).delete()
    db.commit()
    month_quota = _quota(
        db,
        scope_type="user",
        user_id=user.id,
        limit_value=100,
        on_limit="block",
        period="monthly",
    )
    september = datetime(2026, 9, 30, 12, 0, tzinfo=_TPE)
    october = datetime(2026, 10, 1, 1, 0, tzinfo=_TPE)
    _usage(db, user=user, model=model, when=september.astimezone(_UTC), total=400)
    reset_quota_redis()
    set_quota_redis(MemoryQuotaRedis())
    # 每日那條在十月已是新的一天，用量是九月的，不會擋。每月仍算九月。
    month_quota.limit_value = 100
    db.commit()
    assert _blocked(user, db=db, now=september) is not None
    reset_quota_redis()
    set_quota_redis(MemoryQuotaRedis())
    # 十月一日：九月的每月用量不計入；每日用量也不在今天。
    assert _blocked(user, db=db, now=october) is None


def test_platform_usage_kind_is_not_checked(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="platform-kind")
    model = make_model(db, name="platform-model")
    when = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=when, total=500, usage_kind="platform")
    _quota(db, scope_type="user", user_id=user.id, limit_value=1, on_limit="block")
    assert _blocked(user, db=db, usage_kind="platform") is None
    assert _blocked(user, db=db, usage_kind="router_transport") is None
    # 平台列不進計數，所以一般呼叫也不會被那 500 顆擋下。
    assert _blocked(user, db=db, usage_kind="inference") is None


def test_limit_zero_blocks_immediately(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="zero-limit")
    _quota(db, scope_type="user", user_id=user.id, limit_value=0, on_limit="block")
    message = _blocked(user, db=db)
    assert message is not None
    assert "上限（0）" in message


def test_unlimited_quota_is_not_enforced(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="unlimited")
    model = make_model(db, name="unlimited-model")
    _usage(db, user=user, model=model, when=datetime.now(_UTC), total=10_000)
    _quota(db, scope_type="user", user_id=user.id, limit_value=None, on_limit="block")
    assert _blocked(user, db=db) is None


def test_unit_admin_can_read_but_not_write(client: TestClient, db: Session):
    parent = _dept(db, "綁定所")
    child = _dept(db, "綁定組", parent.id)
    outside = _dept(db, "所外")
    admin = make_user(db, username="quota-admin", role="admin")
    unit_admin = make_user(db, username="unit-reader", department_id=parent.id)
    inside = make_user(db, username="inside-user", department_id=child.id)
    outside_user = make_user(db, username="outside-user", department_id=outside.id)
    db.add(UnitAdminAssignment(user_id=unit_admin.id, department_id=parent.id, granted_by=admin.id))
    db.commit()
    headers = {"Authorization": f"Bearer {login(client, 'quota-admin')}"}
    unit_headers = {"Authorization": f"Bearer {login(client, 'unit-reader')}"}

    created = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "unit",
            "department_id": parent.id,
            "period": "monthly",
            "metric": "tokens",
            "limit_value": 1000,
            "on_limit": "warn",
        },
    )
    assert created.status_code == 200, created.text
    inside_user_quota = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "user",
            "user_id": inside.id,
            "period": "daily",
            "metric": "tokens",
            "limit_value": 10,
            "on_limit": "block",
        },
    )
    assert inside_user_quota.status_code == 200, inside_user_quota.text
    hidden = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "unit",
            "department_id": outside.id,
            "period": "daily",
            "metric": "tokens",
            "limit_value": 10,
            "on_limit": "block",
        },
    )
    assert hidden.status_code == 200, hidden.text
    ancestor_only = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "user",
            "user_id": outside_user.id,
            "period": "daily",
            "metric": "tokens",
            "limit_value": 10,
            "on_limit": "warn",
        },
    )
    assert ancestor_only.status_code == 200, ancestor_only.text

    listed = client.get("/api/quotas", headers=unit_headers)
    assert listed.status_code == 200, listed.text
    seen = {item["id"] for item in listed.json()}
    assert created.json()["id"] in seen
    assert inside_user_quota.json()["id"] in seen
    assert hidden.json()["id"] not in seen
    assert ancestor_only.json()["id"] not in seen

    denied = client.post(
        "/api/quotas",
        headers=unit_headers,
        json={
            "scope_type": "user",
            "user_id": inside.id,
            "period": "monthly",
            "metric": "tokens",
            "limit_value": 1,
            "on_limit": "block",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "單位管理員只能查看額度，不能修改"
    updated = client.put(
        f"/api/quotas/{created.json()['id']}",
        headers=unit_headers,
        json={"on_limit": "block"},
    )
    assert updated.status_code == 403
    deleted = client.delete(f"/api/quotas/{created.json()['id']}", headers=unit_headers)
    assert deleted.status_code == 403


def test_embeddings_429_names_quota_and_reset(client: TestClient, db: Session):
    set_quota_redis(MemoryQuotaRedis())
    admin = make_user(db, username="embed-admin", role="admin")
    model = make_model(db, name="embed-quota-model")
    _usage(db, user=admin, model=model, when=datetime.now(_UTC), total=10_000)
    _quota(db, scope_type="user", user_id=admin.id, limit_value=10_000, on_limit="block")
    resp = client.post(
        "/v1/embeddings",
        headers=_bearer(admin),
        json={"model": model.name, "input": "hi"},
    )
    assert resp.status_code == 429, resp.text
    body = resp.json()["detail"]
    assert body["code"] == "quota_exceeded"
    assert body["message"].startswith("已達使用者每日 token 上限（10,000）")
    assert "（台北時間）重置" in body["message"]
    db.expire_all()
    event = db.query(QuotaEvent).one()
    assert event.user_id == admin.id
    assert event.message == body["message"]


def test_export_range_limits(client: TestClient, db: Session):
    with pytest.raises(ExportRangeError, match="單次匯出最長一年"):
        resolve_export_window("24h", start="2024-01-01", end="2025-01-02")
    with pytest.raises(ExportRangeError, match="單次匯出最長一年"):
        resolve_export_window("24h", start="2024-01-01", end="2025-01-01")
    start, end = resolve_export_window("24h", start="2024-01-01", end="2024-12-31")
    assert start == datetime(2024, 1, 1, tzinfo=_TPE)
    assert end == datetime(2025, 1, 1, tzinfo=_TPE)
    with pytest.raises(ExportRangeError, match="快捷鍵與自訂日期請擇一"):
        resolve_export_window("24h", preset="this_month", start="2024-01-01", end="2024-01-02")
    with pytest.raises(ExportRangeError, match="請同時提供開始與結束日期"):
        resolve_export_window("24h", start="2024-01-01")
    with pytest.raises(ExportRangeError, match="結束日期不可早於開始日期"):
        resolve_export_window("24h", start="2024-02-02", end="2024-02-01")
    with pytest.raises(ExportRangeError, match="日期格式須為 YYYY-MM-DD"):
        resolve_export_window("24h", start="2024/01/01", end="2024-01-02")

    october = datetime(2026, 10, 15, 3, 0, tzinfo=_UTC)
    month_start, month_end = resolve_export_window("24h", preset="this_month", now=october)
    assert month_start == datetime(2026, 10, 1, tzinfo=_TPE)
    assert month_end == datetime(2026, 11, 1, tzinfo=_TPE)
    last_start, last_end = resolve_export_window("24h", preset="last_month", now=october)
    assert last_start == datetime(2026, 9, 1, tzinfo=_TPE)
    assert last_end == datetime(2026, 10, 1, tzinfo=_TPE)
    q_start, q_end = resolve_export_window("24h", preset="this_quarter", now=october)
    assert q_start == datetime(2026, 10, 1, tzinfo=_TPE)
    assert q_end == datetime(2027, 1, 1, tzinfo=_TPE)

    make_user(db, username="export-admin", role="admin")
    headers = {"Authorization": f"Bearer {login(client, 'export-admin')}"}
    too_long = client.get(
        "/api/usage/export",
        headers=headers,
        params={"start": "2024-01-01", "end": "2025-01-01"},
    )
    assert too_long.status_code == 400
    assert too_long.json()["detail"] == "單次匯出最長一年"
    ok = client.get(
        "/api/usage/export",
        headers=headers,
        params={"start": "2024-01-01", "end": "2024-12-31"},
    )
    assert ok.status_code == 200, ok.text
    assert "成本" in ok.text


def test_price_and_quota_changes_are_audited(client: TestClient, db: Session):
    admin = make_user(db, username="audit-admin", role="admin")
    target = make_user(db, username="audit-target")
    model = make_model(db, name="audit-model")
    headers = {"Authorization": f"Bearer {login(client, 'audit-admin')}"}

    currency = client.get("/api/billing/currency", headers=headers)
    assert currency.status_code == 200
    assert currency.json()["currency"] == "TWD"

    money = client.put("/api/billing/currency", headers=headers, json={"currency": "usd"})
    assert money.status_code == 200
    assert money.json()["currency"] == "USD"
    bad_money = client.put("/api/billing/currency", headers=headers, json={"currency": "US"})
    assert bad_money.status_code == 422

    moment = datetime.now(_UTC).replace(microsecond=0)
    older = (moment - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newer = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    first = client.post(
        f"/api/models/{model.id}/prices",
        headers=headers,
        json={"input_per_million": "2", "output_per_million": "4", "effective_at": older},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/models/{model.id}/prices",
        headers=headers,
        json={"input_per_million": "5", "output_per_million": "9", "effective_at": newer},
    )
    assert second.status_code == 200, second.text
    history = client.get(f"/api/models/{model.id}/prices", headers=headers)
    assert history.status_code == 200
    assert len(history.json()) == 2
    assert history.json()[0]["input_per_million"] == "5"

    negative = client.post(
        f"/api/models/{model.id}/prices",
        headers=headers,
        json={"input_per_million": "-1"},
    )
    assert negative.status_code == 400
    assert "不可為負" in negative.json()["detail"]

    locked = client.put("/api/billing/currency", headers=headers, json={"currency": "eur"})
    assert locked.status_code == 409
    assert "不能再改平台貨幣" in locked.json()["detail"]
    same = client.put("/api/billing/currency", headers=headers, json={"currency": "usd"})
    assert same.status_code == 200
    assert same.json()["currency"] == "USD"

    missing_price = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "user",
            "user_id": target.id,
            "period": "monthly",
            "metric": "cost",
            "limit_value": 1_000_000,
            "on_limit": "block",
        },
    )
    # audit-model 已有現價，但 make_model 之外若還有其他使用中模型才會擋。
    # 這次請求的模型都已計價時應成功；若有未計價模型則必須點名。
    if missing_price.status_code == 400:
        assert missing_price.json()["detail"].startswith("金額額度需要這個對象可用的模型都已完整計價")
        for other in db.query(type(model)).filter(type(model).id != model.id, type(model).is_active.is_(True)).all():
            _price(db, other.id, datetime.now(_UTC) - timedelta(days=1), input_per_million=1)
        missing_price = client.post(
            "/api/quotas",
            headers=headers,
            json={
                "scope_type": "user",
                "user_id": target.id,
                "period": "monthly",
                "metric": "cost",
                "limit_value": 1_000_000,
                "on_limit": "block",
            },
        )
    assert missing_price.status_code == 200, missing_price.text

    created = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "user",
            "user_id": target.id,
            "period": "daily",
            "metric": "tokens",
            "limit_value": 1000,
            "on_limit": "warn",
        },
    )
    assert created.status_code == 200, created.text
    duplicate = client.post(
        "/api/quotas",
        headers=headers,
        json={
            "scope_type": "user",
            "user_id": target.id,
            "period": "daily",
            "metric": "tokens",
            "limit_value": 5,
            "on_limit": "block",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "同一對象、期間與計量已有額度"

    updated = client.put(
        f"/api/quotas/{created.json()['id']}",
        headers=headers,
        json={"on_limit": "block", "warn_percent": 90},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["on_limit"] == "block"
    deleted = client.delete(f"/api/quotas/{created.json()['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text

    db.expire_all()
    actions = {row.action for row in db.query(AuditLog).all()}
    assert "model.price.set" in actions
    assert "billing.currency.set" in actions
    assert "quota.create" in actions
    assert "quota.update" in actions
    assert "quota.delete" in actions


def test_studio_cut_is_not_folded_into_web(db: Session):
    user = make_user(db, username="studio-cut")
    model = make_model(db, name="studio-model")
    now = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=now, total=8, request_type="studio")
    cuts = get_usage_by_api_key(db, range_key="24h")
    assert cuts[0]["label"] == "簡報／報告"
    assert cuts[0]["total_tokens"] == 8


def test_flush_bumps_after_commit(monkeypatch):
    seen = []
    db = _Ok()

    def _bump(session, rows):
        assert db.committed is True
        seen.append(list(rows))

    monkeypatch.setattr(uw, "SessionLocal", lambda: db)
    monkeypatch.setattr("app.services.quota_service.bump_committed_usage", _bump)
    left = asyncio.run(uw._flush_batch([{"user_id": 1, "total_tokens": 3}]))
    assert left == []
    assert seen == [[{"user_id": 1, "total_tokens": 3}]]


def test_failed_flush_does_not_bump(monkeypatch):
    called = []
    monkeypatch.setattr(uw, "SessionLocal", lambda: _Down())
    monkeypatch.setattr(
        "app.services.quota_service.bump_committed_usage",
        lambda *args, **kwargs: called.append(1),
    )
    left = asyncio.run(uw._flush_batch([{"user_id": 1}]))
    assert left == [{"user_id": 1}]
    assert called == []


def test_quota_passthrough_message():
    sentence = "已達使用者每日 token 上限（10,000），將於 2026-10-02 00:00（台北時間）重置。"
    body = '{"detail":{"code":"quota_exceeded","message":"%s"}}' % sentence
    assert quota_exceeded_message(429, body) == sentence
    assert quota_exceeded_message(500, body) is None
    assert quota_exceeded_message(429, '{"detail":"沒有額度"}') is None
    assert quota_exceeded_message(429, '{"detail":{"code":"other","message":"%s"}}' % sentence) is None
    leaked = '{"detail":{"code":"quota_exceeded","message":"（已達這次回合上限）"}}'
    assert quota_exceeded_message(429, leaked) is None
    assert quota_exceeded_message(429, "not-json") is None


class _Down:
    def bulk_insert_mappings(self, model, batch):
        raise RuntimeError("db down")

    def commit(self):
        raise AssertionError("commit")

    def rollback(self):
        return None

    def close(self):
        return None


class _Ok:
    def __init__(self) -> None:
        self.committed = False

    def bulk_insert_mappings(self, model, batch):
        self.batch = list(batch)

    def commit(self):
        self.committed = True

    def rollback(self):
        return None

    def close(self):
        return None
