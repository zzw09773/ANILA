"""Background task to periodically check model and agent endpoint health."""
import asyncio
import logging
from datetime import datetime, timezone
import httpx
from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.agent import Agent
from app.config import settings
from app.services.alert_service import resolve_alert_by_fingerprint, upsert_alert

logger = logging.getLogger(__name__)


async def check_model_health(model_id: int, endpoint_url: str) -> str:
    """Check a single model endpoint. Returns 'online', 'connecting', or 'offline'."""
    base_url = endpoint_url.rstrip("/")
    health_paths = ["/health", "/v1/models", "/"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for path in health_paths:
                try:
                    resp = await client.get(f"{base_url}{path}")
                    if resp.status_code < 500:
                        return "online"
                except httpx.ConnectError:
                    continue
                except httpx.TimeoutException:
                    return "connecting"
    except Exception as e:
        logger.debug(f"健康檢查異常 (model_id={model_id}): {e}")

    return "offline"


async def _health_check_loop():
    """Periodically check all registered model endpoints."""
    while True:
        try:
            db = SessionLocal()
            try:
                models = (
                    db.query(ModelRegistry)
                    .filter(ModelRegistry.is_active == True)
                    .all()
                )

                for model in models:
                    status = await check_model_health(model.id, model.endpoint_url)
                    if model.health_status != status:
                        logger.info(
                            f"模型 {model.name} 狀態變更: {model.health_status} -> {status}"
                        )
                    if status == "offline":
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
                    elif status == "online":
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
                    status = await check_model_health(agent.id, agent.endpoint_url)
                    if agent.health_status != status:
                        logger.info(
                            "Agent %s 狀態變更: %s -> %s", agent.name,
                            agent.health_status, status,
                        )
                    fingerprint = f"health:agent:{agent.id}"
                    if status == "offline":
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
                    elif status == "online":
                        resolve_alert_by_fingerprint(db, fingerprint)
                    agent.health_status = status
                db.commit()
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Agent 健康檢查迴圈錯誤: %s", exc)

        await asyncio.sleep(settings.HEALTH_CHECK_INTERVAL)


async def start_health_checker() -> asyncio.Task:
    """Start background health checker tasks for models and agents."""
    async def _run_all() -> None:
        model_task = asyncio.create_task(_health_check_loop())
        agent_task = asyncio.create_task(_agent_health_check_loop())
        try:
            await asyncio.gather(model_task, agent_task, return_exceptions=True)
        finally:
            for task in (model_task, agent_task):
                if not task.done():
                    task.cancel()

    logger.info("模型 + Agent 健康檢查背景任務已啟動")
    return asyncio.create_task(_run_all())
