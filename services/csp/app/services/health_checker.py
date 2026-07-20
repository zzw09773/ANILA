"""Background task to periodically check model and agent endpoint health.

Slice 6a (doc 04 §9 / doc 01 §32 拍板):health 字彙收斂為五態
``unknown / healthy / degraded / unhealthy / disabled``。舊三值
(online/connecting/offline)由 r1_0005 遷移到位;本模組的 probe 直接回五態
(reachable → healthy、timeout → degraded、unreachable/unsafe → unhealthy),
``unknown`` 為初始未檢查、``disabled`` 由讀取端依 ``is_active`` 呈現。
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
import httpx
from anila_security import (
    ENDPOINT_KIND_AGENT,
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)
from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.agent import Agent
from app.config import settings
from app.services.alert_service import resolve_alert_by_fingerprint, upsert_alert
from app.services.model_governance_receipts import admit_registry_provider

logger = logging.getLogger(__name__)

# doc 04 §9 五態字彙。
HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNHEALTHY = "unhealthy"
HEALTH_DISABLED = "disabled"

FIVE_STATE_HEALTH = frozenset(
    {HEALTH_UNKNOWN, HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_UNHEALTHY, HEALTH_DISABLED}
)

# 舊三值 → 五態(doc 01 §32 拍板映射);讀取端把 DB 殘留舊值正規化。
_LEGACY_HEALTH_MAP = {
    "online": HEALTH_HEALTHY,
    "connecting": HEALTH_DEGRADED,
    "offline": HEALTH_UNHEALTHY,
    "": HEALTH_UNKNOWN,
}


def normalize_health_status(raw: str | None, *, is_active: bool = True) -> str:
    """Map any stored health value to the five-state vocabulary.

    - ``is_active=False`` → ``disabled``(承接停用/未核准態,doc 04 §9)。
    - 舊三值 online/connecting/offline → healthy/degraded/unhealthy。
    - 已是五態 → 原樣。
    - None / 未知字串 → ``unknown``(fail-safe)。
    """
    if not is_active:
        return HEALTH_DISABLED
    if raw in FIVE_STATE_HEALTH:
        return raw
    return _LEGACY_HEALTH_MAP.get(raw or "", HEALTH_UNKNOWN)


async def probe_model_health_detailed(
    endpoint_url: str, *, endpoint_kind: str = ENDPOINT_KIND_MODEL
) -> tuple[str, int]:
    """Active probe → ``(five_state_status, latency_ms)`` (health probe only).

    reachable(<500 on any of /health, /v1/models, /)→ healthy;timeout →
    degraded;unreachable / unsafe endpoint → unhealthy. Carries NO real user
    data (doc 04 §9). Call-time SSRF re-validation (TOCTOU/rebinding) runs
    first — an unsafe endpoint is reported unhealthy, never probed.
    """
    base_url = endpoint_url.rstrip("/")
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        validate_outbound_url(base_url, endpoint_kind=endpoint_kind)
    except UnsafeEndpointError as exc:
        logger.warning("health probe skipped — unsafe endpoint (%s)", exc)
        return HEALTH_UNHEALTHY, _elapsed_ms()

    saw_timeout = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for path in ["/health", "/v1/models", "/"]:
                try:
                    resp = await client.get(f"{base_url}{path}")
                    if resp.status_code < 500:
                        return HEALTH_HEALTHY, _elapsed_ms()
                except httpx.ConnectError:
                    continue
                except httpx.TimeoutException:
                    saw_timeout = True
                    break
    except Exception as e:
        logger.debug("健康檢查異常 (%s): %s", base_url, e)

    return (HEALTH_DEGRADED if saw_timeout else HEALTH_UNHEALTHY), _elapsed_ms()


async def check_model_health(model_id: int, endpoint_url: str) -> str:
    """Check a single model endpoint. Returns a five-state status
    ('healthy' / 'degraded' / 'unhealthy')."""
    status, _ = await probe_model_health_detailed(
        endpoint_url, endpoint_kind=ENDPOINT_KIND_MODEL
    )
    return status


async def check_agent_health(agent_id: int, endpoint_url: str) -> str:
    """Probe an Agent endpoint with the Agent SSRF/trusted-host policy."""

    status, _ = await probe_model_health_detailed(
        endpoint_url, endpoint_kind=ENDPOINT_KIND_AGENT
    )
    # ``probe_model_health_detailed`` is intentionally side-effect free; the
    # caller stamps the row.  Keep this wrapper for a stable focused test seam
    # and future per-agent probe paths.
    return status


async def _health_check_loop():
    """Periodically check all registered model endpoints."""
    while True:
        try:
            db = SessionLocal()
            try:
                models = (
                    db.query(ModelRegistry)
                    .filter(ModelRegistry.is_active.is_(True))
                    .all()
                )

                for model in models:
                    try:
                        admit_registry_provider(model)
                    except Exception as exc:
                        status = HEALTH_UNHEALTHY
                        logger.error(
                            "模型 %s provider authority 驗證失敗，略過健康探測: %s",
                            model.name,
                            exc,
                        )
                        upsert_alert(
                            db,
                            fingerprint=f"health:model:{model.id}",
                            category="health",
                            severity="high",
                            title=f"模型 {model.display_name} 治理拒絕",
                            message="provider authority 驗證失敗，未發出健康探測",
                            source_type="model",
                            source_id=model.id,
                            metadata={
                                "model_name": model.name,
                                "provider_authority": "denied",
                            },
                        )
                        model.health_status = status
                        model.health_checked_at = datetime.now(timezone.utc)
                        continue
                    status = await check_model_health(model.id, model.endpoint_url)
                    if model.health_status != status:
                        logger.info(
                            f"模型 {model.name} 狀態變更: {model.health_status} -> {status}"
                        )
                    if status == HEALTH_UNHEALTHY:
                        upsert_alert(
                            db,
                            fingerprint=f"health:model:{model.id}",
                            category="health",
                            severity="high",
                            title=f"模型 {model.display_name} 離線",
                            message=f"無法連線至 {model.endpoint_url}",
                            source_type="model",
                            source_id=model.id,
                            metadata={
                                "model_name": model.name,
                                "display_name": model.display_name,
                                "endpoint_url": model.endpoint_url,
                            },
                        )
                    elif status == HEALTH_HEALTHY:
                        resolve_alert_by_fingerprint(db, f"health:model:{model.id}")
                    model.health_status = status
                    model.health_checked_at = datetime.now(timezone.utc)

                db.commit()
            finally:
                db.close()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"健康檢查迴圈錯誤: {e}")

        await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)


async def _agent_health_check_loop():
    """Periodically check all approved agent endpoints."""
    while True:
        try:
            db = SessionLocal()
            try:
                agents = (
                    db.query(Agent)
                    .filter(Agent.approval_status == "approved")
                    .all()
                )
                for agent in agents:
                    status = await check_agent_health(agent.id, agent.endpoint_url)
                    if agent.health_status != status:
                        logger.info(
                            "Agent %s 狀態變更: %s -> %s", agent.name,
                            agent.health_status, status,
                        )
                    fingerprint = f"health:agent:{agent.id}"
                    if status == HEALTH_UNHEALTHY:
                        upsert_alert(
                            db,
                            fingerprint=fingerprint,
                            category="health",
                            severity="high",
                            title=f"Agent {agent.name} 離線",
                            message=f"無法連線至 {agent.endpoint_url}",
                            source_type="agent",
                            source_id=agent.id,
                            metadata={"agent_name": agent.name,
                                      "endpoint_url": agent.endpoint_url},
                        )
                    elif status == HEALTH_HEALTHY:
                        resolve_alert_by_fingerprint(db, fingerprint)
                    agent.health_status = status
                    # A timestamp is written for both success and failure.
                    # Freshness and health are separate predicates; an old
                    # ``healthy`` value must never survive a failed probe.
                    agent.health_checked_at = datetime.now(timezone.utc)
                db.commit()
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Agent 健康檢查迴圈錯誤: %s", exc)

        await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)


async def _task_reconciliation_loop():
    """Converge TaskRuns abandoned by a crashed proxy process."""
    from app.services.proxy.task_link import reconcile_stale_task_runs

    while True:
        try:
            db = SessionLocal()
            try:
                closed = reconcile_stale_task_runs(
                    db, stale_after_seconds=settings.TASK_RUN_STALE_SECONDS
                )
                if closed:
                    logger.error("治理收斂器關閉 %s 筆 stale TaskRun", closed)
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("TaskRun crash reconciliation 失敗")
        await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)


async def start_health_checker() -> asyncio.Task:
    """Start background health checker tasks for models and agents."""
    async def _run_all() -> None:
        model_task = asyncio.create_task(_health_check_loop())
        agent_task = asyncio.create_task(_agent_health_check_loop())
        reconcile_task = asyncio.create_task(_task_reconciliation_loop())
        try:
            await asyncio.gather(
                model_task, agent_task, reconcile_task, return_exceptions=True
            )
        finally:
            for task in (model_task, agent_task, reconcile_task):
                if not task.done():
                    task.cancel()

    logger.info("模型 + Agent 健康檢查背景任務已啟動")
    return asyncio.create_task(_run_all())
