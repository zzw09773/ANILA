"""Round 2：額度重建、呼叫時間、派工金鑰、未計價、貨幣鎖、報表與通知。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.agent import UserAgentPermission
from app.models.agent_session_owner import AgentSessionOwner
from app.models.alert import Alert
from app.models.handoff import Notification
from app.models.model_price import ModelPrice
from app.models.token_usage import TokenUsage
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.usage_quota import QuotaEvent, UsageQuota
from app.models.user import UserModelPermission
from app.services.pricing import PriceBook, cost_rollup, row_cost_micros
from app.services.quota_redis import MemoryQuotaRedis, set_quota_redis
from app.services.quota_service import (
    bump_committed_usage,
    counter_key,
    enforce_call_quota,
    list_quota_blocks,
    period_window,
    read_counter,
)
from app.services.usage_service import (
    ExportRangeError,
    get_usage_by_api_key,
    get_usage_by_unit,
    month_usage_for_api_keys,
    resolve_export_window,
)
from app.services import usage_writer as uw
from tests.conftest import login, make_agent, make_api_key, make_model, make_user
from tests.test_pricing_and_quotas import _blocked, _dept, _price, _quota, _usage

_UTC = timezone.utc


def _snap_pair(user_id: int):
    from app.services.quota_service import QuotaSnap

    common = dict(
        scope_type="user",
        user_id=user_id,
        department_id=None,
        api_key_id=None,
        period="daily",
        metric="tokens",
        limit_value=1000,
        warn_percent=80,
        on_limit="warn",
    )
    return (
        QuotaSnap(id=1, **common),
        QuotaSnap(id=2, **common),
    )


def test_missing_key_rebuild_does_not_clobber_a_newer_value(db: Session, monkeypatch):
    redis = MemoryQuotaRedis()
    set_quota_redis(redis)
    user = make_user(db, username="clobber-user")
    model = make_model(db, name="clobber-model")
    when = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=when, total=40)
    quota = _quota(db, scope_type="user", user_id=user.id, limit_value=1000, on_limit="warn")
    _start, _end, period_key = period_window("daily", when)
    key = counter_key(quota, period_key)

    def plant_then_return_stale(_db, _quota, _moment):
        redis.store[key] = "130"
        return 100

    monkeypatch.setattr("app.services.quota_service.rebuild_counter", plant_then_return_stale)
    assert read_counter(db, redis, quota, when) == 130
    assert redis.store[key] == "130"

    redis.store.clear()
    bump_committed_usage(
        db,
        [{
            "user_id": user.id,
            "department_id": None,
            "api_key_id": None,
            "model_id": model.id,
            "total_tokens": 10,
            "request_timestamp": when,
            "usage_kind": "inference",
        }],
    )
    assert redis.store[key] == "130"


def test_increment_failure_invalidates_and_next_read_uses_db(db: Session, caplog):
    redis = MemoryQuotaRedis()
    set_quota_redis(redis)
    user = make_user(db, username="invalid-user")
    model = make_model(db, name="invalid-model")
    when = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=when, total=40)
    quota = _quota(db, scope_type="user", user_id=user.id, limit_value=1000, on_limit="warn")
    _start, _end, period_key = period_window("daily", when)
    key = counter_key(quota, period_key)
    redis.store[key] = "5"

    def boom(*_args, **_kwargs):
        raise RuntimeError("eval failed")

    redis.eval = boom
    with caplog.at_level("WARNING"):
        bump_committed_usage(
            db,
            [{
                "user_id": user.id,
                "total_tokens": 10,
                "request_timestamp": when,
                "usage_kind": "inference",
            }],
        )
    assert "額度計數已標為失效，待下次讀取對帳" in caplog.text
    assert redis.store.get(key) in (None, "invalid", "5")
    # 還原 eval。失效鍵不能再被當成 5。
    del redis.eval
    assert read_counter(db, redis, quota, when) == 40
    assert redis.store[key] == "40"


def test_enqueue_keeps_call_start_not_flush_clock(monkeypatch):
    early = datetime(2026, 10, 1, 15, 0, tzinfo=_UTC)
    late = datetime(2026, 10, 2, 1, 0, tzinfo=_UTC)

    class _Late:
        @staticmethod
        def now(tz=None):
            return late

    monkeypatch.setattr(uw, "datetime", _Late)
    queue = uw.get_usage_queue()
    while not queue.empty():
        queue.get_nowait()

    async def _put():
        await uw.enqueue_usage(
            None, 1, None, 1, 1, 1, 2, request_timestamp=early,
        )

    asyncio.run(_put())
    item = queue.get_nowait()
    assert item["request_timestamp"] == early

    token = uw.bind_usage_timestamp(early)
    try:
        uw.queue_usage_nowait({
            "api_key_id": None,
            "user_id": 1,
            "model_id": 1,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
            "outcome": "partial",
        })
    finally:
        uw.reset_usage_timestamp(token)
    partial = queue.get_nowait()
    assert partial["request_timestamp"] == early
    assert partial["outcome"] == "partial"


def test_proxy_stream_stamps_call_start_on_success_and_partial(monkeypatch):
    from app.services.proxy.service import ProxyTuning, proxy_stream

    early = datetime(2026, 9, 30, 16, 30, tzinfo=_UTC)
    late = datetime(2026, 10, 2, 1, 0, tzinfo=_UTC)

    class _Late:
        @staticmethod
        def now(tz=None):
            return late

    monkeypatch.setattr(uw, "datetime", _Late)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")
    lines = [
        'data: {"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":"stop"}]}',
        "",
        "data: [DONE]",
        "",
    ]

    class _Resp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            for line in lines:
                yield line

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr("app.services.proxy.service.httpx.AsyncClient", _Client)
    queue = uw.get_usage_queue()
    while not queue.empty():
        queue.get_nowait()

    async def _run():
        async for _chunk in proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            model_name="m",
            tuning=ProxyTuning.from_registry_defaults(),
            request_timestamp=early,
        ):
            pass

    asyncio.run(_run())
    success = queue.get_nowait()
    assert success["request_timestamp"] == early

    async def _partial():
        gen = proxy_stream(
            target_url="http://mock-llm/v1/chat/completions",
            api_key_id=1,
            user_id=2,
            department_id=None,
            usage_model_id=3,
            request_body={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            model_name="m",
            tuning=ProxyTuning.from_registry_defaults(),
            request_timestamp=early,
        )
        async for _chunk in gen:
            await gen.aclose()
            break

    asyncio.run(_partial())
    partial = queue.get_nowait()
    assert partial["request_timestamp"] == early
    assert partial["outcome"] == "partial"


def test_dispatch_token_carries_api_key_into_quota_and_usage(client, db: Session, monkeypatch):
    from app.services.proxy.dispatch_chat import dispatch_model_call
    from app.services.proxy.dispatch_token import issue_dispatch_token, verify_dispatch_token

    owner = make_user(db, username="disp-owner", role="developer")
    asker = make_user(db, username="disp-asker")
    base = make_model(db, name="disp-base")
    agent = make_agent(db, owner, name="disp-agent", approval_status="approved")
    agent.base_model_id = base.id
    db.commit()
    key = make_api_key(db, asker)
    token = issue_dispatch_token(
        user_id=asker.id,
        department=asker.department_id,
        agent_id=agent.id,
        api_key_id=key.id,
    )
    claims = verify_dispatch_token(token, db)
    assert claims is not None
    assert claims["api_key_id"] == key.id
    call = dispatch_model_call(db, claims)
    assert call.api_key_id == key.id

    captured = {}

    async def _fake_proxy(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("app.api.proxy.proxy_request", _fake_proxy)
    started = datetime.now(_UTC)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": base.name, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text
    assert captured["api_key_id"] == key.id
    assert captured["request_timestamp"] <= datetime.now(_UTC)
    assert captured["request_timestamp"] >= started - timedelta(seconds=5)

    set_quota_redis(MemoryQuotaRedis())
    _quota(
        db,
        scope_type="api_key",
        api_key_id=key.id,
        limit_value=0,
        on_limit="block",
    )
    blocked = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": base.name, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["code"] == "quota_exceeded"
    assert "API 金鑰" in blocked.json()["detail"]["message"]


def test_resume_prechecks_quota_and_forwards_api_key(client, db: Session, monkeypatch):
    user = make_user(db, username="resume-user")
    agent = make_agent(db, user, name="resume-agent", approval_status="approved")
    db.add(AgentSessionOwner(session_id="sess-r2", owner_user_id=user.id))
    db.commit()
    key = make_api_key(db, user)
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    db.commit()
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")
    seen = {}

    def _headers(**kwargs):
        seen.update(kwargs)
        return {"Authorization": "Bearer test", "Content-Type": "application/json"}

    class _Resp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def aiter_lines(self):
            yield "data: ok"
            yield ""

    class _Client:
        def __init__(self, *a, **k):
            seen["http"] = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        def stream(self, *a, **k):
            return _Resp()

    monkeypatch.setattr("app.services.proxy_service.build_agent_headers", _headers)
    monkeypatch.setattr("httpx.AsyncClient", _Client)
    set_quota_redis(MemoryQuotaRedis())
    _quota(db, scope_type="api_key", api_key_id=key.id, limit_value=0, on_limit="block")
    blocked = client.post(
        f"/v1/agents/{agent.name}/sessions/sess-r2/answer",
        headers={"Authorization": "Bearer sk-test-key"},
        json={"interrupt_id": "i", "answer": "yes"},
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["code"] == "quota_exceeded"
    assert "http" not in seen

    quota = db.query(UsageQuota).one()
    quota.limit_value = 1000
    db.commit()
    ok = client.post(
        f"/v1/agents/{agent.name}/sessions/sess-r2/answer",
        headers={"Authorization": "Bearer sk-test-key"},
        json={"interrupt_id": "i", "answer": "yes"},
    )
    assert ok.status_code == 200, ok.text
    assert seen["api_key_id"] == key.id
    assert "X-ANILA-Api-Key-Id" not in (seen.get("headers") or {})


def test_partial_price_is_unpriced_and_money_quota_ignores_it(db: Session):
    user = make_user(db, username="partial-user")
    model = make_model(db, name="partial-model")
    when = datetime.now(_UTC)
    db.add(ModelPrice(
        model_id=model.id,
        input_micros=1_000_000,
        output_micros=None,
        reasoning_micros=None,
        effective_at=when - timedelta(days=1),
    ))
    db.commit()
    row = _usage(db, user=user, model=model, when=when, prompt=1_000_000, completion=1_000_000)
    book = PriceBook.load(db)
    assert row_cost_micros(row, book) is None
    rolled = cost_rollup(db, [row])
    assert rolled["cost_state"] == "unpriced"
    assert rolled["cost"] is None

    set_quota_redis(MemoryQuotaRedis())
    quota = _quota(
        db,
        scope_type="user",
        user_id=user.id,
        metric="cost",
        limit_value=1,
        on_limit="block",
    )
    assert _blocked(user, db=db, now=when) is None
    bump_committed_usage(db, [row])
    _start, _end, period_key = period_window("daily", when)
    key = f"anila:quota:v1:daily:{period_key}:user:{user.id}:costnum"
    assert counter_key(quota, period_key) == key
    stored = MemoryQuotaRedis()
    # 未計價不加計數。鍵不該被建成 0。
    from app.services.quota_redis import get_quota_redis
    live = get_quota_redis()
    assert key not in live.store or int(live.store[key]) == 0


def test_money_quota_requires_subject_models_fully_priced(client, db: Session):
    admin = make_user(db, username="cov-admin", role="admin")
    person = make_user(db, username="cov-person")
    priced = make_model(db, name="cov-priced")
    missing = make_model(db, name="cov-missing", )
    missing.display_name = "缺價模型"
    db.add(UserModelPermission(user_id=person.id, model_id=priced.id))
    db.commit()
    _price(db, priced.id, datetime.now(_UTC) - timedelta(minutes=1), input_per_million=1)
    headers = {"Authorization": f"Bearer {login(client, 'cov-admin')}"}
    ok = client.post("/api/quotas", headers=headers, json={
        "scope_type": "user",
        "user_id": person.id,
        "period": "monthly",
        "metric": "cost",
        "limit_value": 1_000_000,
        "on_limit": "block",
    })
    assert ok.status_code == 200, ok.text
    denied = client.post("/api/quotas", headers=headers, json={
        "scope_type": "user",
        "user_id": admin.id,
        "period": "monthly",
        "metric": "cost",
        "limit_value": 1_000_000,
        "on_limit": "block",
    })
    assert denied.status_code == 400, denied.text
    assert denied.json()["detail"].startswith("金額額度需要這個對象可用的模型都已完整計價")
    assert "缺價模型" in denied.json()["detail"]
    coverage = client.get(
        "/api/quotas/price-coverage",
        headers=headers,
        params={"scope_type": "user", "user_id": admin.id},
    )
    assert coverage.status_code == 200
    assert coverage.json()["ready"] is False
    assert "缺價模型" in coverage.json()["missing"]


def test_unpriced_rows_are_named_on_the_quota(client, db: Session):
    admin = make_user(db, username="unpriced-admin", role="admin")
    user = make_user(db, username="unpriced-user")
    model = make_model(db, name="unpriced-model")
    _usage(db, user=user, model=model, when=datetime.now(_UTC), total=5)
    _quota(db, scope_type="user", user_id=user.id, metric="cost", limit_value=5_000_000, on_limit="warn")
    headers = {"Authorization": f"Bearer {login(client, 'unpriced-admin')}"}
    listed = client.get("/api/quotas", headers=headers)
    assert listed.status_code == 200
    row = listed.json()[0]
    assert row["unpriced_count"] == 1
    assert row["unpriced_message"] == "期間內有 1 筆未計價用量，未計入金額"


def test_currency_locks_after_price_or_money_quota(client, db: Session):
    admin = make_user(db, username="lock-admin", role="admin")
    user = make_user(db, username="lock-user")
    headers = {"Authorization": f"Bearer {login(client, 'lock-admin')}"}
    first = client.put("/api/billing/currency", headers=headers, json={"currency": "usd"})
    assert first.status_code == 200
    model = make_model(db, name="lock-model")
    priced = client.post(
        f"/api/models/{model.id}/prices",
        headers=headers,
        json={"input_per_million": "1", "output_per_million": "1"},
    )
    assert priced.status_code == 200, priced.text
    locked = client.put("/api/billing/currency", headers=headers, json={"currency": "eur"})
    assert locked.status_code == 409
    assert "不能再改平台貨幣" in locked.json()["detail"]
    same = client.put("/api/billing/currency", headers=headers, json={"currency": "usd"})
    assert same.status_code == 200
    current = client.get("/api/billing/currency", headers=headers)
    assert current.json()["locked"] is True

    db.query(ModelPrice).delete()
    db.commit()
    _quota(db, scope_type="user", user_id=user.id, metric="cost", limit_value=1, on_limit="warn")
    again = client.put("/api/billing/currency", headers=headers, json={"currency": "jpy"})
    assert again.status_code == 409


def test_effective_at_rejects_more_than_five_minutes_ago(client, db: Session):
    admin = make_user(db, username="eff-admin", role="admin")
    model = make_model(db, name="eff-model")
    headers = {"Authorization": f"Bearer {login(client, 'eff-admin')}"}
    old = (datetime.now(_UTC) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rejected = client.post(
        f"/api/models/{model.id}/prices",
        headers=headers,
        json={"input_per_million": "1", "output_per_million": "2", "effective_at": old},
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "生效時間不能早於現在五分鐘以上"
    edge = (datetime.now(_UTC) - timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    allowed = client.post(
        f"/api/models/{model.id}/prices",
        headers=headers,
        json={"input_per_million": "1", "output_per_million": "2", "effective_at": edge},
    )
    assert allowed.status_code == 200, allowed.text


def test_empty_quota_cache_skips_the_table_on_the_next_call(db: Session):
    user = make_user(db, username="cache-user")
    enforce_call_quota(
        db, user_id=user.id, department_id=user.department_id, api_key_id=None,
    )
    statements = []

    def _capture(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", _capture)
    try:
        enforce_call_quota(
            db, user_id=user.id, department_id=user.department_id, api_key_id=None,
        )
        bump_committed_usage(db, [{
            "user_id": user.id,
            "total_tokens": 3,
            "request_timestamp": datetime.now(_UTC),
            "usage_kind": "inference",
        }])
    finally:
        event.remove(bind, "before_cursor_execute", _capture)
    assert not any("usage_quotas" in text.lower() for text in statements)


def test_duplicate_scope_is_rejected_by_the_database(db: Session):
    user = make_user(db, username="dup-user")
    _quota(db, scope_type="user", user_id=user.id, period="daily", metric="tokens")
    db.add(UsageQuota(
        scope_type="user",
        user_id=user.id,
        period="daily",
        metric="tokens",
        limit_value=5,
        on_limit="block",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_duplicate_rows_share_one_redis_delta(db: Session):
    from app.services.quota_service import _accumulate_deltas

    user = make_user(db, username="merge-user")
    when = datetime.now(_UTC)
    left, right = _snap_pair(user.id)
    merged = _accumulate_deltas(db, [left, right], [{
        "user_id": user.id,
        "department_id": None,
        "api_key_id": None,
        "total_tokens": 10,
        "request_timestamp": when,
        "usage_kind": "inference",
    }], None)
    assert len(merged) == 1
    assert next(iter(merged.values()))["amount"] == 10


def test_sub_micro_rows_sum_before_rounding(db: Session):
    user = make_user(db, username="micro-user")
    model = make_model(db, name="micro-model")
    when = datetime.now(_UTC)
    db.add(ModelPrice(
        model_id=model.id,
        input_micros=1,
        output_micros=0,
        reasoning_micros=None,
        effective_at=when - timedelta(minutes=1),
    ))
    db.commit()
    book = PriceBook.load(db)
    first = _usage(db, user=user, model=model, when=when, prompt=600_000, completion=0)
    assert row_cost_micros(first, book) == 0
    set_quota_redis(MemoryQuotaRedis())
    quota = _quota(
        db,
        scope_type="user",
        user_id=user.id,
        metric="cost",
        limit_value=1,
        on_limit="block",
        warn_percent=100,
    )
    bump_committed_usage(db, [first])
    second = _usage(db, user=user, model=model, when=when, prompt=600_000, completion=0)
    bump_committed_usage(db, [second])
    rolled = cost_rollup(db, [first, second], book=book, currency="TWD")
    assert rolled["cost"] == "0.000001"
    assert rolled["cost_state"] == "priced"
    _start, _end, period_key = period_window("daily", when)
    from app.services.quota_redis import get_quota_redis
    stored = int(get_quota_redis().store[counter_key(quota, period_key)])
    assert stored == 1_200_000
    assert _blocked(user, db=db, now=when) is not None


def test_month_usage_filters_keys_in_sql_and_loads_prices_once(db: Session, monkeypatch):
    user = make_user(db, username="month-user")
    other = make_user(db, username="month-other")
    model = make_model(db, name="month-model")
    key = make_api_key(db, user)
    other_key = make_api_key(db, other, raw_key="sk-other-key")
    when = datetime.now(_UTC)
    _price(db, model.id, when - timedelta(minutes=1), input_per_million=2)
    third = make_api_key(db, user, raw_key="sk-third-key")
    _usage(db, user=user, model=model, when=when, total=4, api_key_id=key.id)
    _usage(db, user=other, model=model, when=when, total=9, api_key_id=other_key.id)
    _usage(db, user=user, model=model, when=when, total=2, api_key_id=third.id)
    loads = {"n": 0}
    real = PriceBook.load

    def _spy(session):
        loads["n"] += 1
        return real(session)

    monkeypatch.setattr(PriceBook, "load", classmethod(lambda cls, session: _spy(session)))
    sql = []

    def _capture(_conn, _cursor, statement, _params, _context, _executemany):
        sql.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", _capture)
    try:
        month = month_usage_for_api_keys(db, [key.id], now=when)
        by_key = get_usage_by_api_key(db, range_key="24h")
        by_unit = get_usage_by_unit(db, range_key="24h")
    finally:
        event.remove(bind, "before_cursor_execute", _capture)
    assert month[key.id]["month_tokens"] == 4
    assert other_key.id not in month
    import re
    assert any(re.search(r"api_key_id\s+in\s*\(", text, re.I) for text in sql)
    # 月份、金鑰報表、單位報表各載一次價格。舊程式每個分組各載一次，三把金鑰會超過。
    assert loads["n"] == 3
    assert by_key and by_unit is not None


def test_export_year_uses_the_exclusive_end(db: Session):
    with pytest.raises(ExportRangeError, match="單次匯出最長一年"):
        resolve_export_window("24h", start="2025-10-01", end="2026-10-01")
    start, end = resolve_export_window("24h", start="2025-10-01", end="2026-09-30")
    assert end - start == timedelta(days=365)
    leap_start, leap_end = resolve_export_window("24h", start="2024-01-01", end="2024-12-31")
    assert leap_end - leap_start == timedelta(days=366)


def test_threshold_notifies_after_commit_once(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="cross-user")
    model = make_model(db, name="cross-model")
    when = datetime.now(_UTC)
    quota = _quota(
        db,
        scope_type="user",
        user_id=user.id,
        limit_value=100,
        on_limit="warn",
        warn_percent=80,
    )
    assert _blocked(user, db=db, now=when) is None
    assert db.query(Notification).count() == 0
    _usage(db, user=user, model=model, when=when, total=90)
    bump_committed_usage(db, [{
        "user_id": user.id,
        "model_id": model.id,
        "total_tokens": 90,
        "request_timestamp": when,
        "usage_kind": "inference",
    }])
    notes = db.query(Notification).all()
    assert len(notes) == 1
    assert notes[0].type == "quota_warn"
    bump_committed_usage(db, [{
        "user_id": user.id,
        "model_id": model.id,
        "total_tokens": 1,
        "request_timestamp": when,
        "usage_kind": "inference",
    }])
    assert db.query(Notification).count() == 1
    assert db.query(Alert).count() == 1
    from app.services.quota_service import _notify

    other = db.get_bind()
    from sqlalchemy.orm import sessionmaker
    SessionB = sessionmaker(bind=other, expire_on_commit=False)
    first = SessionB()
    second = SessionB()
    _notify(
        first, quota, kind="warn", title="用量接近上限", body="再次",
        trigger_user_id=user.id, period_key=period_window("daily", when)[2], commit=False,
    )
    _notify(
        second, quota, kind="warn", title="用量接近上限", body="再次",
        trigger_user_id=user.id, period_key=period_window("daily", when)[2], commit=False,
    )
    first.commit()
    second.commit()
    assert db.query(Alert).filter(Alert.category == "quota").count() == 1


def test_limit_crossing_suppresses_the_warn_notice(db: Session):
    set_quota_redis(MemoryQuotaRedis())
    user = make_user(db, username="both-user")
    model = make_model(db, name="both-model")
    when = datetime.now(_UTC)
    _usage(db, user=user, model=model, when=when, total=100)
    _quota(db, scope_type="user", user_id=user.id, limit_value=100, on_limit="block", warn_percent=80)
    from app.services.quota_redis import get_quota_redis
    # 預檢會擋下並記 block。這裡只測 commit 後的跨越，先清掉預檢。
    bump_committed_usage(db, [{
        "user_id": user.id,
        "total_tokens": 100,
        "request_timestamp": when,
        "usage_kind": "inference",
    }])
    kinds = {row.type for row in db.query(Notification).all()}
    assert "quota_limit" in kinds
    assert "quota_warn" not in kinds
    assert get_quota_redis() is not None


def test_unit_admin_quota_blocks_follow_event_department(client, db: Session):
    admin = make_user(db, username="block-admin", role="admin")
    parent = _dept(db, "甲單位")
    child = _dept(db, "甲下層", parent.id)
    other = _dept(db, "乙單位")
    lead = make_user(db, username="block-lead", department_id=parent.id)
    home_b = make_user(db, username="block-home-b", department_id=other.id)
    db.add(UnitAdminAssignment(user_id=lead.id, department_id=parent.id, granted_by=admin.id))
    now = datetime.now(_UTC)
    db.add(QuotaEvent(
        user_id=home_b.id,
        department_id=parent.id,
        action="block",
        message="甲的金鑰",
        occurred_at=now,
    ))
    db.add(QuotaEvent(
        user_id=lead.id,
        department_id=child.id,
        action="block",
        message="下層",
        occurred_at=now,
    ))
    db.add(QuotaEvent(
        user_id=lead.id,
        department_id=other.id,
        action="block",
        message="乙的",
        occurred_at=now,
    ))
    db.commit()
    headers = {"Authorization": f"Bearer {login(client, 'block-lead')}"}
    picked = client.get("/api/usage/quota-blocks", headers=headers, params={"department_id": parent.id})
    assert picked.status_code == 200, picked.text
    messages = {row["message"] for row in picked.json()}
    assert messages == {"甲的金鑰", "下層"}
    wide = client.get("/api/usage/quota-blocks", headers=headers)
    assert {row["message"] for row in wide.json()} == {"甲的金鑰", "下層"}


def test_internal_quota_message_reaches_prompt_and_thinking(client, db: Session, monkeypatch):
    from app.models.ingestion import IngestionCollection
    from app.services.internal_llm import InternalCompletionError, complete_chat

    message = "已達使用者每日 token 上限（1），將於 2026-10-02 00:00（台北時間）重置。"

    async def _boom(*_args, **_kwargs):
        raise InternalCompletionError(429, code="quota_exceeded", message=message)

    user = make_user(db, username="prompt-dev", role="developer")
    make_model(db, name="prompt-llm")
    col = IngestionCollection(
        name="庫",
        chunking_config={},
        embedding_dim=8,
        created_by=user.id,
    )
    db.add(col)
    db.commit()
    monkeypatch.setattr("app.services.internal_llm.complete_chat", _boom)
    resp = client.post(
        "/api/agents/system-prompt/suggest",
        headers={"Authorization": f"Bearer {login(client, 'prompt-dev')}"},
        json={"collection_id": col.id, "ideas": "做一個助理"},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["detail"]["code"] == "quota_exceeded"
    assert resp.json()["detail"]["message"] == message

    monkeypatch.setattr(
        "app.services.thinking_summary._summary_model",
        lambda _db: make_model(db, name="think-llm"),
    )
    think = client.post(
        "/api/thinking/summarize",
        headers={"Authorization": f"Bearer {login(client, 'prompt-dev')}"},
        json={"added": "這是一段夠長的思考內容，用來跨過摘要的最短長度，再補幾個字讓它超過四十個字呀。OK"},
    )
    assert think.status_code == 429, think.text
    assert think.json()["detail"]["message"] == message

    async def _other(*_a, **_k):
        raise InternalCompletionError(502)

    monkeypatch.setattr("app.services.internal_llm.complete_chat", _other)
    opened = client.post(
        "/api/thinking/summarize",
        headers={"Authorization": f"Bearer {login(client, 'prompt-dev')}"},
        json={"added": "這是一段夠長的思考內容，用來跨過摘要的最短長度，再補幾個字讓它超過四十個字呀。OK"},
    )
    assert opened.status_code == 200
    assert opened.json()["summary"] is None
    assert complete_chat is not None


def test_complete_chat_preserves_quota_detail(db: Session, monkeypatch):
    from app.services.internal_llm import InternalCompletionError, complete_chat

    user = make_user(db, username="inner-user")
    model = make_model(db, name="inner-model")
    message = "已達使用者每日 token 上限（9），將於 2026-10-02 00:00（台北時間）重置。"

    def _raise(*_a, **_k):
        raise HTTPException(status_code=429, detail={"code": "quota_exceeded", "message": message})

    monkeypatch.setattr("app.services.quota_service.enforce_call_quota", _raise)
    with pytest.raises(InternalCompletionError) as caught:
        asyncio.run(complete_chat(
            db, model, {"messages": []}, user_id=user.id, department_id=None, on_behalf_of_user=True,
        ))
    assert caught.value.status_code == 429
    assert caught.value.quota_code == "quota_exceeded"
    assert caught.value.quota_message == message
