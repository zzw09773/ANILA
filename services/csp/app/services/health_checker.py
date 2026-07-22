"""Background task to periodically check model and agent endpoint health.

Slice 6a (doc 04 §9 / doc 01 §32 拍板):health 字彙收斂為五態
``unknown / healthy / degraded / unhealthy / disabled``。舊三值
(online/connecting/offline)由 r1_0005 遷移到位;本模組的 probe 直接回五態
(reachable → healthy、timeout → degraded、unreachable/unsafe → unhealthy),
``unknown`` 為初始未檢查、``disabled`` 由讀取端依 ``is_active`` 呈現。

DB work runs in worker threads via ``asyncio.to_thread``. Each loop iteration
is phase-split so no SQLAlchemy session / transaction is held across an
``await`` HTTP probe (event-loop freeze when ``commit`` blocks on a row lock).
"""
import asyncio
import logging
import time
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class _ModelHealthSnapshot:
    id: int
    name: str
    display_name: str
    endpoint_url: str
    health_status: str | None
    admit_denied: bool


@dataclass(frozen=True, slots=True)
class _AgentHealthSnapshot:
    id: int
    name: str
    endpoint_url: str
    health_status: str | None


def _load_model_health_snapshots() -> list[_ModelHealthSnapshot]:
    """Thread-phase: read plain snapshots + admit checks; commit/close before return."""
    db = SessionLocal()
    try:
        models = (
            db.query(ModelRegistry)
            .filter(ModelRegistry.is_active.is_(True))
            .all()
        )
        snapshots: list[_ModelHealthSnapshot] = []
        for model in models:
            admit_denied = False
            try:
                admit_registry_provider(model)
            except Exception as exc:
                admit_denied = True
                logger.error(
                    "模型 %s provider authority 驗證失敗，略過健康探測: %s",
                    model.name,
                    exc,
                )
            snapshots.append(
                _ModelHealthSnapshot(
                    id=model.id,
                    name=model.name,
                    display_name=model.display_name,
                    endpoint_url=model.endpoint_url,
                    health_status=model.health_status,
                    admit_denied=admit_denied,
                )
            )
        db.commit()
        return snapshots
    finally:
        db.close()


def _apply_model_health_results(
    results: list[tuple[_ModelHealthSnapshot, str]],
) -> None:
    """Thread-phase: fresh session, re-fetch by id, apply status/alerts, commit/close."""
    db = SessionLocal()
    try:
        for snap, status in results:
            model = (
                db.query(ModelRegistry)
                .filter(ModelRegistry.id == snap.id)
                .first()
            )
            if model is None:
                continue
            if not model.is_active or model.endpoint_url != snap.endpoint_url:
                # 快照與寫回之間端點已變更或模型已停用：這筆探測結果
                # 描述的是舊狀態，寫回會污染新設定，跳過讓下一輪重測。
                continue
            if snap.admit_denied:
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
            if snap.health_status != status:
                logger.info(
                    f"模型 {snap.name} 狀態變更: {snap.health_status} -> {status}"
                )
            if status == HEALTH_UNHEALTHY:
                upsert_alert(
                    db,
                    fingerprint=f"health:model:{model.id}",
                    category="health",
                    severity="high",
                    title=f"模型 {model.display_name} 離線",
                    message=f"無法連線至 {snap.endpoint_url}",
                    source_type="model",
                    source_id=model.id,
                    metadata={
                        "model_name": model.name,
                        "display_name": model.display_name,
                        "endpoint_url": snap.endpoint_url,
                    },
                )
            elif status == HEALTH_HEALTHY:
                resolve_alert_by_fingerprint(db, f"health:model:{model.id}")
            model.health_status = status
            model.health_checked_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _load_agent_health_snapshots() -> list[_AgentHealthSnapshot]:
    """Thread-phase: read plain agent snapshots; commit/close before return."""
    db = SessionLocal()
    try:
        agents = (
            db.query(Agent)
            .filter(Agent.approval_status == "approved")
            .all()
        )
        snapshots = [
            _AgentHealthSnapshot(
                id=agent.id,
                name=agent.name,
                endpoint_url=agent.endpoint_url,
                health_status=agent.health_status,
            )
            for agent in agents
        ]
        db.commit()
        return snapshots
    finally:
        db.close()


def _apply_agent_health_results(
    results: list[tuple[_AgentHealthSnapshot, str]],
) -> None:
    """Thread-phase: fresh session, re-fetch by id, apply status/alerts, commit/close."""
    db = SessionLocal()
    try:
        for snap, status in results:
            agent = db.query(Agent).filter(Agent.id == snap.id).first()
            if agent is None:
                continue
            if (
                agent.approval_status != "approved"
                or agent.endpoint_url != snap.endpoint_url
            ):
                # 快照與寫回之間核准被撤或端點已變更：結果已過時，跳過。
                continue
            if snap.health_status != status:
                logger.info(
                    "Agent %s 狀態變更: %s -> %s",
                    snap.name,
                    snap.health_status,
                    status,
                )
            fingerprint = f"health:agent:{agent.id}"
            if status == HEALTH_UNHEALTHY:
                upsert_alert(
                    db,
                    fingerprint=fingerprint,
                    category="health",
                    severity="high",
                    title=f"Agent {agent.name} 離線",
                    message=f"無法連線至 {snap.endpoint_url}",
                    source_type="agent",
                    source_id=agent.id,
                    metadata={
                        "agent_name": agent.name,
                        "endpoint_url": snap.endpoint_url,
                    },
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


def _run_task_reconciliation() -> int:
    """Thread-phase: sync-DB-only reconciler (no awaits inside callee)."""
    from app.services.proxy.task_link import reconcile_stale_task_runs

    db = SessionLocal()
    try:
        return reconcile_stale_task_runs(
            db, stale_after_seconds=settings.TASK_RUN_STALE_SECONDS
        )
    finally:
        db.close()


async def _health_check_loop():
    """Periodically check all registered model endpoints."""
    while True:
        try:
            snapshots = await asyncio.to_thread(_load_model_health_snapshots)
            results: list[tuple[_ModelHealthSnapshot, str]] = []
            for snap in snapshots:
                if snap.admit_denied:
                    results.append((snap, HEALTH_UNHEALTHY))
                    continue
                status = await check_model_health(snap.id, snap.endpoint_url)
                results.append((snap, status))
            if results:
                await asyncio.to_thread(_apply_model_health_results, results)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"健康檢查迴圈錯誤: {e}")

        await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)


async def _agent_health_check_loop():
    """Periodically check all approved agent endpoints."""
    while True:
        try:
            snapshots = await asyncio.to_thread(_load_agent_health_snapshots)
            results: list[tuple[_AgentHealthSnapshot, str]] = []
            for snap in snapshots:
                status = await check_agent_health(snap.id, snap.endpoint_url)
                results.append((snap, status))
            if results:
                await asyncio.to_thread(_apply_agent_health_results, results)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Agent 健康檢查迴圈錯誤: %s", exc)

        await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)


async def _task_reconciliation_loop():
    """Converge TaskRuns abandoned by a crashed proxy process."""
    while True:
        try:
            closed = await asyncio.to_thread(_run_task_reconciliation)
            if closed:
                logger.error("治理收斂器關閉 %s 筆 stale TaskRun", closed)
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
