import logging
from logging.handlers import RotatingFileHandler
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
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

    # Start background tasks
    from app.services.health_checker import start_health_checker
    from app.services.usage_writer import start_usage_writer

    health_task = await start_health_checker()
    writer_task = await start_usage_writer()

    yield

    # Cleanup
    if health_task:
        health_task.cancel()
    if writer_task:
        writer_task.cancel()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url=None,  # Disable default docs to use offline Swagger UI
    redoc_url=None,
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
async def custom_swagger_ui():
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{settings.APP_NAME} - API 文件",
        swagger_js_url="/static/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui.css",
    )


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

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        file_path = frontend_dist / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(frontend_dist / "index.html"))
