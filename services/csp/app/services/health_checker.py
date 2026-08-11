"""Background task to periodically check model and agent endpoint health.

Slice 6a (doc 04 §9 / doc 01 §32 拍板):health 字彙收斂為五態
``unknown / healthy / degraded / unhealthy / disabled``。舊三值
(online/connecting/offline)由 r1_0005 遷移到位;本模組的 probe 直接回五態
(2xx on real path → healthy、401/403-only → unknown、timeout → degraded、
unreachable/unsafe → unhealthy),``unknown`` 亦含「碰到但無法確認會為我們服務」、
``disabled`` 由讀取端依 ``is_active`` 呈現。

P3.3 / attic W3-3⑦ 追加**基礎服務**探測(csp-db / redis / router /
anila-studio / ingestion-worker / pptx-renderer / nginx)。model/agent 那一套
五態語意一個字都沒動 —— 新東西只是**沿用**同一組字彙,見
``probe_base_service``。背景迴圈的 session-release 與告警不帶 raw endpoint
address 不變式維持原樣。
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
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from app.database import SessionLocal
from app.models.model_registry import ModelRegistry
from app.models.agent import Agent
from app.services.alert_service import resolve_alert_by_fingerprint, upsert_alert
from app.services.proxy.urls import join_upstream_path, strip_trailing_api_version

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


# Paths the platform actually uses for model/agent HTTP surfaces.
# Only a 2xx on these paths counts as ``healthy`` — that proves the
# surface will serve an unauthenticated probe. 401/403 only prove that
# something speaks HTTP; they do not prove our key still works
# (2026-07-31 false green: rotated key stayed green on 401).
REAL_PROBE_PATHS: tuple[str, ...] = ("/health", "/v1/models")
HEALTH_CHECK_INTERVAL_SECONDS = 60

# Host-liveness only. Kept so ops can tell "host answers something" from
# "completely dead", but a `/`-only hit must NOT be reported the same way
# as a real probe hit (2026-07-30 false green: `/` almost always answers).
WEAK_PROBE_PATHS: tuple[str, ...] = ("/",)


def _probe_url(endpoint_url: str, path: str) -> str:
    """Build a probe URL without stacking non-version paths under ``/v1``.

    ``join_upstream_path`` leaves a trailing ``/v1`` on the base when the
    path itself is not version-qualified, so ``…/v1`` + ``/health`` becomes
    ``…/v1/health``. Real agent/model health lives at ``/health``. Strip the
    trailing API version for non-version probe paths; versioned paths
    (``/v1/models``) keep the existing join semantics.
    """
    if path.startswith("/v1/") or path.startswith("/v2/") or path in ("/v1", "/v2"):
        return join_upstream_path(endpoint_url, path)
    return join_upstream_path(strip_trailing_api_version(endpoint_url), path)


def _real_probe_hit(status_code: int) -> bool:
    """True when a REAL probe path answered 2xx — usable without guessing auth."""
    return 200 <= status_code < 300


def _auth_rejected(status_code: int) -> bool:
    """Probe reached an API surface that refused us; not proof of health."""
    return status_code in (401, 403)


async def probe_model_health_detailed(
    endpoint_url: str,
    *,
    endpoint_kind: str | None = None,
    protocol: str | None = None,
    model_name: str | None = None,
    skip_validate: bool = False,
) -> tuple[str, int]:
    """Active probe → ``(five_state_status, latency_ms)`` (health probe only).

    - ``protocol=triton_grpc``: Triton ``ModelReady`` / ``ServerLive`` /
      ``grpc.health.v1`` (never httpx GETs against a gRPC port).
    - otherwise HTTP:
      - ``/health`` or ``/v1/models`` responding 2xx → ``healthy``
      - those paths answering 401/403 only → ``unknown`` (reachable, not proven)
      - only ``/`` responding ``<500`` → ``degraded`` (host up, API path unproven)
      - timeout → ``degraded``
      - unreachable / unsafe endpoint → ``unhealthy``

    Carries NO real user data (doc 04 §9). Call-time SSRF re-validation
    (TOCTOU/rebinding) runs once against the registered host — an unsafe
    endpoint is reported unhealthy, never probed. Callers that already
    validated may pass ``skip_validate=True`` to keep the once-per-host
    invariant.
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    # Guard once per host: validate_outbound_url only inspects scheme +
    # hostname (identical across the probe paths) and each call does
    # a blocking getaddrinfo — do not multiply that inside the model/agent loop.
    if not skip_validate:
        try:
            if endpoint_kind is None:
                validate_outbound_url(endpoint_url)
            else:
                validate_outbound_url(endpoint_url, endpoint_kind=endpoint_kind)
        except UnsafeEndpointError as exc:
            # Log reason code only — hostname lives on the exception and must
            # not become an unauthenticated disclosure face via log shipping.
            logger.warning(
                "health probe skipped — unsafe endpoint (reason=%s)",
                getattr(exc, "reason", type(exc).__name__),
            )
            return HEALTH_UNHEALTHY, _elapsed_ms()

    if (protocol or "").strip() == "triton_grpc":
        from app.services.triton_grpc import probe_triton_health

        return await asyncio.to_thread(
            probe_triton_health,
            endpoint_url,
            model_name=model_name,
        )

    real_urls = [_probe_url(endpoint_url, path) for path in REAL_PROBE_PATHS]
    weak_urls = [_probe_url(endpoint_url, path) for path in WEAK_PROBE_PATHS]

    saw_timeout = False
    saw_weak = False
    saw_auth_reject = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for url in real_urls:
                try:
                    resp = await client.get(url)
                    if _real_probe_hit(resp.status_code):
                        return HEALTH_HEALTHY, _elapsed_ms()
                    if _auth_rejected(resp.status_code):
                        saw_auth_reject = True
                except httpx.ConnectError:
                    continue
                except httpx.TimeoutException:
                    saw_timeout = True
                    break
            # Auth rejection on a real path beats a weak `/` hit: we reached
            # the API surface but cannot claim it will serve us.
            if saw_auth_reject and not saw_timeout:
                return HEALTH_UNKNOWN, _elapsed_ms()
            if not saw_timeout:
                for url in weak_urls:
                    try:
                        resp = await client.get(url)
                        if resp.status_code < 500:
                            saw_weak = True
                            break
                    except httpx.ConnectError:
                        continue
                    except httpx.TimeoutException:
                        saw_timeout = True
                        break
    except Exception as e:
        logger.debug("健康檢查異常: %s", e)

    if saw_weak:
        return HEALTH_DEGRADED, _elapsed_ms()
    return (HEALTH_DEGRADED if saw_timeout else HEALTH_UNHEALTHY), _elapsed_ms()


async def check_model_health(
    model_id: int,
    endpoint_url: str,
    *,
    endpoint_kind: str | None = None,
    protocol: str | None = None,
    model_name: str | None = None,
) -> str:
    """Check a single model/agent endpoint. Returns a five-state status
    ('healthy' / 'degraded' / 'unhealthy')."""
    status, _ = await probe_model_health_detailed(
        endpoint_url,
        endpoint_kind=endpoint_kind,
        protocol=protocol,
        model_name=model_name,
    )
    return status


async def _health_check_loop():
    """Periodically check all registered model endpoints."""
    while True:
        try:
            db = SessionLocal()
            try:
                targets = [
                    (
                        m.id,
                        m.endpoint_url,
                        m.name,
                        m.display_name,
                        m.health_status,
                        m.protocol or "openai_compatible",
                    )
                    for m in (
                        db.query(ModelRegistry)
                        .filter(ModelRegistry.is_active.is_(True))
                        .all()
                    )
                ]
                # Release the pooled connection before outbound probes (10s each).
                db.commit()
            finally:
                db.close()

            results = []
            for (
                model_id,
                endpoint_url,
                name,
                display_name,
                prev_status,
                protocol,
            ) in targets:
                status = await check_model_health(
                    model_id,
                    endpoint_url,
                    endpoint_kind="model",
                    protocol=protocol,
                    model_name=name,
                )
                results.append(
                    (model_id, endpoint_url, name, display_name, prev_status, status)
                )

            db = SessionLocal()
            try:
                for (
                    model_id,
                    endpoint_url,
                    name,
                    display_name,
                    prev_status,
                    status,
                ) in results:
                    model = db.get(ModelRegistry, model_id)
                    if model is None:
                        continue
                    if prev_status != status:
                        logger.info(
                            f"模型 {name} 狀態變更: {prev_status} -> {status}"
                        )
                    if status == HEALTH_UNHEALTHY:
                        # Message names the model, never the address;
                        # structured metadata still stores the URL for
                        # owner-only disclosure on the alert listing.
                        upsert_alert(
                            db,
                            fingerprint=f"health:model:{model_id}",
                            category="health",
                            severity="high",
                            title=f"模型 {display_name} 離線",
                            message=(
                                f"無法連線至模型「{display_name}」"
                                f"（{name}）"
                            ),
                            source_type="model",
                            source_id=model_id,
                            metadata={
                                "model_name": name,
                                "display_name": display_name,
                                "endpoint_url": endpoint_url,
                            },
                        )
                    elif status == HEALTH_HEALTHY:
                        resolve_alert_by_fingerprint(db, f"health:model:{model_id}")
                    model.health_status = status
                    model.health_checked_at = datetime.now(timezone.utc)

                db.commit()
            finally:
                db.close()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"健康檢查迴圈錯誤: {e}")

        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)


async def _agent_health_check_loop():
    """Periodically check all approved agent endpoints."""
    while True:
        try:
            db = SessionLocal()
            try:
                targets = [
                    (a.id, a.endpoint_url, a.name, a.health_status)
                    for a in (
                        db.query(Agent)
                        .filter(Agent.approval_status == "approved")
                        .all()
                    )
                ]
                # Release before outbound probes (10s each).
                db.commit()
            finally:
                db.close()

            results = []
            for agent_id, endpoint_url, name, prev_status in targets:
                status = await check_model_health(
                    agent_id, endpoint_url, endpoint_kind="agent"
                )
                results.append((agent_id, endpoint_url, name, prev_status, status))

            db = SessionLocal()
            try:
                for agent_id, endpoint_url, name, prev_status, status in results:
                    agent = db.get(Agent, agent_id)
                    if agent is None:
                        continue
                    if prev_status != status:
                        logger.info(
                            "Agent %s 狀態變更: %s -> %s",
                            name,
                            prev_status,
                            status,
                        )
                    fingerprint = f"health:agent:{agent_id}"
                    if status == HEALTH_UNHEALTHY:
                        # Message names the agent, never the address —
                        # same posture as the model path. Structured
                        # metadata still stores the URL for gated
                        # disclosure on the alert listing
                        # (``can_see_endpoint_address``).
                        upsert_alert(
                            db,
                            fingerprint=fingerprint,
                            category="health",
                            severity="high",
                            title=f"Agent {name} 離線",
                            message=f"無法連線至 Agent「{name}」",
                            source_type="agent",
                            source_id=agent_id,
                            metadata={
                                "agent_name": name,
                                "endpoint_url": endpoint_url,
                            },
                        )
                    elif status == HEALTH_HEALTHY:
                        resolve_alert_by_fingerprint(db, fingerprint)
                    agent.health_status = status
                db.commit()
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Agent 健康檢查迴圈錯誤: %s", exc)

        await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)


# ══════════════════════════════════════════════════════════════════════════
# P3.3 / attic W3-3⑦ 基礎服務探測
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
#: 沒有 HTTP 面的佇列工作者(arq)。liveness 來自它自己寫進 redis 的
#: health-check key,而不是去敲一個它從來沒開過的 port —— 見
#: ``_probe_queue_worker``。
SERVICE_KIND_QUEUE = "queue"


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
    BaseServiceSpec("ingestion-worker", "文件匯入工作者", SERVICE_KIND_QUEUE),
    BaseServiceSpec("anila-studio", "簡報產生服務", SERVICE_KIND_HTTP),
    BaseServiceSpec("pptx-renderer", "簡報渲染服務", SERVICE_KIND_HTTP),
)


@dataclass(frozen=True, slots=True)
class _HttpProbeTarget:
    """一個 HTTP 探測目標(**私有**:url 與 headers 都不進回應)。

    ``healthy_statuses`` 逐服務寫死,門檻是「**這個服務回答了關於它自己健康的
    問題**」,不是「有東西在答話」。舊版一律 ``<500`` 算綠,於是 router 一個
    **不存在路徑**的 404 被畫成綠燈 —— 總覽從來沒問過 router 它好不好
    (2026-08-05 量到)。
    """

    url: str
    healthy_statuses: frozenset[int]
    #: 探測要帶的 request header。目前只有 nginx 需要(見下面的 Host 說明)。
    headers: tuple[tuple[str, str], ...] = ()


#: **私有**探測目標表。與 ``BASE_SERVICE_SPECS`` 分開放,是為了讓「這些字串
#: 不進回應」成為結構上的事實而不是一句承諾 —— 序列化只讀 specs。
_HTTP_PROBE_TARGETS: dict[str, _HttpProbeTarget] = {
    # nginx :80 對 allowlist 內的 Host `return 301`,對清單外的 Host
    # `return 444`(直接關連線、不送回應)。那條 allowlist 是 open redirect
    # 的修補:沒有它的時候 `return 301 https://$host...` 會把攻擊者控制的
    # Host 原樣寫進 `Location`。所以探測**帶一個 allowlist 內的 Host** 去問,
    # 而不是去放寬 allowlist 遷就探測;`localhost` 在那張 map 內,而且不是
    # 任何真實部署位址。301 不 follow,也不會 proxy 回 csp,所以探測不會
    # 形成請求迴圈。
    "nginx": _HttpProbeTarget(
        "http://nginx:80/", frozenset({301}), (("Host", "localhost"),)
    ),
    # router 的健康在 `/health`;`/ready` 這條路徑 router 沒有,探它只會拿到
    # 404。這支探測**只採信 status code**,不讀 body —— 包含 `/health` 裡的
    # `last_refresh_error`。刻意不讀的理由:那個欄位只有在「下一次 refresh
    # 成功」時才被清回 None(`anila_core/registry/remote_agent_manifest.py:181`),
    # 而 refresh 是對話流量經 `ensure_fresh` 觸發的,不是定時器。平台一晚沒人用
    # 的話,一次暫時性失敗會讓這張卡黃到有人來聊天為止 —— 正是這包在關掉的
    # 那種喊狼。要改成讀它,得先給 router 一條與流量無關的 refresh 路徑。
    "router": _HttpProbeTarget("http://router:9000/health", frozenset({200})),
    "anila-studio": _HttpProbeTarget(
        "http://anila-studio:8100/health", frozenset({200})
    ),
    "pptx-renderer": _HttpProbeTarget(
        "http://pptx-renderer:7100/health", frozenset({200})
    ),
}

#: arq worker 每 ``health_check_interval`` 秒把一行狀態 ``psetex`` 進這個 redis
#: key,TTL = interval + 1s(``arq.worker.Worker.record_health``)。key 名 =
#: ``arq.constants.default_queue_name + health_check_key_suffix``,csp 端 arq
#: 0.26.1 與 worker 端 0.28.0 這兩個常數同值(2026-08-05 兩邊都印過)。
#: 所以「key 在」= worker 在上一個 interval 內還在回報;「key 不在」= 它不寫了。
#: 這是 arq worker 唯一自己產生的 liveness 訊號 —— 它沒有 HTTP 面可敲。
_ARQ_HEALTH_CHECK_KEY = "arq:queue:health-check"


#: 允許探測的名稱集合。closed set —— ``probe_base_service`` 只認這裡面的字。
_BASE_SERVICE_NAMES: frozenset[str] = frozenset(
    spec.name for spec in BASE_SERVICE_SPECS
)

#: 走佇列 liveness 而非 HTTP 的服務。**從 specs 推導**,不另抄一份名單 ——
#: 兩份名單遲早會分歧,而分歧的那一刻卡片就開始說謊。
_QUEUE_SERVICE_NAMES: frozenset[str] = frozenset(
    spec.name for spec in BASE_SERVICE_SPECS if spec.kind == SERVICE_KIND_QUEUE
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


async def _probe_http_service(
    name: str, target: _HttpProbeTarget
) -> ServiceProbeResult:
    """HTTP 基礎服務探測。回本模組五態 + bounded reason。

    綠燈的門檻是 ``target.healthy_statuses`` —— 只有那些 status 算「這個服務
    回答了關於它自己健康的問題」。其他會答話的 status(5xx、404、401…)一律
    ``degraded``:活著,但沒證明它好。timeout → degraded;連不上 → unhealthy。
    這組映射刻意與 ``probe_model_health_detailed`` 同姿態:那邊的 ``/`` 弱探
    也是只到 degraded,不到 healthy。
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    parts = urlsplit(target.url)
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
            resp = await client.get(target.url, headers=dict(target.headers))
        if resp.status_code in target.healthy_statuses:
            return ServiceProbeResult(HEALTH_HEALTHY, PROBE_OK, _elapsed_ms())
        # status code 只進 log,不進回應(bounded reason 不承載後端細節)。
        logger.debug("基礎服務 %s 回了非預期 status %s", name, resp.status_code)
        return ServiceProbeResult(HEALTH_DEGRADED, PROBE_UPSTREAM_ERROR, _elapsed_ms())
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


async def _aclose_redis(client) -> None:
    """關掉探測用的 redis 連線。redis-py 新舊兩代分別是 ``aclose`` / ``close``。

    關不掉不影響判定 —— 這裡吞例外是刻意的,不要讓收尾把已經得到的結果蓋掉。
    """
    closer = getattr(client, "aclose", None) or getattr(client, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # pragma: no cover - 關連線失敗不影響判定
        logger.debug("redis 探測連線關閉失敗", exc_info=True)


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
            await _aclose_redis(client)


async def _probe_queue_worker(name: str) -> ServiceProbeResult:
    """佇列工作者(arq)探測:讀它自己寫進 redis 的 health-check key。

    為什麼不敲 HTTP:``infra/compose/platform.yml`` 的 ingestion-worker 沒有
    ``ports``、沒有 HTTP server,它跑的是 ``arq ingestion_worker.main.WorkerSettings``。
    舊設定去打 ``:8081/ready`` 只會永遠 connection refused,那張卡永遠紅,而
    ``aggregate_health`` 取最差 → **整張總覽永遠紅**。永遠紅的儀表板等於沒有
    儀表板(見 ``_resolves`` 的註解)。

    兩個訊號一起看,而不是任一個單獨看
    ------------------------------------
    ``_resolves`` 只在**容器正在跑**的時候解得出 compose 服務名(2026-08-05
    量到:repo 裡有定義但本 project 沒起的 ``flux2-dev`` 直接 gaierror)。
    key 則活在 redis 裡,worker 不在了仍會撐到 TTL 到期。兩個湊起來才分得出
    四種狀態,單看任一個都會說謊:

    ==========  ======  ==========================================  ============
    名稱解析      key     真實情況                                      卡片
    ==========  ======  ==========================================  ============
    解得出       在      正在跑而且在回報                              healthy/ok
    解得出       不在    容器在,但它超過一個 TTL 沒回報(卡死)        unhealthy
    解不出       在      TTL 內還在回報過,現在名字沒了 → 容器停了      unhealthy
    解不出       不在    這個部署沒有這個服務(或早就不在了)           unknown/
                                                                    not_deployed
    ==========  ======  ==========================================  ============

    **只用「解不出 → 未部署」是不夠的**:那是 2026-08-05 驗收抓到的謊 ——
    容器一停,DNS 記錄就消失,於是「worker 掛了」會被畫成黃色的「此部署未啟用」,
    而那一格的說明是「不是故障」。加上 key 這一維,停掉的容器在 key 還活著的
    期間會**立刻**翻紅,不必等 TTL。

    這也是為什麼**不去縮短 worker 的 ``health_check_interval``**:key 活得久,
    「名字沒了但 key 還在」這個判斷才有足夠長的窗;而且 handler 是同步阻塞的
    (parse 一份 400 頁 PDF 就佔住事件迴圈幾十秒,大 collection 的
    ``reresolve_collection_relations`` 是整個迴圈連續解析),TTL 一短,
    **正在正常工作的 worker 就會被畫成死的**。細節見
    ``services/ingestion-worker/README.md`` 的〈治理首頁那盞燈〉。

    redis 自己壞掉時回 ``unknown`` 而不是 ``unhealthy``:那是「我們問不到」,
    不是「worker 死了」,把帳算到 worker 頭上會讓管理者去修錯的東西
    (redis 那張卡會自己紅,真正的原因在那裡)。
    """
    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    resolves = await asyncio.to_thread(_resolves, name, 0)

    try:
        import redis.asyncio as aioredis  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("redis 套件未安裝,無法探測 %s 的佇列 liveness", name)
        return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_UNSUPPORTED, _elapsed_ms())

    client = None
    try:
        client = aioredis.from_url(
            _redis_probe_url(),
            socket_connect_timeout=BASE_SERVICE_PROBE_TIMEOUT,
            socket_timeout=BASE_SERVICE_PROBE_TIMEOUT,
        )
        reporting = await asyncio.wait_for(
            client.exists(_ARQ_HEALTH_CHECK_KEY),
            timeout=BASE_SERVICE_PROBE_TIMEOUT,
        )
    except (asyncio.TimeoutError, TimeoutError):
        return ServiceProbeResult(HEALTH_DEGRADED, PROBE_TIMEOUT, _elapsed_ms())
    except Exception:
        logger.warning("%s 佇列 liveness 探測失敗", name, exc_info=True)
        return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_PROBE_FAILED, _elapsed_ms())
    finally:
        if client is not None:
            await _aclose_redis(client)

    if resolves and reporting:
        return ServiceProbeResult(HEALTH_HEALTHY, PROBE_OK, _elapsed_ms())
    if not resolves and not reporting:
        # 名字沒了、key 也沒了 —— 分不出「本來就沒部署」跟「早就不在了」。
        # 這種時候閉嘴比亂猜好(見 ``_resolves`` 的註解)。
        logger.debug("基礎服務 %s 名稱與 health key 都不在,視為未部署", name)
        return ServiceProbeResult(HEALTH_UNKNOWN, PROBE_NOT_DEPLOYED, _elapsed_ms())
    # 剩下兩格都是「它應該在,但不對勁」:容器在卻不回報,或回報過但名字沒了。
    return ServiceProbeResult(HEALTH_UNHEALTHY, PROBE_UNREACHABLE, _elapsed_ms())


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
    if name in _QUEUE_SERVICE_NAMES:
        return await _probe_queue_worker(name)
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
        try:
            await asyncio.gather(model_task, agent_task, return_exceptions=True)
        finally:
            for task in (model_task, agent_task):
                if not task.done():
                    task.cancel()

    logger.info("模型 + Agent 健康檢查背景任務已啟動")
    return asyncio.create_task(_run_all())
