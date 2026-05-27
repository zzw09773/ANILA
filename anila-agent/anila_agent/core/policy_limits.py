"""P2-8 policyLimits — 組織級政策限制 (cross-session limits engine)。

本模組對應 enhancement roadmap §4.18 / P2-8,把上游
``claude-code-src/src/services/policyLimits/index.ts`` 的「organisation-level
limits」概念 port 成 Python,並擴成獨立可重用的子系統。

# 與 P0-7 PolicyEngine 的差別

P0-7 :class:`anila_agent.core.policy.PolicyEngine` 是 **per-call 決策**:
「這個 tool call 是否 allow / deny / disable」。本 module 是 **跨 session 的
組織級限制**:

* 每 user / department 每天最多 N 個 token
* 每 sub-agent template 每月最多 USD 100
* 同 user 同時最多 5 個 active session
* 每分鐘最多 X 個 tool call(rate limit 性質)

兩者透過 :func:`limits_to_policy_rule` adapter 銜接 — 當組織級 limit 超標時,
我們把該 scope 內所有 tool 變成 DENY rule 餵進 PolicyEngine,即時阻擋下一個
tool call。**不修改** P0-7 本體,維持單向依賴(policy_limits → policy)。

# 與 P1-15 CostTracker 的銜接

:class:`anila_agent.core.cost_tracker.CostTracker` 是純「累加 + 算錢」資料容器,
本模組只「在它 record 時順手 mirror 一份到 LimitsEngine」,透過
:func:`hook_cost_tracker` 把一個 callback 接到既有 ``CostTracker`` 上,不
修改 P1-15 本體 API、不 monkey-patch。

# 概念對齊上游 TS 但子集化

上游 ``policyLimits`` 還做了 ETag cache / 背景 poll / fetch from server。
本 P2-8 port 不做 server fetch(那是 platform backend 工作),只做
**local in-memory + JSON 檔持久化 + period rollover** 的核心引擎,
讓 caller(ANILA backend / runner / cli)各自決定要不要接 server。

# 設計取捨

* **零外部相依** — 只用 std lib;period 計算用 ``datetime`` 內建邏輯。
* **threading** — 不主動加 lock。預期單一 backend process 內順序 record;
  並發 record 由 caller 用 ``threading.Lock`` 或 ``asyncio.Lock`` 包外層。
* **fake time injection** — 所有時間取 ``time_provider`` callable,測試
  可注入固定時間做 period rollover 測試,不依賴 ``time.sleep``。
* **不 mutate ts** — :class:`_Record` 為 frozen dataclass,store 與 engine
  間傳遞時不會被偷改。
* **fail open** — 取 limit metadata 失敗(JSON 檔壞掉)只記 log + 回空集合,
  不丟例外,避免一個壞檔卡死整個 runtime。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from anila_agent.core.policy import PolicyEffect, PolicyRule

if TYPE_CHECKING:
    from anila_agent.core.cost_tracker import CostTracker

# ---------------------------------------------------------------------------
# 型別 / Literal
# ---------------------------------------------------------------------------

#: limit 套用的 scope 維度。
#: - ``user`` — 每 user 一份累計(scope_id = user_id)。
#: - ``department`` — 每部門一份(scope_id = dept_id)。
#: - ``agent_template`` — 每 sub-agent template 一份(scope_id = template_name)。
#: - ``global`` — 全 organisation 一份(scope_id 固定 "global")。
LimitScope = Literal["user", "department", "agent_template", "global"]

#: limit 追蹤的指標。
#: - ``tokens`` — LLM token 用量(prompt + completion 合計)。
#: - ``cost_usd`` — 折算後的 USD。
#: - ``active_sessions`` — 同時開著的 session 數(track = +1, untrack = -1)。
#: - ``tool_calls`` — tool call 次數(rate limit 性質)。
#: - ``request_count`` — 廣義 request 計數(API call / 上傳 / 任何活動)。
LimitMetric = Literal[
    "tokens",
    "cost_usd",
    "active_sessions",
    "tool_calls",
    "request_count",
]

#: limit 的累計時段。
#: - ``all_time`` — 不歸零,從建檔起累計。
#: - ``minute`` / ``hour`` / ``day`` / ``week`` / ``month`` — 滾動視窗(以 UTC
#:   為基準切;週以 ISO 週切,月以同月年切)。
LimitPeriod = Literal[
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "all_time",
]

#: 超標後的處置。
#: - ``block`` — 直接擋住下一次 record / tool call。
#: - ``warn`` — 不擋,但 :class:`LimitCheckResult.action` 帶 warn 旗標,
#:   讓 caller 自行決定是否 surface 給 user。
#: - ``throttle`` — caller 自行延遲;本模組只回 throttle 旗標,不做 sleep。
LimitAction = Literal["block", "warn", "throttle"]


# ---------------------------------------------------------------------------
# Limit dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limit:
    """單一組織級 limit 的設定。

    Attributes:
        name: limit 名稱,store 內 key,出現在 log / audit。
        scope: 套用維度(user / department / agent_template / global)。
        metric: 追蹤指標(tokens / cost_usd / ...)。
        period: 累計時段(day / month / all_time / ...)。
        threshold: 觸發 action 的閾值(float;tokens 等整數量級也用 float 表達)。
        action_when_exceeded: 超標處置(block / warn / throttle)。
        scope_id: 此 limit 套用的 specific scope id;None 表示 wildcard
            (對該 scope 維度下所有 id 都套用,例如「所有 user 一律每天 100k token」)。

    Raises:
        ValueError: threshold < 0 或 name 為空。
    """

    name: str
    scope: LimitScope
    metric: LimitMetric
    period: LimitPeriod
    threshold: float
    action_when_exceeded: LimitAction = "block"
    scope_id: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Limit.name must be a non-empty string")
        if self.threshold < 0:
            raise ValueError(
                f"Limit.threshold must be >= 0, got {self.threshold}"
            )

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe dict(供 store 持久化)。"""
        return {
            "name": self.name,
            "scope": self.scope,
            "metric": self.metric,
            "period": self.period,
            "threshold": self.threshold,
            "action_when_exceeded": self.action_when_exceeded,
            "scope_id": self.scope_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Limit:
        """從 JSON dict 重建 Limit(roundtrip 用)。"""
        return cls(
            name=data["name"],
            scope=data["scope"],
            metric=data["metric"],
            period=data["period"],
            threshold=float(data["threshold"]),
            action_when_exceeded=data.get("action_when_exceeded", "block"),
            scope_id=data.get("scope_id"),
        )


# ---------------------------------------------------------------------------
# LimitCheckResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LimitCheckResult:
    """:meth:`LimitsEngine.check` 的回傳結構。

    Attributes:
        limit: 對應的 Limit 設定。
        current: 當前累計量。
        threshold: 閾值(=Limit.threshold)。
        exceeded: 是否已 ``current >= threshold``。
        action: 若 exceeded 該採取的 action;未 exceeded 為 None。
    """

    limit: Limit
    current: float
    threshold: float
    exceeded: bool
    action: LimitAction | None = None

    @property
    def remaining(self) -> float:
        """還剩下多少配額(不會 < 0)。"""
        return max(0.0, self.threshold - self.current)

    @property
    def usage_ratio(self) -> float:
        """當前用量比例;threshold = 0 時定義為 1.0(避免 div0)。"""
        if self.threshold <= 0:
            return 1.0
        return self.current / self.threshold

    @property
    def should_block(self) -> bool:
        """是否該 block 後續操作(action == block 且 exceeded)。"""
        return self.exceeded and self.action == "block"


# ---------------------------------------------------------------------------
# 內部 _Record(累計事件)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Record:
    """單筆 track 事件 — store 內部用,不對外 export。

    Attributes:
        scope: 套用維度。
        scope_id: 對應 scope 的 id(user_id / dept_id / template_name)。
        metric: 指標。
        amount: 累加量(active_sessions 可為 +1 / -1)。
        ts: UTC 時間戳。
    """

    scope: LimitScope
    scope_id: str
    metric: LimitMetric
    amount: float
    ts: datetime


# ---------------------------------------------------------------------------
# Period rollover 計算
# ---------------------------------------------------------------------------


def _period_start(period: LimitPeriod, now: datetime) -> datetime | None:
    """算出 ``period`` 在 ``now`` 當下的起始邊界。

    返回值的語意:**只統計 ts >= period_start 的紀錄**。

    對應規則:

    * ``minute`` — 同分鐘內。
    * ``hour`` — 同小時內。
    * ``day`` — 同 UTC 日內。
    * ``week`` — 同 ISO 週內(週一 00:00 UTC 為起點)。
    * ``month`` — 同年月內(month 1 號 00:00 UTC 為起點)。
    * ``all_time`` — 回 None,表示無下限,累計全部。
    """
    if period == "all_time":
        return None
    if period == "minute":
        return now.replace(second=0, microsecond=0)
    if period == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        # ISO weekday: 週一 = 1, 週日 = 7。回 weekday-1 天前的 00:00 UTC。
        weekday = now.isoweekday()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start - timedelta(days=weekday - 1)
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # pragma: no cover — Literal 已限制
    raise ValueError(f"unknown period: {period!r}")


# ---------------------------------------------------------------------------
# LimitsStore Protocol + in-memory + JSON
# ---------------------------------------------------------------------------


class LimitsStore(Protocol):
    """組織級 limits 的儲存抽象。

    支援兩類資料:

    1. **紀錄 (record)** — 每次 ``track()`` 寫一筆 :class:`_Record`;
       ``query()`` 依 ``period`` 切回累計值。
    2. **設定 (limit)** — :meth:`set_limit` / :meth:`get_limit` /
       :meth:`list_limits` 管理 :class:`Limit` 本身。

    Notes:
        store 是 in-memory 或檔案後端的差異對 caller 透明;
        :class:`InMemoryLimitsStore` 用於測試 / 短命 process,
        :class:`JsonFileLimitsStore` 用於持久化。
    """

    def record(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        amount: float,
        ts: datetime,
    ) -> None:
        """寫入一筆累計事件。"""
        ...

    def query(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        period: LimitPeriod,
        now: datetime,
    ) -> float:
        """查詢 (scope, scope_id, metric) 在 period 內的累計值。"""
        ...

    def set_limit(self, limit: Limit) -> None:
        """新增 / 覆寫一條 limit 設定。"""
        ...

    def get_limit(self, name: str) -> Limit | None:
        """依 name 查 limit;不存在回 None。"""
        ...

    def list_limits(self) -> list[Limit]:
        """列出所有 limit 設定(順序不保證)。"""
        ...


class InMemoryLimitsStore:
    """In-memory store — 預設 backend,測試與短命 process 使用。

    資料結構:

    * ``_records: list[_Record]`` — 全部累計事件;query 時走線性過濾。
      若紀錄量大,可改 dict-of-deque(以 (scope, scope_id, metric) 為 key),
      本 P2-8 P2 不做此 optimisation。
    * ``_limits: dict[str, Limit]`` — limit 設定,name → Limit。
    """

    def __init__(self) -> None:
        self._records: list[_Record] = []
        self._limits: dict[str, Limit] = {}

    # ----------------------------------------------------------- record API

    def record(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        amount: float,
        ts: datetime,
    ) -> None:
        self._records.append(
            _Record(
                scope=scope,
                scope_id=scope_id,
                metric=metric,
                amount=amount,
                ts=ts,
            )
        )

    def query(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        period: LimitPeriod,
        now: datetime,
    ) -> float:
        start = _period_start(period, now)
        total = 0.0
        for rec in self._records:
            if rec.scope != scope:
                continue
            if rec.scope_id != scope_id:
                continue
            if rec.metric != metric:
                continue
            if start is not None and rec.ts < start:
                continue
            total += rec.amount
        return total

    # ----------------------------------------------------------- limit API

    def set_limit(self, limit: Limit) -> None:
        self._limits[limit.name] = limit

    def get_limit(self, name: str) -> Limit | None:
        return self._limits.get(name)

    def list_limits(self) -> list[Limit]:
        return list(self._limits.values())

    # --------------------------------------------------------- 測試 utility

    def clear(self) -> None:
        """歸零所有 records 與 limits;測試 isolation 用。"""
        self._records.clear()
        self._limits.clear()


class JsonFileLimitsStore:
    """把 records + limits 序列化成 .json 檔的 store。

    檔案 layout(單檔)::

        <path> = {
          "limits": [<Limit.to_dict()>, ...],
          "records": [
            {
              "scope": "user",
              "scope_id": "alice",
              "metric": "tokens",
              "amount": 1000.0,
              "ts": "2026-05-27T03:00:00+00:00"
            }, ...
          ]
        }

    設計取捨:

    * **不做 file lock** — 跟 P1-8 :class:`JsonFileStateStore` 一致,
      預設單 process 順序寫;多 process 請另外 wrap atomic rename + flock。
    * **每次 record 即落檔** — 簡單但寫放大,對 P2-8 量級可接受;
      大量寫入時 caller 可包 in-memory backend + 週期 flush。
    * **fail open on load** — 檔案不存在 / 壞掉時用空 state 開機(不丟例外),
      caller 透過 :attr:`load_errors` 拿到診斷訊息。

    Args:
        path: 持久化檔案路徑;父目錄若不存在會自動建立。
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records: list[_Record] = []
        self._limits: dict[str, Limit] = {}
        self._load_errors: list[str] = []
        self._load()

    @property
    def path(self) -> Path:
        """持久化檔案路徑(唯讀 view)。"""
        return self._path

    @property
    def load_errors(self) -> list[str]:
        """載入 .json 時遇到的所有可恢復錯誤訊息(供診斷)。"""
        return list(self._load_errors)

    # ----------------------------------------------------------- 內部 IO

    def _load(self) -> None:
        """從 .json 載入 records + limits;檔案不存在或壞掉時走 fail-open。"""
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            self._load_errors.append(f"failed to load {self._path}: {e}")
            return

        if not isinstance(data, dict):
            self._load_errors.append(
                f"top-level of {self._path} must be object, got {type(data).__name__}"
            )
            return

        # limits
        for raw in data.get("limits", []) or []:
            try:
                limit = Limit.from_dict(raw)
            except (KeyError, ValueError, TypeError) as e:
                self._load_errors.append(f"skip invalid limit {raw!r}: {e}")
                continue
            self._limits[limit.name] = limit

        # records
        for raw in data.get("records", []) or []:
            try:
                rec = _Record(
                    scope=raw["scope"],
                    scope_id=raw["scope_id"],
                    metric=raw["metric"],
                    amount=float(raw["amount"]),
                    ts=datetime.fromisoformat(raw["ts"]),
                )
            except (KeyError, ValueError, TypeError) as e:
                self._load_errors.append(f"skip invalid record {raw!r}: {e}")
                continue
            self._records.append(rec)

    def _flush(self) -> None:
        """把當前 records + limits 寫回 .json。"""
        payload = {
            "limits": [limit.to_dict() for limit in self._limits.values()],
            "records": [
                {
                    "scope": rec.scope,
                    "scope_id": rec.scope_id,
                    "metric": rec.metric,
                    "amount": rec.amount,
                    "ts": rec.ts.isoformat(),
                }
                for rec in self._records
            ],
        }
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )

    # ----------------------------------------------------------- 對外 API

    def record(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        amount: float,
        ts: datetime,
    ) -> None:
        self._records.append(
            _Record(
                scope=scope,
                scope_id=scope_id,
                metric=metric,
                amount=amount,
                ts=ts,
            )
        )
        self._flush()

    def query(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        period: LimitPeriod,
        now: datetime,
    ) -> float:
        start = _period_start(period, now)
        total = 0.0
        for rec in self._records:
            if rec.scope != scope:
                continue
            if rec.scope_id != scope_id:
                continue
            if rec.metric != metric:
                continue
            if start is not None and rec.ts < start:
                continue
            total += rec.amount
        return total

    def set_limit(self, limit: Limit) -> None:
        self._limits[limit.name] = limit
        self._flush()

    def get_limit(self, name: str) -> Limit | None:
        return self._limits.get(name)

    def list_limits(self) -> list[Limit]:
        return list(self._limits.values())


# ---------------------------------------------------------------------------
# LimitsEngine
# ---------------------------------------------------------------------------


#: time provider 簽名 — 回傳 timezone-aware UTC datetime。
TimeProvider = Callable[[], datetime]


def _default_now() -> datetime:
    """預設 time provider — 回 UTC now。"""
    return datetime.now(timezone.utc)


class LimitsEngine:
    """組織級限制引擎 — 同時管理 limits 設定與累計 + 提供 check API。

    用法::

        store = InMemoryLimitsStore()
        engine = LimitsEngine(store)
        engine.set_limit(daily_token_limit_per_user(threshold=100_000))

        engine.track(scope="user", scope_id="alice", metric="tokens", amount=500)
        result = engine.check(scope="user", scope_id="alice", metric="tokens")
        if result.should_block:
            ...  # 拒絕

    Args:
        store: 持久化後端;預設 :class:`InMemoryLimitsStore`。
        time_provider: 取得當下 UTC datetime 的 callable;測試可注入固定時間。
    """

    def __init__(
        self,
        store: LimitsStore | None = None,
        *,
        time_provider: TimeProvider = _default_now,
    ) -> None:
        self._store: LimitsStore = store if store is not None else InMemoryLimitsStore()
        self._time_provider = time_provider

    # ------------------------------------------------------------- limit 管理

    def set_limit(self, limit: Limit) -> None:
        """設定一條 limit(同 name 覆寫)。"""
        self._store.set_limit(limit)

    def get_limit(self, name: str) -> Limit | None:
        """依 name 查 limit。"""
        return self._store.get_limit(name)

    def list_limits(self) -> list[Limit]:
        """列出所有 limit 設定。"""
        return self._store.list_limits()

    # ---------------------------------------------------------------- track

    def track(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        amount: float,
        *,
        ts: datetime | None = None,
    ) -> None:
        """累加一筆用量。

        Args:
            scope: 套用維度。
            scope_id: 該維度下的 id(user_id / dept_id / template_name /
                "global")。
            metric: 指標(tokens / cost_usd / ...)。
            amount: 累加量;支援負數(active_sessions untrack 用)。
            ts: 該事件時間戳;None 時用 ``time_provider()``。
        """
        actual_ts = ts if ts is not None else self._time_provider()
        if actual_ts.tzinfo is None:
            # 強制 UTC,確保 period rollover 計算一致。
            actual_ts = actual_ts.replace(tzinfo=timezone.utc)
        self._store.record(scope, scope_id, metric, amount, actual_ts)

    # ---------------------------------------------------------------- check

    def check(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        *,
        now: datetime | None = None,
    ) -> list[LimitCheckResult]:
        """檢查 (scope, scope_id, metric) 下所有相關 limit 的狀態。

        Args:
            scope: 套用維度。
            scope_id: 維度下的 id。
            metric: 指標。
            now: 評估時間;None 時用 ``time_provider()``。

        Returns:
            符合 (scope, metric) 的 :class:`LimitCheckResult` list。
            「符合」= limit.scope == scope **且** limit.metric == metric **且**
            (limit.scope_id 為 None 或 == scope_id)。沒任何符合 limit 時回空 list。
        """
        actual_now = now if now is not None else self._time_provider()
        results: list[LimitCheckResult] = []
        for limit in self._store.list_limits():
            if limit.scope != scope or limit.metric != metric:
                continue
            if limit.scope_id is not None and limit.scope_id != scope_id:
                continue
            current = self._store.query(
                scope, scope_id, metric, limit.period, actual_now
            )
            exceeded = current >= limit.threshold
            results.append(
                LimitCheckResult(
                    limit=limit,
                    current=current,
                    threshold=limit.threshold,
                    exceeded=exceeded,
                    action=limit.action_when_exceeded if exceeded else None,
                )
            )
        return results

    def check_all(
        self,
        scope: LimitScope,
        scope_id: str,
        *,
        now: datetime | None = None,
    ) -> list[LimitCheckResult]:
        """一次跑該 (scope, scope_id) 下所有 metric 的所有 limit。

        Args:
            scope: 套用維度。
            scope_id: 維度下的 id。
            now: 評估時間;None 時用 ``time_provider()``。

        Returns:
            所有符合 limit 的 check result(可能跨多個 metric)。
        """
        actual_now = now if now is not None else self._time_provider()
        results: list[LimitCheckResult] = []
        for limit in self._store.list_limits():
            if limit.scope != scope:
                continue
            if limit.scope_id is not None and limit.scope_id != scope_id:
                continue
            current = self._store.query(
                scope, scope_id, limit.metric, limit.period, actual_now
            )
            exceeded = current >= limit.threshold
            results.append(
                LimitCheckResult(
                    limit=limit,
                    current=current,
                    threshold=limit.threshold,
                    exceeded=exceeded,
                    action=limit.action_when_exceeded if exceeded else None,
                )
            )
        return results

    def is_blocked(
        self,
        scope: LimitScope,
        scope_id: str,
        metric: LimitMetric,
        *,
        now: datetime | None = None,
    ) -> bool:
        """便利 helper — 該 (scope, scope_id, metric) 是否有任一 limit 應 block。"""
        return any(
            r.should_block
            for r in self.check(scope, scope_id, metric, now=now)
        )


# ---------------------------------------------------------------------------
# integration with P1-15 CostTracker
# ---------------------------------------------------------------------------


def hook_cost_tracker(
    cost_tracker: CostTracker,
    limits_engine: LimitsEngine,
    *,
    scope: LimitScope = "agent_template",
    scope_id_resolver: Callable[[str], str] | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
) -> Callable[[], None]:
    """把 LimitsEngine 接到既有 CostTracker 的 ``record`` callback 鏈。

    **不修改** :class:`anila_agent.core.cost_tracker.CostTracker` 的 API;
    使用 monkey-wrap 包既有 ``record`` 方法,在不破壞 P1-15 任何測試的前提下
    多 fire 一次 :meth:`LimitsEngine.track`。

    每次 cost_tracker.record(model, prompt, completion) 後:

    1. 算出實際 tokens(prompt + completion)與 USD 成本(用 cost_tracker 內建 pricing)。
    2. 對指定 scope_id 上 track 兩筆:tokens + cost_usd。
    3. 如果 caller 同時給 ``user_id``,額外 mirror 一份到 user scope。

    Args:
        cost_tracker: 既有的 :class:`CostTracker` 實例。
        limits_engine: 要 mirror 進去的 engine。
        scope: 主 scope 維度;預設 ``agent_template``。
        scope_id_resolver: 從 ``model`` 字串解出 scope_id 的 callable;
            None 時用 model 名稱本身當 scope_id。
        user_id: 若給,額外 mirror 一份到 ``user`` scope。
        agent_id: 預留參數(留給未來 mirror 到 ``agent_template`` scope 的
            另一個 scope_id;目前 reserved,未使用)。

    Returns:
        un-hook callable;呼叫後還原 cost_tracker.record 為原本實作,
        測試 / 動態關閉 hook 用。
    """
    original_record = cost_tracker.record
    resolver = scope_id_resolver if scope_id_resolver is not None else lambda m: m
    _ = agent_id  # reserved; 預留給未來 scope mirror

    def _hooked_record(
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        cost = original_record(model, prompt_tokens, completion_tokens)
        total_tokens = prompt_tokens + completion_tokens
        scope_id = resolver(model)
        limits_engine.track(
            scope=scope,
            scope_id=scope_id,
            metric="tokens",
            amount=float(total_tokens),
        )
        limits_engine.track(
            scope=scope,
            scope_id=scope_id,
            metric="cost_usd",
            amount=cost,
        )
        if user_id is not None:
            limits_engine.track(
                scope="user",
                scope_id=user_id,
                metric="tokens",
                amount=float(total_tokens),
            )
            limits_engine.track(
                scope="user",
                scope_id=user_id,
                metric="cost_usd",
                amount=cost,
            )
        return cost

    # 安裝 wrap;cost_tracker.record 動態指向新 callable。
    cost_tracker.record = _hooked_record  # type: ignore[method-assign]

    def _unhook() -> None:
        cost_tracker.record = original_record  # type: ignore[method-assign]

    return _unhook


# ---------------------------------------------------------------------------
# integration with P0-7 PolicyEngine
# ---------------------------------------------------------------------------


def limits_to_policy_rule(
    limits_engine: LimitsEngine,
    scope: LimitScope,
    scope_id: str,
    *,
    name: str = "policy_limits_block",
    priority: int = 900,
    tool_pattern: str | None = None,
    metrics: Iterable[LimitMetric] | None = None,
) -> PolicyRule:
    """把組織級 limits 轉成一條 P0-7 :class:`PolicyRule`(DENY effect)。

    Rule 的 condition 在每次 tool call 時動態 query limits_engine:
    若 (scope, scope_id) 下任一 metric 處於 ``block`` 狀態,condition 回 True
    → tool 被 DENY。其他 effect(warn / throttle)**不**透過此 adapter
    轉為 PolicyEngine deny,因為 caller 可能想保留執行只記 log。

    Args:
        limits_engine: 已掛 store 的 engine 實例。
        scope: 套用維度。
        scope_id: 維度下的 id。
        name: PolicyRule.name。
        priority: PolicyRule.priority;預設 900(高於一般 allow rule,確保命中)。
        tool_pattern: 哪些 tool 套用;None / "*" 表示全 tool。
        metrics: 要納入判斷的 metric 子集;None 表示全 metric。

    Returns:
        :class:`PolicyRule`,可直接 ``engine.add_rule(...)`` 進 PolicyEngine。

    Example::

        policy_engine.add_rule(
            limits_to_policy_rule(
                limits_engine,
                scope="user",
                scope_id="alice",
            )
        )
        # alice 今天 token 用完時,任何 tool call 都會被 P0-7 deny。
    """
    metric_filter: set[LimitMetric] | None = (
        set(metrics) if metrics is not None else None
    )

    def _condition(ctx: Any, tool_name: str, args: dict[str, Any]) -> bool:
        _ = ctx  # condition 只看組織級狀態,跟 ctx 無關
        _ = tool_name
        _ = args
        for result in limits_engine.check_all(scope, scope_id):
            if metric_filter is not None and result.limit.metric not in metric_filter:
                continue
            if result.should_block:
                return True
        return False

    return PolicyRule(
        name=name,
        tool_pattern=tool_pattern,
        effect=PolicyEffect.DENY,
        priority=priority,
        condition=_condition,
        reason=(
            f"organisation policy limit exceeded for "
            f"{scope}={scope_id!r}"
        ),
    )


# ---------------------------------------------------------------------------
# 常用 limit factory
# ---------------------------------------------------------------------------


def daily_token_limit_per_user(
    threshold: float = 100_000.0,
    *,
    name: str = "daily_token_per_user",
    action: LimitAction = "block",
    scope_id: str | None = None,
) -> Limit:
    """組「每 user 每天最多 N 個 token」的常用 Limit。

    Args:
        threshold: token 上限(預設 100k)。
        name: limit name(預設 ``daily_token_per_user``)。
        action: 超標處置(預設 block)。
        scope_id: 指定 user_id;None 代表對所有 user 通用。

    Returns:
        :class:`Limit`,可直接 ``engine.set_limit(...)``。
    """
    return Limit(
        name=name,
        scope="user",
        metric="tokens",
        period="day",
        threshold=threshold,
        action_when_exceeded=action,
        scope_id=scope_id,
    )


def monthly_cost_limit_per_agent(
    threshold_usd: float = 1000.0,
    *,
    name: str = "monthly_cost_per_agent",
    action: LimitAction = "block",
    scope_id: str | None = None,
) -> Limit:
    """組「每 agent_template 每月最多 N USD」的常用 Limit。

    Args:
        threshold_usd: USD 上限(預設 1000)。
        name: limit name(預設 ``monthly_cost_per_agent``)。
        action: 超標處置(預設 block)。
        scope_id: 指定 agent template 名稱;None 代表對所有 template 通用。

    Returns:
        :class:`Limit`,可直接 ``engine.set_limit(...)``。
    """
    return Limit(
        name=name,
        scope="agent_template",
        metric="cost_usd",
        period="month",
        threshold=threshold_usd,
        action_when_exceeded=action,
        scope_id=scope_id,
    )


# ---------------------------------------------------------------------------
# 顯式對外 API list
# ---------------------------------------------------------------------------


__all__ = [
    # types
    "Limit",
    "LimitAction",
    "LimitCheckResult",
    "LimitMetric",
    "LimitPeriod",
    "LimitScope",
    # engine + store
    "InMemoryLimitsStore",
    "JsonFileLimitsStore",
    "LimitsEngine",
    "LimitsStore",
    "TimeProvider",
    # adapters
    "hook_cost_tracker",
    "limits_to_policy_rule",
    # factories
    "daily_token_limit_per_user",
    "monthly_cost_limit_per_agent",
]
