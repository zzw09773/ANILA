import logging
import sys
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from pathlib import Path
from anila_core.api.routing import routed_path
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from app.config import settings
from app.database import engine, Base
from app.api.router import api_router
from app.api.conversations import router as conversations_router
from app.api.attachments import router as attachments_router
from app.api.handoffs import router as handoffs_router
from app.api.directory import router as directory_router
from app.api.platform_settings import router as platform_settings_router
from app.middleware.csrf import CsrfMiddleware
from app.models.user import User
from app.services.auth_service import require_admin


def _run_alembic_upgrade() -> None:
    """Run `alembic upgrade head` programmatically at startup."""
    from alembic.config import Config
    from alembic import command

    migrations_dir = Path(__file__).parent.parent / "migrations"
    alembic_ini = Path(__file__).parent.parent / "alembic.ini"

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(migrations_dir))
    command.upgrade(cfg, "head")


def setup_logging():
    # Round 5 補:setup_logging 必須 idempotent。alembic.ini 含
    # [loggers] section,_run_alembic_upgrade() 內部會觸發
    # logging.fileConfig() 把既有 handler 全部 disable 並把 root
    # level 降到 WARN。lifespan 在 alembic 之後會 re-call
    # setup_logging 補回 handlers;若不先清舊 handlers,重 setup
    # 時會殘留 disabled / duplicate handler,log 行會印兩次或全失。
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    # Round 5 補(再強化):fileConfig 不只清 root handlers,還會把
    # 跑 fileConfig 那一刻 Logger.manager.loggerDict 內**所有已存在
    # 的 named logger** 的 `.disabled` 屬性設成 True(這是
    # disable_existing_loggers=True 的真實效果,跟 root handlers 是兩
    # 件事)。top-level import 在 alembic 之前發生的 logger(像
    # `app.api.studio`)會被廢;後續 lifespan-time 才 import 的(像
    # `app.services.health_checker`)沒事——所以 access log 看得到
    # 但 H-DIAG 看不到。逐一 reset disabled=False 才完整還原。
    for logger_obj in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(logger_obj, logging.Logger):
            logger_obj.disabled = False

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "csp.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(handler)

    # Also emit to stdout so `docker logs` shows access logs + warnings/errors.
    # Without this, RotatingFileHandler captures everything to /var/log only
    # and operational debugging requires shell-into-container.
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(stream_handler)

    logging.getLogger().setLevel(logging.INFO)

    # uvicorn ships with propagate=False on its access logger, so the
    # per-request lines bypass the handlers above. Flip propagate back
    # on so HTTP access lines also appear in docker logs.
    logging.getLogger("uvicorn.access").propagate = True


def _resync_app_identity(target_app: FastAPI) -> None:
    """Re-apply the platform name / version onto the FastAPI object after boot.

    ``FastAPI(title=..., version=...)`` below copies both values **at import
    time**, and the B-class override does not land until the lifespan runs. Skip
    this and one setting gets two answers: ``/health`` and the ``/docs`` page
    title show the new name while ``/openapi.json``'s ``info.title`` still shows
    the old one — exactly the display-vs-effective split this package exists to
    remove. ``openapi_schema`` is FastAPI's cache of the generated document, so
    it is invalidated rather than left holding the pre-override title.

    These two are the only settings read at import time that stay B-editable;
    every other such read is a B_LOCKED entry (the import-time scan in
    tests/test_settings_boot_override.py pins that both ways).
    """
    target_app.title = settings.APP_NAME
    target_app.version = settings.APP_VERSION
    target_app.openapi_schema = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()

    # Sprint 5 X / M1: refuse to boot when known-dev defaults are still in
    # place (SECRET_KEY / admin / service token / DB password). Skipping
    # this check requires explicit ANILA_ALLOW_DEV_SECRET=1.
    from app.services.startup_security import (
        assert_card_dev_bypass_not_in_a_real_boot,
        assert_intranet_lockdown_consistency,
        assert_no_dev_defaults,
    )
    assert_no_dev_defaults()
    # Branch SSO: 確保 REQUIRE_CARD_LOGIN_ONLY 與 ENABLE_CARD_LOGIN 互相一致，
    # 避免「政策設為卡片唯一但卡片功能沒開」的 bricked 狀態。
    assert_intranet_lockdown_consistency()
    # CARD_DEV_SKIP_NONCE_BINDING（關掉卡登反 replay 綁定）只准活在 dev-card
    # 模式裡。這一顆以前沒有任何程式層攔截，加上去就靜默生效。
    assert_card_dev_bypass_not_in_a_real_boot()

    # Run Alembic migrations to bring schema to head.
    # Falls back to create_all if Alembic config is not found (e.g. in tests).
    try:
        _run_alembic_upgrade()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Alembic upgrade failed, falling back to create_all: %s", exc
        )
        Base.metadata.create_all(bind=engine)

    # Round 5 補:alembic.ini 的 [loggers] section 在 _run_alembic_upgrade
    # 內部觸發 logging.fileConfig(),把 setup_logging 加的
    # RotatingFileHandler + stdout StreamHandler 全部 disable、
    # 並把 root logger level 降到 WARN。其結果是 alembic 之後
    # 所有 logger.info(...)(含 R5 H-DIAG)寫不進 csp.log 也不上
    # docker logs,debug 起來像幽靈。重 call setup_logging 把
    # handler + level 補回(setup_logging 已改 idempotent)。
    setup_logging()

    # Now — and not at import time, and not before the line above — is the
    # first moment a log record from this module actually reaches docker
    # logs. The runbook's "is the Host allow-list on?" check greps for
    # this line, so it has to be emitted where logging works.
    log_host_allowlist_state(_allowed_hosts)

    # B-editable settings: whatever the admin last saved in platform_settings is
    # laid over the frozen ``settings`` object here. The position is deliberate
    # and pinned by test_the_hook_runs_after_the_schema_and_before_every_consumer:
    # AFTER the alembic upgrade (the table has to exist) and BEFORE every
    # consumer of a B-editable value — startup migrations, auto_seed
    # (ADMIN_USERNAME / AUTO_REGISTER_*) and the background loops (the health /
    # usage / alert intervals). Applied one line later, the override would be a
    # control that reports success and changes nothing.
    #
    # ⚠ Boot must never hang on the settings table. ``apply_boot_overrides``
    # already turns a broken read into "env values + one loud ERROR + a snapshot
    # that says so"; this second net covers the session itself failing to open.
    from app.config import (
        BOOT_OVERRIDE_LOG_TAG,
        apply_boot_overrides,
        record_boot_override_failure,
    )
    from app.database import SessionLocal as _BootSessionLocal
    _boot_overrides = None
    _boot_db = None
    try:
        _boot_db = _BootSessionLocal()
        _boot_overrides = apply_boot_overrides(_boot_db)
    except Exception as exc:
        # The snapshot must say "we did not look", not stay at its initial
        # "nothing was overridden" — those look identical on the settings page
        # and only one of them is true.
        record_boot_override_failure(
            f"開機時無法連上設定表（{type(exc).__name__}）"
        )
        logging.getLogger(__name__).exception(
            "%s 覆蓋載入本身失敗 —— 以環境變數的值繼續開機", BOOT_OVERRIDE_LOG_TAG
        )
    finally:
        if _boot_db is not None:
            _boot_db.close()
    if _boot_overrides is not None and (
        "app.name" in _boot_overrides.applied or "app.version" in _boot_overrides.applied
    ):
        _resync_app_identity(app)

    # Legacy SQLite migration + column backfills (kept for zero-downtime upgrades
    # from pre-Alembic deployments — safe to re-run, idempotent).
    from app.services.startup_migrations import run_startup_migrations
    run_startup_migrations()

    # P2.7: the audit tables must not be owned by (or writable by) the runtime
    # role. Checked after migrations so a fresh DB has already been locked
    # down by r1_0027; fail-closed in production, warn in dev.
    from app.services.startup_security import assert_audit_ledger_locked_down
    assert_audit_ledger_locked_down()

    # Auto-seed: create admin, register models & links from env vars
    from app.services.auto_seed import auto_seed
    auto_seed()

    # Trusted-host allow-list: backfill ANILA_TRUSTED_HOSTS env into the
    # new DB table (idempotent on unique constraint), then register the
    # cache provider with anila-core's SSRF guard so URL validation sees
    # admin-managed hosts on top of the env fallback.
    from app.database import SessionLocal as _SessionLocal
    from app.services import trusted_host_service
    _db = _SessionLocal()
    try:
        inserted = trusted_host_service.backfill_from_env(_db)
        if inserted:
            logging.getLogger(__name__).info(
                "trusted_hosts: backfilled %d host(s) from ANILA_TRUSTED_HOSTS env",
                inserted,
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "trusted_hosts: env backfill failed (continuing with env-only fallback)"
        )
    finally:
        _db.close()
    trusted_host_service.register_with_url_guard()

    # Start background tasks
    from app.services.alert_detectors import start_alert_detectors
    from app.services.audit_ledger import start_audit_checkpointer
    from app.services.health_checker import start_health_checker
    from app.services.usage_writer import start_usage_writer

    health_task = await start_health_checker()
    writer_task = await start_usage_writer()
    alert_task = await start_alert_detectors()
    # P2.7 稽核帳日級雜湊鏈。熱路徑不受影響 — 封存是每天一次的背景工作。
    ledger_task = await start_audit_checkpointer()

    # Phase 2 Sprint 2 / Chunk H: open the shared anila_core PgPool
    # used by the ingestion inspector endpoints (read-only chunk
    # listing + agent-scoped FTS). The pool registers vector / halfvec
    # / jsonb codecs per-connection, so SQLAlchemy-side queries are
    # untouched. Skip silently if the env / DB isn't available so a
    # pre-0014 schema doesn't crash startup.
    from app.services.ingestion_pool import open_pool, close_pool
    try:
        await open_pool()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Ingestion PgPool open failed (%s) — inspector endpoints "
            "will return 503 until the pool comes back.", exc,
        )

    yield

    # Cleanup
    if health_task:
        health_task.cancel()
    if writer_task:
        writer_task.cancel()
    if alert_task:
        alert_task.cancel()
    if ledger_task:
        ledger_task.cancel()
    await close_pool()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url=None,  # Disable default docs to use offline Swagger UI
    redoc_url=None,
    # 預設 ``/openapi.json`` 是 unauth public,任何訪客都能拿到完整 API schema
    # (含 admin endpoints 的 request body shape) 做 recon。設 None 關掉內建路由,
    # 改用下方 admin-gated 版本。
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def _request_access_log(request, call_next):
    """Per-request access log → stdout so docker logs surfaces it.

    Bypasses uvicorn.access (which has propagate=False and its own
    StreamHandler that doesn't end up in docker logs in this setup).
    Skips noisy /health to keep the log readable.
    """
    import time as _time
    start = _time.monotonic()
    response = await call_next(request)
    # The access log has to name the endpoint that actually ran, or an
    # incident reconstructed from it points at the wrong one — hence
    # ``routed_path`` rather than ``request.url.path``, which is assembled
    # from the caller's ``Host`` header.
    path = routed_path(request)
    if path not in ("/health",):
        elapsed_ms = int((_time.monotonic() - start) * 1000)
        logging.getLogger("csp.access").info(
            "%s %s → %s %dms",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )
    return response

_allowed_origins = [
    origin.strip()
    for origin in (settings.ALLOWED_ORIGINS or "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    # Browsers reject "*" with allow_credentials=True. The config default
    # covers local development (Vite dev server on :5173, nginx on :80/443,
    # direct anila-ui container on :3001). Production must override via
    # ALLOWED_ORIGINS env.
    # No "*" fallback: an empty/misconfigured ALLOWED_ORIGINS denies all
    # cross-origin requests (same-origin SPA via nginx still works) rather
    # than silently opening the API to any origin.
    allow_origins=_allowed_origins,
    allow_credentials=bool(_allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF protection for cookie-authenticated mutating requests. Runs after
# CORS so preflight OPTIONS responses are generated without the check.
app.add_middleware(CsrfMiddleware)


# ── Incoming Host-header allow-list ────────────────────────────────────────
# Hosts that are a structural property of this deployment rather than a
# policy choice, so they are unioned into whatever the operator configures:
#
#   localhost   — the csp container healthcheck calls
#                 http://localhost:8000/health (infra/compose/platform.yml)
#   127.0.0.1   — nginx's loopback readiness listener proxies /health to
#                 csp_backend with `proxy_set_header Host $host`
#                 (infra/nginx/anila.conf), and its own healthcheck speaks
#                 to 127.0.0.1:8080
#   csp         — every in-network caller reaches us at http://csp:8000 over
#                 docker DNS (router, anila-studio, asr-gateway,
#                 ingestion-worker), so the Host is the service name
#
# Without the union, an operator who sets ALLOWED_HOSTS to just the FQDN —
# the obvious thing to type — takes the healthcheck down with it, and
# `depends_on: csp: service_healthy` then stops nginx from starting at all.
# The union opens nothing new: nginx's own $is_anila_host map already
# accepts localhost / 127.0.0.1 from the LAN.
_INTERNAL_HOSTS = ("localhost", "127.0.0.1", "csp")

# The log line the runbook greps for. One token, two verdicts, so that
# "no line at all" reads as "something is wrong" rather than as "off".
HOST_ALLOWLIST_LOG_TAG = "host allow-list:"


def normalize_host(value: str) -> str:
    """Canonical form of a Host header (or of an allow-list entry).

    Hostnames are case-insensitive and the DNS root label is optional, so
    ``ANILA.AI.NCSIST.ORG.TW.`` and ``anila.ai.ncsist.org.tw`` are the same
    name and must get the same verdict. nginx folds case into ``$host``,
    but several csp locations forward ``$http_host`` — the raw header — so
    csp really can see either spelling.

    The port is left attached: ``TrustedHostMiddleware`` strips it itself.
    Splitting on the first ``:`` leaves IPv6 literals (``[::1]:8000``)
    byte-identical, which keeps their existing verdict rather than
    inventing a new one here.
    """
    host, sep, port = value.partition(":")
    return host.lower().rstrip(".") + sep + port


def _validate_host_pattern(pattern: str) -> None:
    """Reject a wildcard shape starlette would only reject on first request.

    ``TrustedHostMiddleware`` checks its patterns with bare ``assert``
    statements, but FastAPI instantiates middleware lazily — so
    ``ALLOWED_HOSTS=10.53.*.15`` (the "cover the subnet" typo) registers
    happily and then raises on *every* request, ``/health`` included. The
    container is then healthy-looking for one probe interval, then
    unhealthy, and `depends_on: csp: service_healthy` keeps nginx from
    starting: a total outage from a typo, with no error naming it.

    Checking here makes it a boot failure that names the pattern, which is
    this tree's established shape for bad config (cf. startup_security).
    It also does not evaporate under ``python -O``, which is what an
    ``assert``-based guard does.
    """
    if "*" not in pattern:
        return
    if not pattern.startswith("*.") or "*" in pattern[1:]:
        raise ValueError(
            f"ALLOWED_HOSTS contains a malformed wildcard pattern: {pattern!r}. "
            "A wildcard entry must be exactly one leading '*.' followed by a "
            "domain suffix (e.g. '*.ncsist.org.tw'); '*' anywhere else — "
            "including subnet-style values like '10.53.*.15' — is not "
            "supported. Use '*' on its own to disable the check entirely."
        )


def parse_allowed_hosts(raw: str | None) -> list[str]:
    """Turn the ALLOWED_HOSTS env string into a starlette allow-list.

    ``"*"`` — and blank, which means "the operator said nothing" — stay the
    disabled sentinel and are returned as ``["*"]``; anything else is a real
    list and gets ``_INTERNAL_HOSTS`` folded in. Entries carry no port:
    ``TrustedHostMiddleware`` matches on ``host.split(":")[0]``, so
    ``Host: csp:8000`` is compared as ``csp``.

    Raises ``ValueError`` on a malformed wildcard — see
    :func:`_validate_host_pattern` for why that is better than letting
    starlette's lazy assert fire on the first request.
    """
    hosts = [h.strip() for h in (raw or "*").split(",") if h.strip()] or ["*"]
    if "*" in hosts:
        return ["*"]
    hosts = [normalize_host(h) for h in hosts]
    for pattern in hosts:
        _validate_host_pattern(pattern)
    return hosts + [h for h in _INTERNAL_HOSTS if h not in hosts]


class HostAllowlistMiddleware(TrustedHostMiddleware):
    """``TrustedHostMiddleware`` with RFC-correct Host comparison.

    The parent compares the raw header, so ``ANILA.AI.NCSIST.ORG.TW`` and a
    trailing-dot FQDN are rejected by an allow-list that contains the same
    name in lower case. Normalising here — rather than reimplementing the
    match — keeps the security decision in the library and confines this
    subclass to spelling.

    The normalised scope is what continues downstream, so the rest of the
    app sees one canonical Host. That is the same folding nginx already
    applies to ``$host``, so it is not a new behaviour for the deployed
    path — only for callers that reach csp:8000 directly.
    """

    async def __call__(self, scope, receive, send):
        if not self.allow_any and scope["type"] in ("http", "websocket"):
            scope = self._normalize_scope_host(scope)
        await super().__call__(scope, receive, send)

    @staticmethod
    def _normalize_scope_host(scope):
        headers = scope.get("headers") or []
        rewritten = []
        changed = False
        for name, value in headers:
            if name == b"host":
                canonical = normalize_host(value.decode("latin-1")).encode("latin-1")
                changed = changed or canonical != value
                rewritten.append((name, canonical))
            else:
                rewritten.append((name, value))
        if not changed:
            return scope
        # Shallow copy: never mutate the scope dict handed to us.
        scope = dict(scope)
        scope["headers"] = rewritten
        return scope


def install_host_allowlist(target_app: FastAPI, raw: str | None) -> list[str]:
    """Register the Host allow-list unless it is disabled.

    Called last on purpose, which makes it the **outermost** middleware:
    starlette builds the stack so that the most recently added wrapper runs
    first. An untrusted Host is therefore rejected before ``CsrfMiddleware``
    — the layer a path-carrying Host header fooled on 2026-08-06 — gets to
    read it at all.
    """
    hosts = parse_allowed_hosts(raw)
    if hosts != ["*"]:
        target_app.add_middleware(HostAllowlistMiddleware, allowed_hosts=hosts)
    return hosts


def log_host_allowlist_state(hosts: list[str]) -> None:
    """Say, in the logs, whether the check is on — and with which hosts.

    Deliberately **not** called at import time. ``setup_logging`` runs
    inside the lifespan, so anything logged while ``app.main`` is being
    imported goes to a root logger with no handlers at level WARNING and
    is dropped: the operator greps, finds nothing, and cannot tell "off"
    from "never printed". Both branches log, so an absent line means the
    boot did not get this far — a third, distinguishable state.
    """
    log = logging.getLogger("csp")
    if hosts == ["*"]:
        log.warning(
            "%s DISABLED — ALLOWED_HOSTS is '*', every incoming Host header "
            "is accepted",
            HOST_ALLOWLIST_LOG_TAG,
        )
    else:
        log.info(
            "%s ENFORCED — %d host(s): %s",
            HOST_ALLOWLIST_LOG_TAG,
            len(hosts),
            ", ".join(hosts),
        )


_allowed_hosts = install_host_allowlist(app, settings.ALLOWED_HOSTS)

app.include_router(api_router)
app.include_router(conversations_router)
app.include_router(attachments_router)
app.include_router(handoffs_router)
app.include_router(directory_router)
# 設定頁的後端（登錄表全 96 顆的實情 + 可編輯那些的寫入）。
app.include_router(platform_settings_router)

# Mount static files for Swagger UI
static_dir = Path(settings.STATIC_DIR)
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui(_admin: User = Depends(require_admin)):
    """Offline Swagger UI — admin tier only (admin / owner)。

    require_admin dependency 跑 side-effect:role 不符直接 raise 403。
    瀏覽器要看 /docs 必須帶 valid session cookies + role in {admin, owner}。
    Swagger UI 載入後會 fetch ``/openapi.json``,該路由同樣 admin-gated,
    browser 帶 cookie 自然通過。
    """
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{settings.APP_NAME} - API 文件",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
    )


@app.get("/openapi.json", include_in_schema=False)
async def custom_openapi(_admin: User = Depends(require_admin)):
    """API schema — admin tier only。

    替代 FastAPI 預設的 public ``/openapi.json``。未登入或非 admin tier 看到
    403,擋住 recon 攻擊面(列舉 endpoints / request body shape)。
    """
    return app.openapi()


@app.get("/health", tags=["health"])
async def health_check():
    """Health check endpoint for container orchestration and monitoring."""
    return {
        "status": "healthy",
        "version": settings.APP_VERSION,
        "service": settings.APP_NAME,
    }


# Serve frontend SPA - check multiple possible locations
frontend_dist = None
for candidate in [
    Path(__file__).parent.parent.parent / "frontend" / "dist",   # dev: backend/../frontend/dist
    Path(__file__).parent.parent / "frontend-dist",              # docker: /app/frontend-dist
]:
    if candidate.exists() and (candidate / "index.html").exists():
        frontend_dist = candidate
        break

if frontend_dist:
    assets_dir = frontend_dist / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="frontend-assets")

    # M6: 確保任何 ``../`` 解析後仍位於 frontend_dist 內；否則一律 fallback
    # 到 SPA index.html，避免讀到 /etc/passwd 或 backend source。
    _frontend_root = frontend_dist.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        index_path = _frontend_root / "index.html"
        # 任何含 NUL / 非法 byte 的 path → 直接給 index.html。
        if "\x00" in full_path:
            return FileResponse(str(index_path))
        try:
            candidate = (_frontend_root / full_path).resolve()
        except (OSError, ValueError):
            return FileResponse(str(index_path))
        # 必須仍位於 _frontend_root 子樹中；否則視為 SPA route fallback。
        try:
            candidate.relative_to(_frontend_root)
        except ValueError:
            return FileResponse(str(index_path))
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index_path))
