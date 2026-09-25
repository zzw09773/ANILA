"""P3.2 alert detectors — five failure classes + consecutive-failure streaks.

Uses existing ``upsert_alert`` / ``resolve_alert_by_fingerprint`` vocabulary.
Does not invent a second ledger. Alert messages never carry raw endpoint
addresses (URLs stay in gated metadata when present).

Thresholds are tuned for a single maintainer: a detector that fires on a
normal day trains the operator to ignore the real one.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError as SATimeoutError

from app.database import SessionLocal, engine
from app.models.alert import Alert
from app.services.alert_notifier import notify_alert_opened
from app.services.alert_service import resolve_alert_by_fingerprint, upsert_alert
from app.services.storage_paths import (
    ATTACHMENT_STORAGE_ROOT,
    INGESTION_UPLOAD_ROOT,
)

logger = logging.getLogger(__name__)

# ── Thresholds (justification in w-alerts-report.md) ─────────────────────────

#: nginx probe consecutive unhealthy before "平台入口無回應".
PLATFORM_INGRESS_STREAK = 3

#: DB probe consecutive failures (server gone OR pool exhausted, separate fps).
DB_FAIL_STREAK = 2

#: Model-gateway traffic: consecutive exhausted-retry failures (non-agent).
GATEWAY_FAIL_STREAK = 5

#: Agent dispatch: consecutive exhausted-retry failures.
AGENT_FAIL_STREAK = 3

#: Disk used% → high (morning) / critical (wake).
DISK_WARN_PCT = 85.0
DISK_CRIT_PCT = 95.0

FP_PLATFORM_INGRESS = "platform:ingress"
FP_DB_SERVER = "db:server_gone"
FP_DB_POOL = "db:pool_exhausted"


# ── In-memory consecutive counters ───────────────────────────────────────────

class FailureStreakTracker:
    """Process-local consecutive failure counters. Success resets to zero.

    Not durable across restarts — after a restart the operator gets a fresh
    quiet window, which is the safe direction (miss one streak rebuild rather
    than false-positive on boot).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def record_failure(self, key: str) -> int:
        with self._lock:
            n = self._counts.get(key, 0) + 1
            self._counts[key] = n
            return n

    def record_success(self, key: str) -> int:
        with self._lock:
            prev = self._counts.pop(key, 0)
            return prev

    def get(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


_streaks = FailureStreakTracker()


def get_streak_tracker() -> FailureStreakTracker:
    return _streaks


def reset_streaks_for_tests() -> None:
    _streaks.clear()


# ── Ledger helpers ───────────────────────────────────────────────────────────

def _emit_alert(
    db,
    *,
    fingerprint: str,
    category: str,
    severity: str,
    title: str,
    message: str,
    source_type: str | None = None,
    source_id: str | int | None = None,
    metadata: dict | None = None,
) -> bool:
    """Upsert + notify on create/reopen. Returns True if notifier was called.

    ``message`` must not contain raw endpoint addresses.
    """
    existing = db.query(Alert).filter(Alert.fingerprint == fingerprint).first()
    was_open_transition = existing is None or existing.status == "resolved"
    upsert_alert(
        db,
        fingerprint=fingerprint,
        category=category,
        severity=severity,
        title=title,
        message=message,
        source_type=source_type,
        source_id=source_id,
        metadata=metadata,
    )
    if was_open_transition:
        notify_alert_opened(
            fingerprint=fingerprint,
            category=category,
            severity=severity,
            title=title,
            message=message,
            source_type=source_type,
            source_id=source_id,
        )
    return was_open_transition


def _resolve(db, fingerprint: str) -> None:
    resolve_alert_by_fingerprint(db, fingerprint)


def _emit_alert_autocommit(**kwargs) -> bool:
    """Open a short session, emit, commit. On DB failure still notify."""
    fingerprint = kwargs["fingerprint"]
    try:
        db = SessionLocal()
        try:
            opened = _emit_alert(db, **kwargs)
            db.commit()
            return opened
        finally:
            db.close()
    except Exception:
        logger.exception(
            "alert ledger write failed fingerprint=%s — notifying without ledger",
            fingerprint,
        )
        notify_alert_opened(
            fingerprint=fingerprint,
            category=kwargs["category"],
            severity=kwargs["severity"],
            title=kwargs["title"],
            message=kwargs["message"],
            source_type=kwargs.get("source_type"),
            source_id=kwargs.get("source_id"),
        )
        return True


def _resolve_autocommit(fingerprint: str) -> None:
    try:
        db = SessionLocal()
        try:
            _resolve(db, fingerprint)
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("alert resolve failed fingerprint=%s", fingerprint)


def _with_db(db, emit_kwargs: dict | None = None, resolve_fp: str | None = None) -> bool:
    """Use caller session when provided (tests); otherwise autocommit helpers."""
    if db is not None:
        if resolve_fp is not None:
            _resolve(db, resolve_fp)
            return False
        assert emit_kwargs is not None
        return _emit_alert(db, **emit_kwargs)
    if resolve_fp is not None:
        _resolve_autocommit(resolve_fp)
        return False
    assert emit_kwargs is not None
    return _emit_alert_autocommit(**emit_kwargs)


# ── 1. Platform ingress (honest about CSP-self) ──────────────────────────────

@dataclass(frozen=True, slots=True)
class PlatformProbeOutcome:
    """Result of one internal ingress check."""

    reachable: bool
    reason: str


async def probe_platform_ingress() -> PlatformProbeOutcome:
    """From inside CSP, the closest signal to 'platform unresponsive' is the
    front door (nginx). CSP itself cannot alert that CSP is dead — that needs
    an external watcher hitting ``/health``.
    """
    from app.services.health_checker import (
        HEALTH_HEALTHY,
        HEALTH_UNKNOWN,
        probe_base_service,
    )

    result = await probe_base_service("nginx")
    # not_deployed / unknown → do not fire (dev stacks without nginx).
    if result.status == HEALTH_UNKNOWN:
        return PlatformProbeOutcome(True, result.reason)
    if result.status == HEALTH_HEALTHY:
        return PlatformProbeOutcome(True, result.reason)
    return PlatformProbeOutcome(False, result.reason)


async def evaluate_platform_ingress(*, db=None) -> int:
    """Update streak; emit/resolve platform:ingress. Returns current streak."""
    outcome = await probe_platform_ingress()
    if outcome.reachable:
        _streaks.record_success(FP_PLATFORM_INGRESS)
        # Always resolve on healthy probe — in-memory streak is empty after
        # process restart, but the ledger row may still be open.
        _with_db(db, resolve_fp=FP_PLATFORM_INGRESS)
        return 0

    streak = _streaks.record_failure(FP_PLATFORM_INGRESS)
    if streak >= PLATFORM_INGRESS_STREAK:
        _with_db(
            db,
            emit_kwargs={
                "fingerprint": FP_PLATFORM_INGRESS,
                "category": "platform",
                "severity": "critical",
                "title": "平台入口無回應",
                "message": (
                    f"從控制平面連續 {streak} 次無法連上反向代理（nginx）。"
                    f"使用者可能已進不了平台。原因碼：{outcome.reason}。"
                    "若控制平面本身已死，此告警不會發出——需外部 watcher 打 /health。"
                ),
                "source_type": "platform",
                "source_id": "nginx",
                "metadata": {"reason": outcome.reason, "streak": streak},
            },
        )
    return streak


# ── 2. Database: server gone vs pool exhausted ───────────────────────────────

@dataclass(frozen=True, slots=True)
class DatabaseProbeOutcome:
    """Discriminated DB health sample."""

    ok: bool
    kind: str  # ok | server_gone | pool_exhausted
    detail: str


def probe_database_discriminated() -> DatabaseProbeOutcome:
    """Distinguish pool checkout timeout from a dead server.

    Pool exhausted → maintainer looks at connection leaks / concurrency.
    Server gone → maintainer looks at postgres / disk / host.
    Conflating them sends the one maintainer to the wrong place at 2am.
    """
    pool = engine.pool
    checkedout = getattr(pool, "checkedout", lambda: None)()
    size = getattr(pool, "size", lambda: None)()
    overflow = getattr(pool, "overflow", lambda: None)()

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return DatabaseProbeOutcome(True, "ok", "select_1_ok")
    except SATimeoutError as exc:
        return DatabaseProbeOutcome(
            False,
            "pool_exhausted",
            f"pool_timeout checkedout={checkedout} size={size} overflow={overflow} "
            f"exc={type(exc).__name__}",
        )
    except OperationalError as exc:
        # psycopg / sqlite operational — server unreachable, auth, etc.
        return DatabaseProbeOutcome(
            False,
            "server_gone",
            f"operational_error checkedout={checkedout} exc={type(exc).__name__}",
        )
    except Exception as exc:
        # Unknown — treat as server_gone (conservative: look at DB first).
        orig = getattr(exc, "orig", None)
        name = type(exc).__name__
        if "Timeout" in name or "timeout" in str(exc).lower():
            return DatabaseProbeOutcome(
                False,
                "pool_exhausted",
                f"timeout_like checkedout={checkedout} size={size} "
                f"overflow={overflow} exc={name}",
            )
        return DatabaseProbeOutcome(
            False,
            "server_gone",
            f"unexpected orig={type(orig).__name__ if orig else '-'} exc={name}",
        )


def evaluate_database(
    *,
    db=None,
    outcome: DatabaseProbeOutcome | None = None,
) -> DatabaseProbeOutcome:
    outcome = outcome if outcome is not None else probe_database_discriminated()
    if outcome.ok:
        for fp in (FP_DB_SERVER, FP_DB_POOL):
            _streaks.record_success(fp)
            _with_db(db, resolve_fp=fp)
        return outcome

    if outcome.kind == "pool_exhausted":
        _streaks.record_success(FP_DB_SERVER)
        _with_db(db, resolve_fp=FP_DB_SERVER)
        streak = _streaks.record_failure(FP_DB_POOL)
        if streak >= DB_FAIL_STREAK:
            _with_db(
                db,
                emit_kwargs={
                    "fingerprint": FP_DB_POOL,
                    "category": "database",
                    "severity": "high",
                    "title": "資料庫連線池耗盡",
                    "message": (
                        f"連續 {streak} 次從連線池取出連線逾時。"
                        "資料庫行程可能仍在；請查連線洩漏或尖峰併發，不要先重啟 postgres。"
                    ),
                    "source_type": "database",
                    "source_id": "pool",
                    "metadata": {"detail": outcome.detail, "streak": streak},
                },
            )
        return outcome

    # server_gone
    _streaks.record_success(FP_DB_POOL)
    _with_db(db, resolve_fp=FP_DB_POOL)
    streak = _streaks.record_failure(FP_DB_SERVER)
    if streak >= DB_FAIL_STREAK:
        _with_db(
            db,
            emit_kwargs={
                "fingerprint": FP_DB_SERVER,
                "category": "database",
                "severity": "critical",
                "title": "資料庫連不上",
                "message": (
                    f"連續 {streak} 次無法對資料庫執行探測查詢（非連線池逾時）。"
                    "請查 postgres 行程、主機與磁碟；此情況 ledger 也可能寫失敗，"
                    "請同時看控制平面 console 的 ALERT_SMTP_UNWIRED 紀錄。"
                ),
                "source_type": "database",
                "source_id": "server",
                "metadata": {"detail": outcome.detail, "streak": streak},
            },
        )
    return outcome


# ── 3 & 4. Gateway / agent traffic consecutive failures ──────────────────────

def _traffic_fingerprint(model_type: str, model_id: int) -> str:
    if model_type == "agent":
        return f"agent:dispatch:{model_id}"
    return f"gateway:traffic:{model_id}"


def record_proxy_outcome(
    *,
    model_id: int,
    model_name: str,
    model_type: str,
    display_name: str | None = None,
    success: bool,
    db=None,
) -> int:
    """Record one final proxy attempt (after internal retries exhausted).

    ``success=True`` resets the streak and resolves any open alert.
    Does **not** put endpoint URLs in title/message.
    Returns the current streak after the update.
    """
    fp = _traffic_fingerprint(model_type, model_id)
    label = (display_name or model_name or f"id:{model_id}").strip()

    if success:
        _streaks.record_success(fp)
        # Always resolve — streak is process-local and empty after restart
        # while the ledger row may still be open.
        _with_db(db, resolve_fp=fp)
        return 0

    streak = _streaks.record_failure(fp)
    if model_type == "agent":
        threshold = AGENT_FAIL_STREAK
        if streak >= threshold:
            _with_db(
                db,
                emit_kwargs={
                    "fingerprint": fp,
                    "category": "agent",
                    "severity": "high",
                    "title": f"Agent 派工連續失敗：{label}",
                    "message": (
                        f"Agent「{label}」（{model_name}）連續 {streak} 次派工失敗"
                        f"（已含代理層重試）。請查該 agent 服務與健康狀態。"
                    ),
                    "source_type": "agent",
                    "source_id": model_id,
                    "metadata": {
                        "model_name": model_name,
                        "display_name": label,
                        "streak": streak,
                        "threshold": threshold,
                    },
                },
            )
    else:
        threshold = GATEWAY_FAIL_STREAK
        if streak >= threshold:
            _with_db(
                db,
                emit_kwargs={
                    "fingerprint": fp,
                    "category": "gateway",
                    "severity": "high",
                    "title": f"模型 gateway 連續失敗：{label}",
                    "message": (
                        f"模型「{label}」（{model_name}）連續 {streak} 次呼叫失敗"
                        f"（已含代理層重試）。請查模型 gateway 與該模型健康狀態。"
                    ),
                    "source_type": "model",
                    "source_id": model_id,
                    "metadata": {
                        "model_name": model_name,
                        "display_name": label,
                        "streak": streak,
                        "threshold": threshold,
                    },
                },
            )
    return streak


# ── 5. Disk fullness (data growth, not log runaway) ──────────────────────────

@dataclass(frozen=True, slots=True)
class DiskSample:
    label: str
    path: str
    used_pct: float
    free_bytes: int
    total_bytes: int


def disk_paths_to_check() -> list[tuple[str, str]]:
    """Mounts that grow with real data after log rotation landed."""
    candidates = [
        ("ingestion", str(INGESTION_UPLOAD_ROOT)),
        ("attachments", str(ATTACHMENT_STORAGE_ROOT)),
    ]
    found: list[tuple[str, str]] = []
    for label, raw in candidates:
        path = Path(raw)
        # Resolve to an existing parent so unit tests / bare hosts still work.
        probe = path if path.exists() else path.parent
        if probe.exists():
            found.append((label, str(probe.resolve())))
    if not found:
        found.append(("root", "/"))
    # De-dupe paths (attachments may sit under same fs as ingestion).
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for label, path in found:
        if path in seen:
            continue
        seen.add(path)
        unique.append((label, path))
    return unique


def sample_disk(path: str, label: str) -> DiskSample:
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used_pct = 0.0 if total <= 0 else (1.0 - (free / total)) * 100.0
    return DiskSample(
        label=label,
        path=path,
        used_pct=used_pct,
        free_bytes=free,
        total_bytes=total,
    )


def evaluate_disk(
    samples: list[DiskSample] | None = None,
    *,
    db=None,
) -> list[DiskSample]:
    """Emit/resolve per-mount disk alerts. Paths omitted from message (host
    layout is ops knowledge; label is enough for the one maintainer)."""
    samples = samples if samples is not None else [
        sample_disk(path, label) for label, path in disk_paths_to_check()
    ]
    for sample in samples:
        fp = f"disk:usage:{sample.label}"
        if sample.used_pct < DISK_WARN_PCT:
            _streaks.record_success(fp)
            _with_db(db, resolve_fp=fp)
            continue
        # Disk is sticky — treat each evaluation above threshold as a
        # confirmed reading; streak semantics still apply so a single
        # weird statvfs blip needs confirmation. Reuse DB_FAIL_STREAK (2).
        streak = _streaks.record_failure(fp)
        if streak < DB_FAIL_STREAK:
            continue
        severity = "critical" if sample.used_pct >= DISK_CRIT_PCT else "high"
        free_gib = sample.free_bytes / (1024 ** 3)
        _with_db(
            db,
            emit_kwargs={
                "fingerprint": fp,
                "category": "disk",
                "severity": severity,
                "title": f"磁碟快滿：{sample.label}",
                "message": (
                    f"資料掛載「{sample.label}」使用率約 {sample.used_pct:.1f}%"
                    f"（剩餘約 {free_gib:.1f} GiB）。"
                    "容器日誌已有 50MB×7 輪替；此告警針對知識庫／附件等資料成長。"
                ),
                "source_type": "disk",
                "source_id": sample.label,
                "metadata": {
                    "label": sample.label,
                    "used_pct": round(sample.used_pct, 2),
                    "free_bytes": sample.free_bytes,
                    "total_bytes": sample.total_bytes,
                    # path in gated metadata only — same posture as endpoints
                    "path": sample.path,
                    "streak": streak,
                },
            },
        )
    return samples


# ── Background loop ──────────────────────────────────────────────────────────

ALERT_INTERVAL_SECONDS = 60


def resolve_check_interval() -> int:
    """告警輪詢是固定的內部背景週期，不是治理頁設定。"""
    return ALERT_INTERVAL_SECONDS


async def _alert_detector_loop() -> None:
    # Same reason as the model health loop: the nginx probe must not run
    # on the event loop before lifespan reports startup complete.
    await asyncio.sleep(0)
    while True:
        try:
            evaluate_database()
            evaluate_disk()
            await evaluate_platform_ingress()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("alert detector loop iteration failed")
        # 固定週期避免把背景告警迴圈暴露成治理旋鈕。
        try:
            await asyncio.sleep(resolve_check_interval())
        except asyncio.CancelledError:
            raise


async def start_alert_detectors() -> asyncio.Task:
    """Start background detectors for platform / DB / disk.

    Gateway + agent streaks are event-driven from the proxy path.
    """
    logger.info(
        "告警偵測背景任務已啟動 (platform/db/disk; gateway/agent via proxy; "
        "SMTP=%s)",
        "unwired",
    )
    return asyncio.create_task(_alert_detector_loop())
