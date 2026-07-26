import asyncio
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from app.config import settings
from app.errors import install_error_envelope_schema, register_exception_handlers
from app.api.router import api_router
from app.api.conversations import router as conversations_router
from app.api.attachments import router as attachments_router
from app.api.handoffs import router as handoffs_router
from app.api.public_share import router as public_share_router
from app.middleware.csrf import CsrfMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.models.user import User
from app.schemas.model_governance import ModelGovernanceReadiness
from app.services.auth_service import require_admin
from app.services.model_governance_runtime import governance_required_for_settings


def _run_alembic_upgrade() -> None:
    """Run `alembic upgrade head` programmatically at startup."""
    from alembic.config import Config
    from alembic import command

    migrations_dir = Path(__file__).parent.parent / "migrations"
    alembic_ini = Path(__file__).parent.parent / "alembic.ini"

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(migrations_dir))
    command.upgrade(cfg, "head")


def _set_migration_state(
    application: FastAPI, status: str, error: str | None = None
) -> None:
    application.state.migration_status = status
    application.state.migration_error = error


def _refresh_model_governance_readiness(application: FastAPI) -> None:
    """Refresh mounted Gate 5 evidence before exposing health/readiness.

    The runtime deliberately reloads its signed material and observed facts
    atomically.  Rechecking here closes the window where a profile, trust
    store, or deployment-health evidence is rotated/staled after lifespan
    bootstrap but before the next health probe.  Applications/tests that do
    not install the optional runtime keep their existing health behavior.
    """

    governance_required = governance_required_for_settings(settings) or settings.GATE5_MODEL_GOVERNANCE_ENABLED
    if not governance_required:
        return
    runtime = getattr(application.state, "model_governance_runtime", None)
    if runtime is None:
        return
    try:
        application.state.model_governance_readiness = runtime.reload(
            now=datetime.now(timezone.utc)
        )
    except Exception as exc:
        application.state.model_governance_readiness = ModelGovernanceReadiness(
            status="not_ready",
            ready=False,
            reason=f"runtime readiness refresh failed: {type(exc).__name__}",
            checked_at=datetime.now(timezone.utc),
        )


def _apply_schema_migrations(application: FastAPI) -> None:
    """Apply every startup schema step or abort the process.

    There is intentionally no ``create_all`` recovery. A partial/failed
    Alembic chain is not equivalent to the current ORM schema and must never
    be presented as a healthy production instance.
    """
    if settings.SKIP_STARTUP_MIGRATIONS:
        _set_migration_state(application, "skipped")
        return

    _set_migration_state(application, "running")
    try:
        _run_alembic_upgrade()
        from app.services import startup_migrations

        startup_migrations.run_startup_migrations()
    except Exception as exc:
        _set_migration_state(application, "failed", type(exc).__name__)
        # Alembic's logging config can disable the application's handlers.
        # Restore them before recording the fatal startup event.
        setup_logging()
        logging.getLogger(__name__).exception(
            "Database migration failed; refusing to start"
        )
        raise RuntimeError("Database migration failed; refusing to start") from exc
    _set_migration_state(application, "succeeded")


def _migration_health_payload(application: FastAPI) -> dict:
    _refresh_model_governance_readiness(application)
    migration_status = getattr(application.state, "migration_status", "pending")
    if migration_status in {"succeeded", "skipped"}:
        overall = "healthy"
    elif migration_status == "failed":
        overall = "unhealthy"
    else:
        overall = "starting"
    payload = {
        "status": overall,
        "version": settings.APP_VERSION,
        "service": settings.APP_NAME,
        "migration_status": migration_status,
    }
    governance = getattr(application.state, "model_governance_readiness", None)
    governance_required = governance_required_for_settings(settings) or settings.GATE5_MODEL_GOVERNANCE_ENABLED
    if governance_required and governance is not None:
        payload["model_governance"] = governance.model_dump(mode="json")
        if not governance.ready:
            payload["status"] = "unhealthy"
    migration_error = getattr(application.state, "migration_error", None)
    if migration_error:
        payload["migration_error"] = migration_error
    return payload


def _readiness_response(application: FastAPI) -> JSONResponse:
    payload = _migration_health_payload(application)
    relay_task = getattr(application.state, "ingestion_relay_task", None)
    relay_ready = relay_task is not None and not relay_task.done()
    governance = getattr(application.state, "model_governance_readiness", None)
    governance_required = governance_required_for_settings(settings) or settings.GATE5_MODEL_GOVERNANCE_ENABLED
    governance_ready = not governance_required or (governance is not None and governance.ready)
    ready = (
        payload["migration_status"] in {"succeeded", "skipped"}
        and relay_ready
        and governance_ready
    )
    governance_payload = (
        governance.model_dump(mode="json")
        if governance is not None
        else {"status": "not_configured", "ready": False}
    )
    content = {
        "status": "ready" if ready else "not_ready",
        "migration_status": payload["migration_status"],
        "ingestion_outbox_relay": "running" if relay_ready else "stopped",
    }
    if governance_required:
        content["model_governance"] = governance_payload
    return JSONResponse(
        status_code=200 if ready else 503,
        content=content,
    )


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()

    # Sprint 5 X / M1: refuse to boot when known-dev defaults are still in
    # place (SECRET_KEY / admin / service token / DB password). Skipping
    # this check requires explicit ANILA_ALLOW_DEV_SECRET=1.
    from app.services.startup_security import (
        assert_card_only_data_feature_policy,
        assert_artifact_blob_storage_policy,
        assert_card_nonce_binding_policy,
        assert_card_crl_policy,
        assert_deployment_profile_posture,
        assert_gate2_pilot_profile,
        assert_intranet_lockdown_consistency,
        assert_ingestion_queue_integrity_policy,
        assert_no_dev_defaults,
        assert_runtime_deadline_policy,
        assert_retention_policy,
        assert_secure_cookie_policy,
        assert_source_snapshot_storage_policy,
        assert_startup_migration_policy,
    )
    assert_deployment_profile_posture()
    assert_gate2_pilot_profile()
    assert_no_dev_defaults()
    assert_card_nonce_binding_policy()
    assert_card_crl_policy()
    assert_secure_cookie_policy()
    assert_source_snapshot_storage_policy()
    assert_artifact_blob_storage_policy()
    assert_ingestion_queue_integrity_policy()
    assert_retention_policy()
    assert_startup_migration_policy()
    assert_runtime_deadline_policy()
    # Branch SSO: 確保 REQUIRE_CARD_LOGIN_ONLY 與 ENABLE_CARD_LOGIN 互相一致，
    # 避免「政策設為卡片唯一但卡片功能沒開」的 bricked 狀態。
    assert_intranet_lockdown_consistency()
    assert_card_only_data_feature_policy()

    # Gate 5 model governance is a CSP-owned readiness boundary.  Bootstrap
    # reads only explicit mounted paths and never treats the disabled template
    # as approval.  Formal deployments may make this a startup hard-stop;
    # otherwise /ready remains 503 until the signed material is provisioned.
    from app.services.model_governance_runtime import ModelGovernanceRuntime
    from app.services.model_governance_receipts import (
        set_model_governance_runtime_provider,
    )

    model_governance_runtime = ModelGovernanceRuntime.from_settings(settings)
    app.state.model_governance_runtime = model_governance_runtime
    set_model_governance_runtime_provider(
        lambda: getattr(app.state, "model_governance_runtime", None)
    )
    model_governance_readiness = model_governance_runtime.bootstrap()
    app.state.model_governance_readiness = model_governance_readiness
    if model_governance_runtime.startup_required and not model_governance_readiness.ready:
        raise RuntimeError(
            "Gate 5 model-governance bootstrap failed: "
            + model_governance_readiness.reason
        )

    # Run the complete schema chain before any seed/background work. Any
    # failure is fatal; SQLite unit fixtures explicitly opt out and create
    # their own schema in tests/conftest.py.
    _apply_schema_migrations(app)

    # Round 5 補:alembic.ini 的 [loggers] section 在 _run_alembic_upgrade
    # 內部觸發 logging.fileConfig(),把 setup_logging 加的
    # RotatingFileHandler + stdout StreamHandler 全部 disable、
    # 並把 root logger level 降到 WARN。其結果是 alembic 之後
    # 所有 logger.info(...)(含 R5 H-DIAG)寫不進 csp.log 也不上
    # docker logs,debug 起來像幽靈。重 call setup_logging 把
    # handler + level 補回(setup_logging 已改 idempotent)。
    setup_logging()

    # Auto-seed: create admin, register models & links from env vars
    from app.services.auto_seed import auto_seed
    auto_seed()

    if settings.STUDIO_ARTIFACT_SERVICE_TOKEN:
        from app.database import SessionLocal as _ArtifactSessionLocal
        from app.services.artifact_service_bootstrap import ensure_artifact_service

        _artifact_db = _ArtifactSessionLocal()
        try:
            ensure_artifact_service(
                _artifact_db, token=settings.STUDIO_ARTIFACT_SERVICE_TOKEN
            )
        finally:
            _artifact_db.close()

    if settings.STUDIO_RUNTIME_SERVICE_TOKEN:
        from app.database import SessionLocal as _RuntimeSessionLocal
        from app.services.studio_runtime_service_bootstrap import (
            ensure_studio_runtime_service,
        )

        _runtime_db = _RuntimeSessionLocal()
        try:
            ensure_studio_runtime_service(
                _runtime_db,
                token=settings.STUDIO_RUNTIME_SERVICE_TOKEN,
                artifact_token=settings.STUDIO_ARTIFACT_SERVICE_TOKEN,
            )
        finally:
            _runtime_db.close()

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
    from app.services.health_checker import start_health_checker
    from app.services.ingestion_outbox import start_ingestion_outbox_relay
    from app.services.usage_writer import start_usage_writer
    from app.services.retention_reaper import start_retention_reaper

    health_task = await start_health_checker()
    writer_task = await start_usage_writer()
    ingestion_relay_task = await start_ingestion_outbox_relay()
    app.state.ingestion_relay_task = ingestion_relay_task
    retention_task = await start_retention_reaper()

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

    try:
        yield
    finally:
        try:
            # Cleanup
            if health_task:
                health_task.cancel()
            if writer_task:
                writer_task.cancel()
            if ingestion_relay_task:
                ingestion_relay_task.cancel()
                with suppress(asyncio.CancelledError):
                    await ingestion_relay_task
                app.state.ingestion_relay_task = None
            if retention_task:
                retention_task.cancel()
                with suppress(asyncio.CancelledError):
                    await retention_task
            await close_pool()
        finally:
            # Do not leak a previous app/runtime into the next test or
            # lifespan instance in this process.
            set_model_governance_runtime_provider(None)


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url=None,  # Disable default docs to use offline Swagger UI
    redoc_url=None,
    # 預設 ``/openapi.json`` 是 unauth public,任何訪客都能拿到完整 API schema
    # (含 admin endpoints 的 request body shape) 做 recon。設 None 關掉內建路由,
    # 改用下方 admin-gated 版本(中科院內網比 ENABLE_API_DOCS gating 更嚴)。
    openapi_url=None,
    lifespan=lifespan,
)
_set_migration_state(app, "pending")
app.state.model_governance_readiness = ModelGovernanceReadiness(
    status="not_configured",
    ready=True,
    reason="Gate 5 model governance has not been enabled",
    checked_at=datetime.now(timezone.utc),
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
    if request.url.path not in ("/health",):
        elapsed_ms = int((_time.monotonic() - start) * 1000)
        logging.getLogger("csp.access").info(
            "%s %s → %s %dms",
            request.method,
            request.url.path,
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

# Incoming Host-header allow-list. Default "*" is a no-op (non-breaking);
# operators pin ALLOWED_HOSTS in prod to block Host-header injection.
_allowed_hosts = [
    h.strip() for h in (settings.ALLOWED_HOSTS or "*").split(",") if h.strip()
] or ["*"]
if _allowed_hosts != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
    logging.getLogger("csp").info(
        "TrustedHostMiddleware enabled for %d host(s)", len(_allowed_hosts)
    )

# CSRF protection for cookie-authenticated mutating requests. Runs after
# CORS so preflight OPTIONS responses are generated without the check.
app.add_middleware(CsrfMiddleware)

# W3-3⑤:每個請求一個 id。**最後註冊 = 最外層執行**,所以連被 CSRF 或
# TrustedHost 擋掉的請求也拿得到 id 並留下 access log —— 那些正是最需要被查的。
# 這也讓 W2-12 錯誤信封的 `request_id` 從「永遠 null」變成真值:使用者念得出
# 畫面上的 id,支援端 grep 得到同一個字串。
app.add_middleware(RequestIdMiddleware)

# W2-12:統一錯誤信封。在此之前全 repo 零 ``add_exception_handler``,於是
# ``HTTPException`` 的 ``{"detail": str}``、3 處的 ``{"detail": {...}}`` 與 422 的
# ``{"detail": [...]}`` 是三種形狀,前端把後兩者渲染成 ``[object Object]``。
# 註冊處刻意放在 include_router 之前並集中在一行,方便確認「總共只掛兩個」。
register_exception_handlers(app)
install_error_envelope_schema(app)

app.include_router(api_router)
app.include_router(conversations_router)
app.include_router(attachments_router)
app.include_router(handoffs_router)
app.include_router(public_share_router)

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
async def health_check(request: Request):
    """Liveness with explicit schema-migration state."""
    return _migration_health_payload(request.app)


@app.get("/ready", tags=["health"])
async def readiness_check(request: Request):
    """Readiness is 200 only after the complete migration chain succeeds."""
    return _readiness_response(request.app)


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
