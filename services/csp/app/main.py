import logging
import os
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
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, inspect as sa_inspect, text
from app.config import settings
from app.database import engine, Base
from app.api.router import api_router
from app.api.conversations import router as conversations_router
from app.api.thinking import router as thinking_router
from app.api.attachments import router as attachments_router
from app.api.handoffs import router as handoffs_router
from app.api.directory import router as directory_router
from app.api.platform_settings import router as platform_settings_router
from app.api.router_prompts import router as router_prompts_router
from app.middleware.csrf import CsrfMiddleware
from app.models.user import User
from app.services.auth_service import require_admin

APP_NAME = "ANILA"
APP_VERSION = "1.0.0"
STATIC_DIR = Path(__file__).parent / "static"

# Operator opt-out of boot-time schema changes. Default (unset / any other
# value) is to migrate. Truthy literals are strip+lower of 1/true/yes only.
SKIP_STARTUP_MIGRATIONS_ENV = "ANILA_SKIP_STARTUP_MIGRATIONS"
_SKIP_STARTUP_MIGRATIONS_TRUTHY = frozenset({"1", "true", "yes"})
# Dedicated, grep-able prefix. Do not mix the X → Y announcement into
# generic alembic / uvicorn lines.
STARTUP_MIGRATION_TAG = "STARTUP MIGRATION"


class StartupMigrationError(RuntimeError):
    """Non-empty database could not be migrated; the process must not serve."""


def _alembic_paths() -> tuple[Path, Path]:
    root = Path(__file__).parent.parent
    return root / "migrations", root / "alembic.ini"


def _alembic_config():
    from alembic.config import Config

    migrations_dir, alembic_ini = _alembic_paths()
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(migrations_dir))
    return cfg


def _run_alembic_upgrade() -> None:
    """Run `alembic upgrade head` programmatically at startup.

    Alembic itself reads ``MIGRATION_DATABASE_URL`` in ``migrations/env.py``
    (superuser ``csp``). Do not point this at the runtime ``csp_app`` engine:
    a fresh volume has no ``csp_app`` role until revision 0014.
    """
    from alembic import command

    command.upgrade(_alembic_config(), "head")


def _alembic_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("alembic") is not None


def _alembic_head_revision() -> str:
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    if len(heads) != 1:
        raise StartupMigrationError(f"alembic head is not unique: {heads!r}")
    return heads[0]


def _startup_migrations_refused() -> bool:
    raw = os.environ.get(SKIP_STARTUP_MIGRATIONS_ENV, "")
    return raw.strip().lower() in _SKIP_STARTUP_MIGRATIONS_TRUTHY


def _is_sqlite_pytest_host(bind) -> bool:
    """Sqlite under pytest must not run the Postgres-only alembic chain.

    The test runner is the signal, not whether the alembic package is
    installed (requirements pin alembic; the image has it). Production
    uvicorn does not set PYTEST_CURRENT_TEST and does not import pytest.
    Dialect alone is not enough: a sqlite DATABASE_URL in production
    must still fail closed, not skip upgrade.
    """
    if bind.dialect.name != "sqlite":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules


def _database_is_empty(bind) -> bool:
    return not sa_inspect(bind).get_table_names()


def _migration_database_url() -> str:
    """Same URL alembic uses — never the runtime csp_app DSN by preference."""
    return (os.environ.get("MIGRATION_DATABASE_URL") or "").strip() or settings.DATABASE_URL


def _current_schema_revision() -> str | None:
    """Read ``alembic_version`` on the migration URL, not runtime ``engine``.

    Inspect errors (empty volume, unreachable) become ``None`` so we still
    attempt ``upgrade head``; they must not fail the boot before alembic.
    """
    url = _migration_database_url()
    inspect_engine = create_engine(url)
    try:
        names = sa_inspect(inspect_engine).get_table_names()
        if "alembic_version" not in names:
            return None
        with inspect_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).fetchall()
        if not rows:
            return None
        return ",".join(sorted({row[0] for row in rows}))
    except Exception:
        return None
    finally:
        inspect_engine.dispose()


def _apply_startup_schema(bind=None) -> None:
    """Bring schema to alembic head.

    Postgres (empty included) always ``upgrade head`` via
    ``MIGRATION_DATABASE_URL``. Never inspect or ``create_all`` on the
    runtime ``csp_app`` engine before that.

    Sqlite under pytest never runs that chain (PG-only DDL). That hatch
    is keyed off the test runner, not off alembic being absent.
    ``create_all`` is only that hatch, and only when the sqlite file is
    empty.

    Upgrade failure is fatal — never ``create_all``.
    ``ANILA_SKIP_STARTUP_MIGRATIONS=1|true|yes`` refuses the action.
    """
    log = logging.getLogger("csp.startup_migration")
    if bind is None:
        bind = engine

    if _startup_migrations_refused():
        raw = os.environ.get(SKIP_STARTUP_MIGRATIONS_ENV, "")
        log.warning(
            "%s refused by %s=%r (starting without migrating)",
            STARTUP_MIGRATION_TAG,
            SKIP_STARTUP_MIGRATIONS_ENV,
            raw,
        )
        return

    if _is_sqlite_pytest_host(bind):
        if _database_is_empty(bind):
            log.warning(
                "%s sqlite pytest host — create_all, not alembic upgrade",
                STARTUP_MIGRATION_TAG,
            )
            Base.metadata.create_all(bind=bind)
        else:
            log.warning(
                "%s sqlite pytest host — not running alembic upgrade",
                STARTUP_MIGRATION_TAG,
            )
        return

    if not _alembic_available():
        raise StartupMigrationError(
            "alembic is not installed; refusing to start "
            "(create_all is not a production bootstrap)"
        )

    # Alembic path. Do not call _database_is_empty(runtime engine): a
    # fresh volume has POSTGRES_USER=csp only; csp_app is created in 0014.
    current = _current_schema_revision() or "unversioned"
    head = "head"
    try:
        head = _alembic_head_revision()
        # WARNING so the line survives alembic.ini fileConfig (root → WARN)
        # and stays grep-able instead of mixing into generic alembic logs.
        log.warning("%s 即將 %s → %s", STARTUP_MIGRATION_TAG, current, head)
        _run_alembic_upgrade()
    except StartupMigrationError:
        raise
    except Exception as exc:
        raise StartupMigrationError(
            f"alembic upgrade failed ({current} → {head}): {exc}"
        ) from exc


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
        assert_card_dev_bypass_not_in_a_real_boot,
        assert_intranet_lockdown_consistency,
        assert_no_dev_defaults,
    )
    assert_no_dev_defaults()
    # Branch SSO: 驗證單一 ANILA_AUTH_MODE，避免 auth policy 自相矛盾。
    assert_intranet_lockdown_consistency()
    # CARD_DEV_SKIP_NONCE_BINDING（關掉卡登反 replay 綁定）只准活在 dev-card
    # 模式裡。這一顆以前沒有任何程式層攔截，加上去就靜默生效。
    assert_card_dev_bypass_not_in_a_real_boot()

    # Empty Postgres also runs alembic (MIGRATION_DATABASE_URL / superuser).
    # Sqlite pytest host never runs that chain (PG-only DDL), whether or
    # not the alembic package is installed.
    # ANILA_SKIP_STARTUP_MIGRATIONS refuses the whole action.
    # Upgrade failure is fatal: create_all cannot add columns, /health
    # would still return 200.
    _apply_startup_schema()

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

    # Startup backfills (safe to re-run, idempotent). Same refuse flag:
    # "start without migrating" includes these ADD COLUMN backfills.
    if _startup_migrations_refused():
        logging.getLogger("csp.startup_migration").warning(
            "%s skipping run_startup_migrations backfills",
            STARTUP_MIGRATION_TAG,
        )
    else:
        from app.services.startup_migrations import run_startup_migrations
        run_startup_migrations()

    # P2.7: the audit tables must not be owned by (or writable by) the runtime
    # role. Checked after migrations so a fresh DB has already been locked
    # down by r1_0027; fail-closed in production, warn in dev.
    from app.services.startup_security import assert_audit_ledger_locked_down
    assert_audit_ledger_locked_down()

    # Auto-seed: create admin, register models and API keys from env vars
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

    # Internal service credentials (router-primary, and any client named in
    # ANILA_INTERNAL_SERVICE_CLIENTS). One pass before we report healthy so
    # the router, which waits on this healthcheck, finds the token file.
    # The periodic task repeats the same ensure. Tests set
    # ANILA_SERVICE_CLIENT_AUTO_PROVISION=0 and skip both.
    from app.services.internal_service_clients import (
        auto_provision_enabled,
        provision_internal_service_clients_once,
        start_internal_service_client_provisioner,
    )
    provision_task = None
    if auto_provision_enabled():
        # Failures are recorded for /health. Startup continues so the
        # process can report degraded instead of exiting before the probe.
        provision_internal_service_clients_once()
        provision_task = await start_internal_service_client_provisioner()

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
    if provision_task:
        provision_task.cancel()
    await close_pool()


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
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
app.include_router(thinking_router)
app.include_router(attachments_router)
app.include_router(handoffs_router)
app.include_router(directory_router)
# 設定頁的後端（目前只提供 12 顆立即生效的 C 類設定）。
app.include_router(platform_settings_router)
app.include_router(router_prompts_router)

# Mount static files for Swagger UI
static_dir = STATIC_DIR
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
        title=f"{APP_NAME} - API 文件",
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
    """Health check endpoint for container orchestration and monitoring.

    Required internal-client provisioning is part of readiness. A write
    or database failure, or auto-provision turned off, is HTTP 503
    ``status=degraded`` so a deploy does not look healthy while the
    router credential is not being published. ``service_client_provisioning``
    is ``disabled`` when auto-provision is off.
    """
    from app.services.internal_service_clients import provisioning_health

    ready = provisioning_health()
    body = {
        "status": "healthy" if ready["ok"] else "degraded",
        "version": APP_VERSION,
        "service": APP_NAME,
        "service_client_provisioning": ready["state"],
    }
    if ready["failed"]:
        body["service_client_provisioning_failed"] = ready["failed"]
    if not ready["ok"]:
        return JSONResponse(status_code=503, content=body)
    return body


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
