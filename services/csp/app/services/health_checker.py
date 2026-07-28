"""Background task to periodically check model and agent endpoint health.

Slice 6a (doc 04 §9 / doc 01 §32 拍板):health 字彙收斂為五態
``unknown / healthy / degraded / unhealthy / disabled``。舊三值
(online/connecting/offline)由 r1_0005 遷移到位;本模組的 probe 直接回五態
(reachable → healthy、timeout → degraded、unreachable/unsafe → unhealthy),
``unknown`` 為初始未檢查、``disabled`` 由讀取端依 ``is_active`` 呈現。

DB work runs in worker threads via ``asyncio.to_thread``. Each loop iteration
is phase-split so no SQLAlchemy session / transaction is held across an
``await`` HTTP probe (event-loop freeze when ``commit`` blocks on a row lock).

W3-3⑦ 追加**基礎服務**探測(csp-db / redis / router / anila-studio /
ingestion-worker / pptx-renderer / nginx)。model/agent 那一套五態語意
一個字都沒動 —— 新東西只是**沿用**同一組字彙,見 ``probe_base_service``。
"""
import asyncio
import logging
import os
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
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


def is_explicitly_unhealthy(health_status: str | None) -> bool:
    """True only when health_checker has explicitly marked probe-failure unhealthy.

    Conservative circuit-breaker predicate: ``unknown``, never-probed, skipped
    probes, ``degraded``, and ``healthy`` all return False so proxy keeps
    forwarding. Legacy ``offline`` normalizes to unhealthy and returns True.
    """
    return normalize_health_status(health_status, is_active=True) == HEALTH_UNHEALTHY


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


# ══════════════════════════════════════════════════════════════════════════
# W3-3⑦ 基礎服務探測
# ══════════════════════════════════════════════════════════════════════════
#
# 為什麼需要
# ----------
# admin-journey D1:22 個治理視圖沒有**任何一個**顯示 router / anila-studio /
# ingestion-worker / csp-db / redis / nginx / pptx-renderer 的狀態。管理者目前
# 唯一的真實工具是 SSH 進 `.15` 跑 `anila-ops.sh health`。這一段補上那個
# 資料來源,讓治理首頁能回答「平台現在活著嗎」。
#
# 為什麼探測目標寫死
# ------------------
# 「健康探測」天生是「幫我連過去看看」的原語。若目標可由呼叫端指定,它就變成
# 一支掛在 admin 身分後面、看起來很正當的**內網掃描代理**。所以:
#
#   1. 目標清單 = compose 服務名,**hardcode 在本模組**,端點不吃 host/port。
#   2. 目標本身(host / port / path / 連線字串)**絕不出現在回應裡** ——
#      對外只有服務名、五態、bounded reason、latency。
#   3. ``trust_env=False``:HTTP_PROXY 之類的環境變數不得改寫探測去向。
#
# 為什麼不走 ``validate_outbound_url``
# -----------------------------------
# 那支 guard 的職責是擋「使用者註冊的外部端點」(single-label docker 服務名
# 預設就被它擋掉)。這裡的目標不是使用者輸入,是部署自身的服務圖;套上去會
# 一律拒絕,得到一張全紅但毫無資訊的卡。安全性由「目標寫死」提供,而不是由
# 對常數再驗一次 SSRF 提供。

# bounded reason 字彙。**刻意不放後端例外訊息**:psycopg / httpx 的錯誤字串
# 會帶 host、port、user、甚至整條連線字串,那正是驗收③的紅線。細節只進 log。
PROBE_OK = "ok"
PROBE_TIMEOUT = "timeout"
PROBE_UNREACHABLE = "unreachable"
PROBE_UPSTREAM_ERROR = "upstream_error"
PROBE_PROBE_FAILED = "probe_failed"
PROBE_NOT_DEPLOYED = "not_deployed"
PROBE_UNSUPPORTED = "unsupported"

PROBE_REASONS = frozenset(
    {
        PROBE_OK,
        PROBE_TIMEOUT,
        PROBE_UNREACHABLE,
        PROBE_UPSTREAM_ERROR,
        PROBE_PROBE_FAILED,
        PROBE_NOT_DEPLOYED,
        PROBE_UNSUPPORTED,
    }
)

#: 基礎服務探測的逾時(秒)。單一服務慢不該把整張卡拖垮。
BASE_SERVICE_PROBE_TIMEOUT = 4.0

SERVICE_KIND_SELF = "self"
SERVICE_KIND_DATABASE = "database"
SERVICE_KIND_CACHE = "cache"
SERVICE_KIND_HTTP = "http"


@dataclass(frozen=True, slots=True)
class BaseServiceSpec:
    """一個基礎服務的**對外**描述。刻意不含 host / port / path。"""

    name: str
    label: str
    kind: str


@dataclass(frozen=True, slots=True)
class ServiceProbeResult:
    """探測結果。``status`` 用的是本模組既有的五態字彙。"""

    status: str
    reason: str
    latency_ms: int


#: 對外的服務清單與顯示順序。名稱 = ``infra/compose/platform.yml`` 的服務名。
BASE_SERVICE_SPECS: tuple[BaseServiceSpec, ...] = (
    BaseServiceSpec("csp", "控制平面", SERVICE_KIND_SELF),
    BaseServiceSpec("csp-db", "資料庫", SERVICE_KIND_DATABASE),
    BaseServiceSpec("redis", "佇列與快取", SERVICE_KIND_CACHE),
    BaseServiceSpec("nginx", "反向代理", SERVICE_KIND_HTTP),
    BaseServiceSpec("router", "對話路由", SERVICE_KIND_HTTP),
    BaseServiceSpec("ingestion-worker", "文件匯入工作者", SERVICE_KIND_HTTP),
    BaseServiceSpec("anila-studio", "簡報產生服務", SERVICE_KIND_HTTP),
    BaseServiceSpec("pptx-renderer", "簡報渲染服務", SERVICE_KIND_HTTP),
)

#: **私有**探測目標表。與 ``BASE_SERVICE_SPECS`` 分開放,是為了讓「這些字串
#: 不進回應」成為結構上的事實而不是一句承諾 —— 序列化只讀 specs。
_HTTP_PROBE_TARGETS: dict[str, str] = {
    # nginx :80 對所有路徑 `return 301`(見 infra/nginx/anila.conf:89-91),
    # 不會 proxy 回 csp,所以探測不會形成請求迴圈。
    "nginx": "http://nginx:80/",
    "router": "http://router:9000/ready",
    "ingestion-worker": "http://ingestion-worker:8081/ready",
    "anila-studio": "http://anila-studio:8100/health",
    "pptx-renderer": "http://pptx-renderer:7100/health",
}


#: 允許探測的名稱集合。closed set —— ``probe_base_service`` 只認這裡面的字。
_BASE_SERVICE_NAMES: frozenset[str] = frozenset(
    spec.name for spec in BASE_SERVICE_SPECS
)


def base_service_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in BASE_SERVICE_SPECS)


def _resolves(host: str, port: int) -> bool:
    """DNS 預檢:名稱解不出來 = 這個部署沒有這個服務。

    為什麼要分這一刀:三條部署分支 + compose profiles 之下,某些服務(例如
    只在 developer-tools profile 起的東西、或本機精簡 dev stack)本來就不存在。
    把「沒部署」跟「部署了但掛了」混成同一個紅點,卡片會天天喊狼,管理者
    三天後就不看了 —— 那等於這個功能沒做。
    """
    try:
        socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        return True
    except socket.gaierror:
        return False
    except OSError:
        # 解析層以外的問題(例如 sandbox 無網路):交給後續探測判定。
        return True


async def _probe_http_service(name: str, url: str) -> ServiceProbeResult:
    """HTTP 基礎服務探測。回本模組五態 + bounded reason。

    <500 → healthy(301 / 401 / 404 都算「服務在答話」);5xx → degraded
    (活著但壞);timeout → degraded;連不上 → unhealthy。這組映射刻意與
    ``probe_model_health_detailed`` 同姿態。
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    parts = urlsplit(url)
    host = parts.hostname or name
    port = parts.port or 80
    if not await asyncio.to_thread(_resolves, host, port):
        logger.debug("基礎服務 %s 名稱無法解析,視為未部署", name)
        return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_NOT_DEPLOYED, _elapsed_ms())

    try:
        async with httpx.AsyncClient(
            timeout=BASE_SERVICE_PROBE_TIMEOUT,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            resp = await client.get(url)
        if resp.status_code >= 500:
            return ServiceProbeResult(
                HEALTH_DEGRADED, PROBE_UPSTREAM_ERROR, _elapsed_ms()
            )
        return ServiceProbeResult(HEALTH_HEALTHY, PROBE_OK, _elapsed_ms())
    except httpx.TimeoutException:
        return ServiceProbeResult(HEALTH_DEGRADED, PROBE_TIMEOUT, _elapsed_ms())
    except (httpx.ConnectError, httpx.NetworkError):
        return ServiceProbeResult(HEALTH_UNHEALTHY, PROBE_UNREACHABLE, _elapsed_ms())
    except Exception:
        # 訊息只進 log,不進回應。
        logger.warning("基礎服務 %s 探測異常", name, exc_info=True)
        return ServiceProbeResult(HEALTH_UNHEALTHY, PROBE_PROBE_FAILED, _elapsed_ms())


def _redis_probe_url() -> str:
    """與 ``token_revocation_publisher`` 同一個來源,避免兩份真相。"""
    return os.environ.get("REDIS_URL", "redis://redis:6379/0")


async def _probe_redis() -> ServiceProbeResult:
    """真的送 ``PING``,而不是只 TCP connect。

    TCP 開得起來只證明有東西在聽 port;redis 在 ``noeviction`` + AOF 壞掉時
    仍然接受連線但拒絕寫入。PING 至少證明它在跑 redis 協定。
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    url = _redis_probe_url()
    parts = urlsplit(url)
    if not await asyncio.to_thread(
        _resolves, parts.hostname or "redis", parts.port or 6379
    ):
        return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_NOT_DEPLOYED, _elapsed_ms())

    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("redis 套件未安裝,無法探測 redis")
        return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_UNSUPPORTED, _elapsed_ms())

    client = None
    try:
        client = aioredis.from_url(
            url,
            socket_connect_timeout=BASE_SERVICE_PROBE_TIMEOUT,
            socket_timeout=BASE_SERVICE_PROBE_TIMEOUT,
        )
        pong = await asyncio.wait_for(
            client.ping(), timeout=BASE_SERVICE_PROBE_TIMEOUT
        )
        if pong:
            return ServiceProbeResult(HEALTH_HEALTHY, PROBE_OK, _elapsed_ms())
        return ServiceProbeResult(
            HEALTH_DEGRADED, PROBE_UPSTREAM_ERROR, _elapsed_ms()
        )
    except (asyncio.TimeoutError, TimeoutError):
        return ServiceProbeResult(HEALTH_DEGRADED, PROBE_TIMEOUT, _elapsed_ms())
    except Exception:
        logger.warning("redis 探測失敗", exc_info=True)
        return ServiceProbeResult(HEALTH_UNHEALTHY, PROBE_UNREACHABLE, _elapsed_ms())
    finally:
        if client is not None:
            closer = getattr(client, "aclose", None) or getattr(client, "close", None)
            if closer is not None:
                try:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:  # pragma: no cover - 關連線失敗不影響判定
                    logger.debug("redis 探測連線關閉失敗", exc_info=True)


def probe_database(db) -> ServiceProbeResult:
    """用**呼叫端當次請求的 session** 打 ``SELECT 1``。

    刻意不開新 ``SessionLocal``:要驗的就是 runtime 真正在用的那條路
    (``csp_app`` role、同一個 pool、同一組 statement timeout)。另外它是同步的,
    所以不會有「session 跨 await」的問題 —— 那是本模組開頭那段註解在講的坑。
    """
    from sqlalchemy import text

    started = time.monotonic()
    try:
        db.execute(text("SELECT 1")).scalar()
        return ServiceProbeResult(
            HEALTH_HEALTHY, PROBE_OK, int((time.monotonic() - started) * 1000)
        )
    except Exception:
        logger.warning("csp-db 探測失敗", exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - 連線已死時 rollback 也會炸
            logger.debug("csp-db 探測後 rollback 失敗", exc_info=True)
        return ServiceProbeResult(
            HEALTH_UNHEALTHY,
            PROBE_UNREACHABLE,
            int((time.monotonic() - started) * 1000),
        )


async def probe_base_service(name: str, *, db=None) -> ServiceProbeResult:
    """單一基礎服務探測。``name`` 必須在 ``BASE_SERVICE_SPECS`` 內。

    未知名稱直接 ``KeyError`` —— 這支函式不是通用連線工具,不接受清單外的目標。
    """
    if name not in _BASE_SERVICE_NAMES:
        raise KeyError(name)
    if name == "csp":
        # 我們正在回答這個請求,所以控制平面顯然活著。
        return ServiceProbeResult(HEALTH_HEALTHY, PROBE_OK, 0)
    if name == "csp-db":
        if db is None:
            return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_UNSUPPORTED, 0)
        return await asyncio.to_thread(probe_database, db)
    if name == "redis":
        return await _probe_redis()
    return await _probe_http_service(name, _HTTP_PROBE_TARGETS[name])


#: overall 取「最差」用的權重。``disabled`` 不拉低總覽(那是刻意停用)。
_OVERALL_PRIORITY = {
    HEALTH_HEALTHY: 0,
    HEALTH_DISABLED: 0,
    HEALTH_UNKNOWN: 1,
    HEALTH_DEGRADED: 2,
    HEALTH_UNHEALTHY: 3,
}


def aggregate_health(statuses: Iterable[str]) -> str:
    """總覽 = 最差的那一個。空清單 → ``unknown``(不是 healthy)。"""
    worst = HEALTH_UNKNOWN
    worst_rank = -1
    for status in statuses:
        rank = _OVERALL_PRIORITY.get(status, 1)
        if rank > worst_rank:
            worst_rank, worst = rank, status
    return worst if worst_rank >= 0 else HEALTH_UNKNOWN


def summarize_five_state(rows: Iterable[tuple[str | None, bool]]) -> dict[str, int]:
    """把 ``(health_status, is_active)`` 壓成五態計數 + total。

    正規化走既有的 ``normalize_health_status``,所以 DB 裡殘留的舊三值
    (online/connecting/offline)在這裡看到的與治理清單頁看到的是同一套字。
    """
    counts = {state: 0 for state in sorted(FIVE_STATE_HEALTH)}
    total = 0
    for raw, is_active in rows:
        total += 1
        counts[normalize_health_status(raw, is_active=bool(is_active))] += 1
    return {"total": total, **counts}


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
