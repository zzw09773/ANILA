"""Unit tests:P2-8 policyLimits 組織政策。

涵蓋面
======

* :class:`Limit` dataclass:
  - 必填欄位、threshold 驗證、name 非空驗證
  - ``to_dict`` / ``from_dict`` roundtrip

* :class:`LimitCheckResult`:
  - ``remaining`` / ``usage_ratio`` / ``should_block`` 計算

* :class:`LimitsEngine`:
  - ``track`` 累加、wildcard scope_id 行為
  - ``check`` 對單一 (scope, metric) 跑、回傳符合 limit
  - ``check_all`` 一次跑所有 metric
  - ``is_blocked`` helper

* :class:`InMemoryLimitsStore`:
  - 記 records + limits、query 過濾
  - 清空 (clear)

* :class:`JsonFileLimitsStore`:
  - save / load roundtrip
  - 多筆 record 持久化
  - 壞檔 fail-open(load_errors 記錯)

* period rollover:
  - day / week / month 邊界用 fake time 切割

* block / warn / throttle 三種 action 行為

* 與 P1-15 CostTracker 整合:
  - ``hook_cost_tracker`` 不破 P1-15 record return 值
  - mirror 一筆到 agent_template scope
  - 同時給 user_id → mirror 到 user scope
  - unhook 後 cost_tracker.record 還原

* 與 P0-7 PolicyEngine 整合:
  - ``limits_to_policy_rule`` 在 limit exceeded 時 DENY
  - 未 exceeded 時不阻擋(走 default ALLOW)
  - metrics 篩子集
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from anila_agent.core.cost_tracker import CostTracker, PricingRegistry
from anila_agent.core.policy import PolicyDecision, PolicyEngine
from anila_agent.core.policy_limits import (
    InMemoryLimitsStore,
    JsonFileLimitsStore,
    Limit,
    LimitCheckResult,
    LimitsEngine,
    daily_token_limit_per_user,
    hook_cost_tracker,
    limits_to_policy_rule,
    monthly_cost_limit_per_agent,
)


# ---------------------------------------------------------------------------
# 共用 fixture / helper
# ---------------------------------------------------------------------------


def _fixed_time(year: int, month: int, day: int, hour: int = 12) -> datetime:
    """產生固定 UTC 時間,測試 period rollover 用。"""
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _engine_with_time(now: datetime) -> LimitsEngine:
    """造一個 time-frozen LimitsEngine。"""
    return LimitsEngine(time_provider=lambda: now)


# ---------------------------------------------------------------------------
# Limit dataclass
# ---------------------------------------------------------------------------


def test_limit_basic_construction() -> None:
    limit = Limit(
        name="daily_tokens",
        scope="user",
        metric="tokens",
        period="day",
        threshold=100_000,
    )
    assert limit.name == "daily_tokens"
    assert limit.scope == "user"
    assert limit.metric == "tokens"
    assert limit.threshold == 100_000
    assert limit.action_when_exceeded == "block"
    assert limit.scope_id is None


def test_limit_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Limit(
            name="",
            scope="user",
            metric="tokens",
            period="day",
            threshold=100,
        )


def test_limit_rejects_negative_threshold() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        Limit(
            name="bad",
            scope="user",
            metric="tokens",
            period="day",
            threshold=-1,
        )


def test_limit_zero_threshold_is_legal() -> None:
    """threshold=0 合法(代表「禁止所有用量」),Engine 會在 first track 立即 exceeded。"""
    limit = Limit(
        name="zero",
        scope="user",
        metric="tokens",
        period="day",
        threshold=0,
    )
    assert limit.threshold == 0


def test_limit_to_from_dict_roundtrip() -> None:
    original = Limit(
        name="cost_cap",
        scope="agent_template",
        metric="cost_usd",
        period="month",
        threshold=1000.0,
        action_when_exceeded="warn",
        scope_id="my_template",
    )
    payload = original.to_dict()
    restored = Limit.from_dict(payload)
    assert restored == original


def test_limit_is_frozen() -> None:
    """frozen dataclass — 不能改欄位(避免 store 被偷改)。"""
    limit = Limit(
        name="x",
        scope="user",
        metric="tokens",
        period="day",
        threshold=10,
    )
    with pytest.raises(Exception):
        limit.threshold = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LimitCheckResult
# ---------------------------------------------------------------------------


def test_check_result_remaining_and_usage_ratio() -> None:
    limit = Limit(
        name="x",
        scope="user",
        metric="tokens",
        period="day",
        threshold=100.0,
    )
    res = LimitCheckResult(
        limit=limit, current=30.0, threshold=100.0, exceeded=False
    )
    assert res.remaining == 70.0
    assert res.usage_ratio == 0.3
    assert res.should_block is False


def test_check_result_should_block() -> None:
    limit = Limit(
        name="x",
        scope="user",
        metric="tokens",
        period="day",
        threshold=100.0,
    )
    res = LimitCheckResult(
        limit=limit,
        current=200.0,
        threshold=100.0,
        exceeded=True,
        action="block",
    )
    assert res.should_block is True
    assert res.remaining == 0.0
    assert res.usage_ratio == 2.0


def test_check_result_warn_does_not_block() -> None:
    limit = Limit(
        name="x",
        scope="user",
        metric="tokens",
        period="day",
        threshold=100.0,
    )
    res = LimitCheckResult(
        limit=limit,
        current=200.0,
        threshold=100.0,
        exceeded=True,
        action="warn",
    )
    assert res.should_block is False
    assert res.exceeded is True


def test_check_result_zero_threshold_ratio_is_one() -> None:
    """threshold=0 時 usage_ratio 走 sentinel 1.0(避免 div0)。"""
    limit = Limit(
        name="x",
        scope="user",
        metric="tokens",
        period="day",
        threshold=0.0,
    )
    res = LimitCheckResult(
        limit=limit,
        current=5.0,
        threshold=0.0,
        exceeded=True,
        action="block",
    )
    assert res.usage_ratio == 1.0


# ---------------------------------------------------------------------------
# LimitsEngine — track / check 基本流程
# ---------------------------------------------------------------------------


def test_engine_track_then_check_under_threshold() -> None:
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(daily_token_limit_per_user(threshold=100_000))

    engine.track("user", "alice", "tokens", 500.0)
    engine.track("user", "alice", "tokens", 1500.0)

    results = engine.check("user", "alice", "tokens")
    assert len(results) == 1
    res = results[0]
    assert res.current == 2000.0
    assert res.exceeded is False
    assert res.should_block is False
    assert res.action is None


def test_engine_check_exceeds_threshold_triggers_block() -> None:
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(daily_token_limit_per_user(threshold=1000))

    engine.track("user", "alice", "tokens", 1500.0)

    results = engine.check("user", "alice", "tokens")
    assert results[0].exceeded is True
    assert results[0].should_block is True
    assert results[0].action == "block"


def test_engine_check_all_covers_multi_metric() -> None:
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(daily_token_limit_per_user(threshold=100_000))
    engine.set_limit(
        Limit(
            name="daily_cost_per_user",
            scope="user",
            metric="cost_usd",
            period="day",
            threshold=10.0,
        )
    )

    engine.track("user", "alice", "tokens", 5_000.0)
    engine.track("user", "alice", "cost_usd", 3.0)

    results = engine.check_all("user", "alice")
    assert len(results) == 2
    metrics = {r.limit.metric for r in results}
    assert metrics == {"tokens", "cost_usd"}


def test_engine_scope_id_filter() -> None:
    """指定 scope_id 的 limit 只套用該 scope_id;wildcard 套用全部。"""
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(
        Limit(
            name="alice_only",
            scope="user",
            metric="tokens",
            period="day",
            threshold=1000,
            scope_id="alice",
        )
    )
    engine.track("user", "alice", "tokens", 500.0)
    engine.track("user", "bob", "tokens", 5000.0)

    # alice 的 limit 命中(scope_id match)
    alice_res = engine.check("user", "alice", "tokens")
    assert len(alice_res) == 1
    assert alice_res[0].current == 500.0

    # bob 不匹配那條 limit
    bob_res = engine.check("user", "bob", "tokens")
    assert bob_res == []


def test_engine_wildcard_scope_id_applies_to_all_users() -> None:
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    # scope_id 留 None → 對所有 user 套用
    engine.set_limit(daily_token_limit_per_user(threshold=1000))
    engine.track("user", "alice", "tokens", 500.0)
    engine.track("user", "bob", "tokens", 500.0)

    alice_res = engine.check("user", "alice", "tokens")
    bob_res = engine.check("user", "bob", "tokens")
    assert alice_res[0].current == 500.0
    assert bob_res[0].current == 500.0


def test_engine_is_blocked_helper() -> None:
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(daily_token_limit_per_user(threshold=100))

    assert engine.is_blocked("user", "alice", "tokens") is False
    engine.track("user", "alice", "tokens", 150.0)
    assert engine.is_blocked("user", "alice", "tokens") is True


def test_engine_track_naive_ts_normalised_to_utc() -> None:
    """track ts 若 naive,自動補成 UTC(避免 period 計算炸 tz comparison)。"""
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(daily_token_limit_per_user(threshold=1000))
    naive_ts = datetime(2026, 5, 27, 12, 0, 0)
    engine.track("user", "alice", "tokens", 100.0, ts=naive_ts)

    result = engine.check("user", "alice", "tokens", now=now)
    assert result[0].current == 100.0


# ---------------------------------------------------------------------------
# InMemoryLimitsStore
# ---------------------------------------------------------------------------


def test_in_memory_store_records_and_queries() -> None:
    store = InMemoryLimitsStore()
    ts = _fixed_time(2026, 5, 27)
    store.record("user", "alice", "tokens", 100.0, ts)
    store.record("user", "alice", "tokens", 50.0, ts)
    store.record("user", "bob", "tokens", 999.0, ts)

    assert store.query("user", "alice", "tokens", "all_time", ts) == 150.0
    assert store.query("user", "bob", "tokens", "all_time", ts) == 999.0


def test_in_memory_store_limit_crud() -> None:
    store = InMemoryLimitsStore()
    limit = daily_token_limit_per_user(threshold=100)
    store.set_limit(limit)
    assert store.get_limit(limit.name) == limit
    assert store.list_limits() == [limit]
    assert store.get_limit("does_not_exist") is None


def test_in_memory_store_clear() -> None:
    store = InMemoryLimitsStore()
    store.set_limit(daily_token_limit_per_user(threshold=100))
    store.record("user", "alice", "tokens", 50.0, _fixed_time(2026, 5, 27))

    store.clear()
    assert store.list_limits() == []
    assert store.query(
        "user", "alice", "tokens", "all_time", _fixed_time(2026, 5, 27)
    ) == 0.0


# ---------------------------------------------------------------------------
# JsonFileLimitsStore
# ---------------------------------------------------------------------------


def test_json_file_store_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    store = JsonFileLimitsStore(path)
    limit = daily_token_limit_per_user(threshold=1000)
    store.set_limit(limit)

    ts = _fixed_time(2026, 5, 27)
    store.record("user", "alice", "tokens", 100.0, ts)
    store.record("user", "alice", "tokens", 250.0, ts)

    # 開新 instance 從 disk 載回
    reloaded = JsonFileLimitsStore(path)
    assert reloaded.get_limit(limit.name) == limit
    assert (
        reloaded.query("user", "alice", "tokens", "all_time", ts) == 350.0
    )
    assert reloaded.load_errors == []


def test_json_file_store_fail_open_on_corrupt_file(tmp_path: Path) -> None:
    """壞檔 fail-open — 開機不丟例外,load_errors 留診斷訊息。"""
    path = tmp_path / "limits.json"
    path.write_text("{ not valid json }", encoding="utf-8")
    store = JsonFileLimitsStore(path)
    assert store.list_limits() == []
    assert store.load_errors  # 有錯誤訊息


def test_json_file_store_skips_invalid_records(tmp_path: Path) -> None:
    """records 中個別壞掉的 entry 被 skip,不影響其他資料。"""
    path = tmp_path / "limits.json"
    path.write_text(
        '{"limits": [], "records": ['
        '{"scope": "user", "scope_id": "alice", "metric": "tokens",'
        ' "amount": 50, "ts": "2026-05-27T12:00:00+00:00"},'
        '{"this_one_is_broken": true}'
        ']}',
        encoding="utf-8",
    )
    store = JsonFileLimitsStore(path)
    # 第一筆有效、第二筆被 skip。
    total = store.query(
        "user", "alice", "tokens", "all_time", _fixed_time(2026, 5, 27)
    )
    assert total == 50.0
    assert len(store.load_errors) == 1


def test_json_file_store_creates_parent_dir(tmp_path: Path) -> None:
    """父目錄不存在時自動建立(對齊 P1-8 JsonFileStateStore 風格)。"""
    path = tmp_path / "nested" / "dir" / "limits.json"
    store = JsonFileLimitsStore(path)
    store.set_limit(daily_token_limit_per_user(threshold=100))
    assert path.exists()
    assert path.parent.is_dir()


def test_json_file_store_top_level_must_be_object(tmp_path: Path) -> None:
    """top-level 不是 object 時 fail-open + 記錯。"""
    path = tmp_path / "limits.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    store = JsonFileLimitsStore(path)
    assert store.list_limits() == []
    assert any("must be object" in e for e in store.load_errors)


# ---------------------------------------------------------------------------
# Period rollover — 用 fake time 切邊界
# ---------------------------------------------------------------------------


def test_period_day_rollover() -> None:
    """前一天的 record 不該算進今天 day-period。"""
    yesterday = _fixed_time(2026, 5, 26, hour=23)
    today = _fixed_time(2026, 5, 27, hour=1)

    engine = _engine_with_time(today)
    engine.set_limit(daily_token_limit_per_user(threshold=1000))
    engine.track("user", "alice", "tokens", 500.0, ts=yesterday)
    engine.track("user", "alice", "tokens", 100.0, ts=today)

    result = engine.check("user", "alice", "tokens", now=today)
    # 只算今日;yesterday 那筆不計
    assert result[0].current == 100.0


def test_period_week_rollover() -> None:
    """跨 ISO 週,前週的 record 不算。

    ISO 週以週一起算;2026-05-25(週一)是新一週起點,2026-05-24(週日)是上一週。
    """
    last_week_sun = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    this_week_tue = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)

    engine = _engine_with_time(this_week_tue)
    engine.set_limit(
        Limit(
            name="weekly",
            scope="user",
            metric="tokens",
            period="week",
            threshold=1000,
        )
    )
    engine.track("user", "alice", "tokens", 200.0, ts=last_week_sun)
    engine.track("user", "alice", "tokens", 50.0, ts=this_week_tue)

    result = engine.check("user", "alice", "tokens", now=this_week_tue)
    assert result[0].current == 50.0


def test_period_month_rollover() -> None:
    """跨月,上個月的 record 不算。"""
    last_month = _fixed_time(2026, 4, 30)
    this_month = _fixed_time(2026, 5, 1)

    engine = _engine_with_time(this_month)
    engine.set_limit(monthly_cost_limit_per_agent(threshold_usd=100))
    engine.track("agent_template", "tpl", "cost_usd", 50.0, ts=last_month)
    engine.track("agent_template", "tpl", "cost_usd", 5.0, ts=this_month)

    result = engine.check("agent_template", "tpl", "cost_usd", now=this_month)
    assert result[0].current == 5.0


def test_period_hour_minute_rollover() -> None:
    """minute / hour 邊界。"""
    t1 = datetime(2026, 5, 27, 12, 0, 30, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 27, 12, 1, 30, tzinfo=timezone.utc)
    t3 = datetime(2026, 5, 27, 13, 0, 0, tzinfo=timezone.utc)

    engine = _engine_with_time(t3)
    engine.set_limit(
        Limit(
            name="per_min",
            scope="user",
            metric="tool_calls",
            period="minute",
            threshold=100,
        )
    )
    engine.set_limit(
        Limit(
            name="per_hour",
            scope="user",
            metric="tool_calls",
            period="hour",
            threshold=100,
        )
    )
    engine.track("user", "alice", "tool_calls", 5.0, ts=t1)
    engine.track("user", "alice", "tool_calls", 5.0, ts=t2)
    # t3 那一刻
    engine.track("user", "alice", "tool_calls", 1.0, ts=t3)

    # 該分鐘只算 t3 那筆(13:00:00 開始)
    minute_check = [
        r for r in engine.check("user", "alice", "tool_calls", now=t3)
        if r.limit.name == "per_min"
    ]
    assert minute_check[0].current == 1.0

    # 該小時(13 點)也只算 t3 那筆
    hour_check = [
        r for r in engine.check("user", "alice", "tool_calls", now=t3)
        if r.limit.name == "per_hour"
    ]
    assert hour_check[0].current == 1.0


def test_period_all_time_accumulates_forever() -> None:
    long_ago = _fixed_time(2020, 1, 1)
    now = _fixed_time(2026, 5, 27)

    engine = _engine_with_time(now)
    engine.set_limit(
        Limit(
            name="all_time_tokens",
            scope="user",
            metric="tokens",
            period="all_time",
            threshold=10_000,
        )
    )
    engine.track("user", "alice", "tokens", 100.0, ts=long_ago)
    engine.track("user", "alice", "tokens", 200.0, ts=now)

    result = engine.check("user", "alice", "tokens", now=now)
    assert result[0].current == 300.0


# ---------------------------------------------------------------------------
# 三種 action 行為 — block / warn / throttle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action,expect_block",
    [
        ("block", True),
        ("warn", False),
        ("throttle", False),
    ],
)
def test_action_when_exceeded_variants(
    action: str, expect_block: bool
) -> None:
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(
        Limit(
            name="x",
            scope="user",
            metric="tokens",
            period="day",
            threshold=10,
            action_when_exceeded=action,  # type: ignore[arg-type]
        )
    )
    engine.track("user", "alice", "tokens", 20.0)
    res = engine.check("user", "alice", "tokens")[0]
    assert res.exceeded is True
    assert res.action == action
    assert res.should_block is expect_block


# ---------------------------------------------------------------------------
# integration with P1-15 CostTracker
# ---------------------------------------------------------------------------


def test_hook_cost_tracker_mirrors_to_limits_engine() -> None:
    registry = PricingRegistry()
    tracker = CostTracker(registry)

    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)

    unhook = hook_cost_tracker(tracker, engine)
    try:
        cost = tracker.record("gpt-4o", prompt_tokens=1000, completion_tokens=500)
        assert cost > 0  # P1-15 計算正常

        # mirror 到 agent_template scope(scope_id = model 名稱)
        token_results = engine.check("agent_template", "gpt-4o", "tokens")
        assert token_results == []  # 無 limit 設定 → 空 list

        # 拿 store 直接 query 確認 amount mirror 進去
        assert (
            tracker.total_tokens
            == 1500
        )
    finally:
        unhook()


def test_hook_cost_tracker_with_user_id_mirrors_user_scope() -> None:
    """同時給 user_id → mirror 到 user scope 用於 daily limit。"""
    registry = PricingRegistry()
    tracker = CostTracker(registry)

    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(daily_token_limit_per_user(threshold=100_000))

    unhook = hook_cost_tracker(tracker, engine, user_id="alice")
    try:
        tracker.record("gpt-4o", prompt_tokens=2000, completion_tokens=1000)
        result = engine.check("user", "alice", "tokens")
        assert result[0].current == 3000.0
    finally:
        unhook()


def test_hook_cost_tracker_unhook_restores_record() -> None:
    """unhook 後 cost_tracker.record 還原成原本實作。"""
    registry = PricingRegistry()
    tracker = CostTracker(registry)
    engine = _engine_with_time(_fixed_time(2026, 5, 27))
    engine.set_limit(daily_token_limit_per_user(threshold=100_000))

    unhook = hook_cost_tracker(tracker, engine, user_id="alice")
    tracker.record("gpt-4o", prompt_tokens=100, completion_tokens=50)
    unhook()
    tracker.record("gpt-4o", prompt_tokens=100, completion_tokens=50)

    # 第二次 record 沒走 hook → 仍記在 cost_tracker,但 engine 沒收到
    assert tracker.total_tokens == 300
    result = engine.check("user", "alice", "tokens")
    assert result[0].current == 150.0  # 只第一次的 100+50 進到 engine


def test_hook_cost_tracker_does_not_break_p1_15_record_return() -> None:
    """hook 後 record() 仍正確回傳本次 USD(不破 P1-15 API)。"""
    registry = PricingRegistry()
    tracker = CostTracker(registry)
    engine = _engine_with_time(_fixed_time(2026, 5, 27))

    hook_cost_tracker(tracker, engine)
    cost = tracker.record(
        "claude-3-5-sonnet", prompt_tokens=1000, completion_tokens=500
    )
    # claude-3-5-sonnet: 0.003 prompt + 0.015 completion / 1k
    # = 1.0 * 0.003 + 0.5 * 0.015 = 0.003 + 0.0075 = 0.0105
    assert cost == pytest.approx(0.0105)


def test_hook_cost_tracker_custom_scope_id_resolver() -> None:
    """scope_id_resolver 可從 model 字串解出 template name。"""
    registry = PricingRegistry()
    tracker = CostTracker(registry)
    engine = _engine_with_time(_fixed_time(2026, 5, 27))

    def _resolver(model: str) -> str:
        return f"tpl::{model}"

    hook_cost_tracker(tracker, engine, scope_id_resolver=_resolver)
    tracker.record("gpt-4o", prompt_tokens=100, completion_tokens=50)

    result = engine.check("agent_template", "tpl::gpt-4o", "tokens")
    # 沒設 limit → 空 list,但 query store 確認有 mirror
    store_q = engine._store.query(  # type: ignore[attr-defined]
        "agent_template",
        "tpl::gpt-4o",
        "tokens",
        "all_time",
        _fixed_time(2026, 5, 27),
    )
    assert store_q == 150.0
    assert result == []  # 沒設 limit → check 為空


# ---------------------------------------------------------------------------
# integration with P0-7 PolicyEngine
# ---------------------------------------------------------------------------


def test_limits_to_policy_rule_blocks_when_exceeded() -> None:
    now = _fixed_time(2026, 5, 27)
    limits_engine = _engine_with_time(now)
    limits_engine.set_limit(daily_token_limit_per_user(threshold=100))
    limits_engine.track("user", "alice", "tokens", 200.0)

    rule = limits_to_policy_rule(limits_engine, scope="user", scope_id="alice")
    policy = PolicyEngine()
    policy.add_rule(rule)

    decision = policy.evaluate(ctx=None, tool_name="file.read", args={})
    assert isinstance(decision, PolicyDecision)
    assert decision.denied is True
    assert decision.rule_name == "policy_limits_block"


def test_limits_to_policy_rule_allows_when_under_threshold() -> None:
    now = _fixed_time(2026, 5, 27)
    limits_engine = _engine_with_time(now)
    limits_engine.set_limit(daily_token_limit_per_user(threshold=100))
    limits_engine.track("user", "alice", "tokens", 50.0)

    rule = limits_to_policy_rule(limits_engine, scope="user", scope_id="alice")
    policy = PolicyEngine()
    policy.add_rule(rule)

    decision = policy.evaluate(ctx=None, tool_name="file.read", args={})
    assert decision.allowed is True


def test_limits_to_policy_rule_warn_does_not_deny() -> None:
    """warn 不該變成 PolicyEngine DENY — 只有 block 才會。"""
    now = _fixed_time(2026, 5, 27)
    limits_engine = _engine_with_time(now)
    limits_engine.set_limit(
        Limit(
            name="warn_only",
            scope="user",
            metric="tokens",
            period="day",
            threshold=10,
            action_when_exceeded="warn",
        )
    )
    limits_engine.track("user", "alice", "tokens", 100.0)

    rule = limits_to_policy_rule(limits_engine, scope="user", scope_id="alice")
    policy = PolicyEngine()
    policy.add_rule(rule)

    decision = policy.evaluate(ctx=None, tool_name="file.read", args={})
    assert decision.allowed is True


def test_limits_to_policy_rule_metric_filter() -> None:
    """metrics filter 可限定只看部分 metric。"""
    now = _fixed_time(2026, 5, 27)
    limits_engine = _engine_with_time(now)
    limits_engine.set_limit(
        Limit(
            name="cost_only",
            scope="user",
            metric="cost_usd",
            period="day",
            threshold=1.0,
        )
    )
    limits_engine.track("user", "alice", "cost_usd", 5.0)

    # 過濾掉 cost_usd metric → 不會被 DENY
    rule = limits_to_policy_rule(
        limits_engine,
        scope="user",
        scope_id="alice",
        metrics=["tokens"],  # 排除 cost_usd
    )
    policy = PolicyEngine()
    policy.add_rule(rule)
    assert policy.evaluate(ctx=None, tool_name="x", args={}).allowed is True

    # 反之開放所有 metric → 會 DENY
    rule_full = limits_to_policy_rule(
        limits_engine,
        scope="user",
        scope_id="alice",
        name="full_rule",
    )
    policy2 = PolicyEngine()
    policy2.add_rule(rule_full)
    assert policy2.evaluate(ctx=None, tool_name="x", args={}).denied is True


def test_limits_to_policy_rule_tool_pattern_scoped() -> None:
    """tool_pattern 可指定只擋部分 tool。"""
    now = _fixed_time(2026, 5, 27)
    limits_engine = _engine_with_time(now)
    limits_engine.set_limit(daily_token_limit_per_user(threshold=10))
    limits_engine.track("user", "alice", "tokens", 100.0)

    rule = limits_to_policy_rule(
        limits_engine,
        scope="user",
        scope_id="alice",
        tool_pattern="shell.run",
    )
    policy = PolicyEngine()
    policy.add_rule(rule)

    # shell.run 命中
    assert policy.evaluate(ctx=None, tool_name="shell.run", args={}).denied is True
    # file.read 不在 pattern,走 default ALLOW
    assert policy.evaluate(ctx=None, tool_name="file.read", args={}).allowed is True


# ---------------------------------------------------------------------------
# 常用 factory
# ---------------------------------------------------------------------------


def test_daily_token_limit_factory_defaults() -> None:
    limit = daily_token_limit_per_user()
    assert limit.scope == "user"
    assert limit.metric == "tokens"
    assert limit.period == "day"
    assert limit.threshold == 100_000.0
    assert limit.action_when_exceeded == "block"
    assert limit.scope_id is None


def test_daily_token_limit_factory_custom_threshold() -> None:
    limit = daily_token_limit_per_user(threshold=50_000, scope_id="alice")
    assert limit.threshold == 50_000
    assert limit.scope_id == "alice"


def test_monthly_cost_limit_factory_defaults() -> None:
    limit = monthly_cost_limit_per_agent()
    assert limit.scope == "agent_template"
    assert limit.metric == "cost_usd"
    assert limit.period == "month"
    assert limit.threshold == 1000.0
    assert limit.action_when_exceeded == "block"


def test_monthly_cost_limit_factory_custom() -> None:
    limit = monthly_cost_limit_per_agent(
        threshold_usd=500,
        action="warn",
        scope_id="my_tpl",
    )
    assert limit.threshold == 500
    assert limit.action_when_exceeded == "warn"
    assert limit.scope_id == "my_tpl"


# ---------------------------------------------------------------------------
# active_sessions metric — 支援負累加
# ---------------------------------------------------------------------------


def test_active_sessions_supports_negative_amount() -> None:
    """active_sessions metric:track +1 開始 / -1 結束。"""
    now = _fixed_time(2026, 5, 27)
    engine = _engine_with_time(now)
    engine.set_limit(
        Limit(
            name="max_sessions",
            scope="user",
            metric="active_sessions",
            period="all_time",
            threshold=5,
        )
    )
    # 開三個 session
    for _ in range(3):
        engine.track("user", "alice", "active_sessions", 1.0)
    result = engine.check("user", "alice", "active_sessions")
    assert result[0].current == 3.0
    assert result[0].exceeded is False

    # 結束一個
    engine.track("user", "alice", "active_sessions", -1.0)
    result = engine.check("user", "alice", "active_sessions")
    assert result[0].current == 2.0
