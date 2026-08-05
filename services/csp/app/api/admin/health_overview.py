"""服務健康總覽(P3.3 / attic W3-3⑦)—— ``GET /api/admin/health/overview``。

為什麼有這支端點
----------------
稽核的 admin-journey D1:**22 個治理視圖沒有一個**顯示 router /
anila-studio / ingestion-worker / csp-db / redis / nginx / pptx-renderer 的
狀態(``健康總覽|服務健康`` 全域命中 0)。管理者要知道「平台現在活著嗎」,
唯一的真實工具是 SSH 進平台主機跑 ``anila-ops.sh health``。這支端點是那件事
的直接解:治理首頁一張卡就能回答。

狀態字彙沿用 ``app.services.health_checker`` 的五態
(``unknown / healthy / degraded / unhealthy / disabled``),不另發明一套 ——
model / agent 清單頁與這張卡講的必須是同一種話。

安全姿態(驗收會逐欄看)
------------------------
1. ``require_admin`` —— 健康/metrics 端點洩內部資訊是規格點名的風險。
2. **回應是白名單**:只有服務名、繁中顯示名、kind、五態、bounded reason、
   latency、``checked_at``。連線字串 / 內部 IP / port / 探測 URL / 版本號 /
   後端例外訊息**一個都不出**。細節只進 log。
3. **探測目標不可由呼叫端指定**:清單寫死在 ``health_checker``,而且本端點
   宣告零查詢參數並對任何查詢參數 fail-closed 回 400 —— 不讓它退化成一支
   掛在 admin 身分後面的內網掃描代理。

連線池姿態(本樹 session-release 不變式)
--------------------------------------
DB 探測與 registry 計數先做完並 ``commit`` 釋放交易,再跑 HTTP/redis 出向
探測。不得把請求的 SQLAlchemy session 跨在 outbound await 上(與
``health_checker`` 背景迴圈同一條教訓)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.auth_service import require_admin
from app.services.health_checker import (
    BASE_SERVICE_SPECS,
    HEALTH_HEALTHY,
    HEALTH_UNKNOWN,
    PROBE_UNSUPPORTED,
    aggregate_health,
    probe_base_service,
    summarize_five_state,
)

router = APIRouter(prefix="/api/admin/health", tags=["服務健康"])

_LOCAL_PROBE_NAMES = frozenset({"csp", "csp-db"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ServiceHealthEntry(BaseModel):
    """單一基礎服務。**這個 model 就是白名單** —— 不要加欄位。

    尤其不要加 ``endpoint`` / ``host`` / ``port`` / ``error``:前三個是部署
    細節,最後一個會把 psycopg / httpx 的錯誤字串(裡面有連線字串)送到瀏覽器。
    """

    name: str = Field(description="compose 服務名")
    label: str = Field(description="繁中顯示名")
    kind: str = Field(description="self / database / cache / http / queue")
    status: str = Field(description="五態:unknown/healthy/degraded/unhealthy/disabled")
    reason: str = Field(description="bounded 原因碼,非自由文字")
    latency_ms: int
    checked_at: str


class RegistryHealthCounts(BaseModel):
    """model / agent 註冊表的五態計數。只有數字,沒有名稱與端點。"""

    total: int
    unknown: int
    healthy: int
    degraded: int
    unhealthy: int
    disabled: int


class HealthOverviewResponse(BaseModel):
    overall: str
    checked_at: str
    services: list[ServiceHealthEntry]
    models: RegistryHealthCounts | None = None
    agents: RegistryHealthCounts | None = None


def _reject_caller_supplied_targets(request: Request) -> None:
    """任何查詢參數都拒。

    這支端點**沒有**任何合法參數,所以「安靜忽略未知參數」與「明確拒絕」在
    功能上等價,但語意差很多:明確拒絕讓「探測目標不可由呼叫端指定」成為
    端點契約的一部分,而不是一句靠 code review 維持的口頭承諾。
    有人日後想加 ``?host=`` 時,會先撞到這裡與釘住它的測試。
    """
    if not request.query_params:
        return
    keys = ", ".join(sorted(set(request.query_params.keys())))
    raise HTTPException(
        status_code=400,
        detail=(
            "服務健康總覽不接受查詢參數（收到："
            f"{keys}）。探測目標是部署內建的固定清單，不可由呼叫端指定。"
        ),
    )


def _registry_counts(db: Session) -> tuple[RegistryHealthCounts, RegistryHealthCounts]:
    model_rows = db.query(
        ModelRegistry.health_status, ModelRegistry.is_active
    ).all()
    agent_rows = db.query(Agent.health_status, Agent.approval_status).all()
    return (
        RegistryHealthCounts(**summarize_five_state(model_rows)),
        RegistryHealthCounts(
            **summarize_five_state(
                [(status, approval == "approved") for status, approval in agent_rows]
            )
        ),
    )


def _entry_from_result(spec, result, checked_at: str) -> ServiceHealthEntry:
    if isinstance(result, BaseException):
        # probe 本身不該漏例外;漏了也只降級這一列,不炸整張卡。
        return ServiceHealthEntry(
            name=spec.name,
            label=spec.label,
            kind=spec.kind,
            status=HEALTH_UNKNOWN,
            reason=PROBE_UNSUPPORTED,
            latency_ms=0,
            checked_at=checked_at,
        )
    return ServiceHealthEntry(
        name=spec.name,
        label=spec.label,
        kind=spec.kind,
        status=result.status,
        reason=result.reason,
        latency_ms=result.latency_ms,
        checked_at=checked_at,
    )


@router.get("/overview", response_model=HealthOverviewResponse)
async def health_overview(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> HealthOverviewResponse:
    """彙總基礎服務 + model/agent 註冊表的健康狀態。

    **單一服務探測失敗不得讓總覽本身失敗** —— 那樣管理者在最需要這張卡的時候
    (東西正在壞)反而什麼都看不到。所以每個 probe 都自己吃掉例外回一個紅/黃
    狀態,``gather`` 也帶 ``return_exceptions=True`` 當最後一道網。
    """
    _reject_caller_supplied_targets(request)

    specs = BASE_SERVICE_SPECS
    local_specs = [s for s in specs if s.name in _LOCAL_PROBE_NAMES]
    outbound_specs = [s for s in specs if s.name not in _LOCAL_PROBE_NAMES]

    # Phase 1 — DB-bound work only (csp self-check + SELECT 1 + registry).
    # Do NOT start outbound probes while the request session still has work.
    local_results: dict[str, object] = {}
    for spec in local_specs:
        try:
            local_results[spec.name] = await probe_base_service(spec.name, db=db)
        except Exception as exc:  # pragma: no cover - defensive net
            local_results[spec.name] = exc

    db_result = local_results.get("csp-db")
    db_ok = (
        not isinstance(db_result, BaseException)
        and getattr(db_result, "status", None) == HEALTH_HEALTHY
    )

    models = agents = None
    if db_ok:
        # DB 探測成功之後才讀註冊表;DB 掛著還去 query 只會把一個已知的紅點
        # 變成 500。回 null 而不是回 0 —— 0 是謊,null 是「這輪沒讀到」。
        try:
            models, agents = _registry_counts(db)
        except Exception:
            models = agents = None

    # Release the request transaction before outbound probes. With
    # expire_on_commit=False this returns the pooled connection without
    # silently re-checking it out on later attribute access.
    db.commit()

    # Phase 2 — HTTP / redis. Never pass the request session.
    outbound_raw = await asyncio.gather(
        *(probe_base_service(spec.name) for spec in outbound_specs),
        return_exceptions=True,
    )
    outbound_by_name = {
        spec.name: result for spec, result in zip(outbound_specs, outbound_raw)
    }

    checked_at = _utc_now_iso()
    services: list[ServiceHealthEntry] = []
    for spec in specs:
        if spec.name in _LOCAL_PROBE_NAMES:
            result = local_results[spec.name]
        else:
            result = outbound_by_name[spec.name]
        services.append(_entry_from_result(spec, result, checked_at))

    return HealthOverviewResponse(
        overall=aggregate_health(entry.status for entry in services),
        checked_at=checked_at,
        services=services,
        models=models,
        agents=agents,
    )
