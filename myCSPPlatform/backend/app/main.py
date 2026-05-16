import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse
from app.config import settings
from app.database import engine, Base
from app.api.router import api_router
from app.api.conversations import router as conversations_router
from app.api.attachments import router as attachments_router
from app.api.handoffs import router as handoffs_router
from app.api.public_share import router as public_share_router
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
    logging.getLogger().setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()

    # Sprint 5 X / M1: refuse to boot when known-dev defaults are still in
    # place (SECRET_KEY / admin / service token / DB password). Skipping
    # this check requires explicit ANILA_ALLOW_DEV_SECRET=1.
    from app.services.startup_security import (
        assert_intranet_lockdown_consistency,
        assert_no_dev_defaults,
    )
    assert_no_dev_defaults()
    # Branch SSO: 確保 REQUIRE_CARD_LOGIN_ONLY 與 ENABLE_CARD_LOGIN 互相一致，
    # 避免「政策設為卡片唯一但卡片功能沒開」的 bricked 狀態。
    assert_intranet_lockdown_consistency()

    # Run Alembic migrations to bring schema to head.
    # Falls back to create_all if Alembic config is not found (e.g. in tests).
    try:
        _run_alembic_upgrade()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Alembic upgrade failed, falling back to create_all: %s", exc
        )
        Base.metadata.create_all(bind=engine)

    # Legacy SQLite migration + column backfills (kept for zero-downtime upgrades
    # from pre-Alembic deployments — safe to re-run, idempotent).
    from app.services.startup_migrations import run_startup_migrations
    run_startup_migrations()

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
    from app.services.health_checker import start_health_checker
    from app.services.usage_writer import start_usage_writer

    health_task = await start_health_checker()
    writer_task = await start_usage_writer()

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
    allow_origins=_allowed_origins or ["*"],
    allow_credentials=bool(_allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF protection for cookie-authenticated mutating requests. Runs after
# CORS so preflight OPTIONS responses are generated without the check.
app.add_middleware(CsrfMiddleware)

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
