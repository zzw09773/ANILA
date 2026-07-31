"""P2.7 稽核帳:防竄改(tamper-**evidence**)的單一出處。

## 為什麼是「查得出來」而不是「改不了」

威脅模型(SYSTEM-MAP §8)裡的對手是**這台機器上有 admin 權限的人**。
氣隙、單機、一人維運 —— 那個人同時是 host root、是 superuser、是備份持有者。
在這個拓撲下「讓他改不了」是做不到的;宣稱做得到就是賣假安心感。
所以本模組的目標只有一個:**他改了會被抓到,而且抓得出是哪一天。**

兩層:

1. **權限分離**(migration ``r1_0027``)。受保護的稽核表 owner 收回給
   migration role,runtime role ``csp_app`` 只剩 ``SELECT`` / ``INSERT``,
   再加 BEFORE UPDATE / BEFORE DELETE 觸發器。拿到 runtime 憑證的人
   從此改不了、刪不掉、也拆不掉觸發器(非 owner)。
   對 superuser 無效 —— 這一點誠實寫在這裡,不假裝。
2. **日級雜湊鏈 + 資料庫外錨定**(本模組)。每天把前一日的稽核列摘要成
   一個 SHA-256,再與前一日鏈頭串成鏈;鏈頭嵌進稽核匯出檔的檔頭。
   匯出檔一旦交給稽核單位/長官,就是**這台機器上的人碰不到的東西**。
   之後任何對已錨定區間的改寫,重算 digest 都會對不上。

## 為什麼是「日級」不是「每列」

未錨定之前,列級鏈與日級鏈的證據力相同(能改列的人必然連鏈一起重算),
粒度只影響「最後一個已分發錨點之後」那個窗口。日級把窗口留在 24 小時,
換到的是**熱路徑零成本**:``log_audit_event`` 一行都不用改,沒有
序列化點、沒有額外鎖、沒有 per-row hash。

## 操作者每月要做什麼

**沒有。** 檢查點是 csp 內的背景工作自動產生的;鏈頭自動印進本來就要出的
稽核匯出檔。沒有每週儀式、沒有第二台主機、沒有 HSM。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 受保護集合 —— 單一出處。migration r1_0027 把同一組表的 owner 收走,
# startup_security 的開機自檢照這份清單驗權限,pytest 也照這份驗。
#
# ⚠ 這份清單與 r1_0027 的清單「必須一致但不共用」:migration 是歷史事實,
#   凍結在當時;這份是 runtime 的現況。加新的稽核表要同時改兩邊,並且新增
#   一支新 migration 去鎖它(不是回頭改 r1_0027)。
#   `tests/test_audit_ledger_pg.py::test_protected_set_matches_database`
#   會在 PostgreSQL 上驗證兩者沒有漂移。
# ─────────────────────────────────────────────────────────────────────────────

#: 進入日雜湊摘要的事件表(依此順序,順序是摘要的一部分)。
AUDIT_EVENT_TABLES: tuple[str, ...] = (
    "audit_logs",
    "policy_decisions",
    "classification_events",
)

#: 全部受保護的表 = 事件表 + 檢查點表本身。
AUDIT_LEDGER_TABLES: tuple[str, ...] = AUDIT_EVENT_TABLES + ("audit_checkpoints",)

#: 保留期(SYSTEM-MAP §8「留半年」)。DELETE 觸發器只放行比這更舊的列。
AUDIT_RETENTION_DAYS = 180

#: 摘要演算法版本。改欄位集合或正規化規則就要 +1,舊檢查點照它自己記的
#: 版本重算,不會被追溯性地弄壞。
DIGEST_VERSION = 1

GENESIS_HASH = "0" * 64

#: v1 摘要涵蓋的欄位,順序固定。刻意寫死而不是讀 model:日後有人加欄位
#: 不會讓歷史檢查點無聲失效(要涵蓋新欄位就升 DIGEST_VERSION)。
_DIGEST_COLUMNS_V1: dict[str, tuple[str, ...]] = {
    "audit_logs": (
        "id", "actor_user_id", "actor_username", "action", "resource_type",
        "resource_id", "status", "detail", "ip_address", "metadata_json",
        "created_at",
    ),
    "policy_decisions": (
        "id", "task_id", "actor_type", "actor_id", "action", "resource_type",
        "resource_id", "decision", "reason", "matched_policy_ids",
        "policy_version", "metadata_json", "classification_level",
        "created_at",
    ),
    "classification_events": (
        "id", "resource_type", "resource_id", "previous_level", "new_level",
        "reason", "actor_user_id", "inherited_from_resource_type",
        "inherited_from_resource_id", "trace_id", "created_at",
    ),
}


def digest_columns(version: int) -> dict[str, tuple[str, ...]]:
    if version == 1:
        return _DIGEST_COLUMNS_V1
    raise ValueError(f"未知的摘要版本 {version};此版本的 csp 無法驗證該檢查點")


# ─────────────────────────────────────────────────────────────────────────────
# 正規化 —— 同一列在任何機器、任何 session timezone 下都要算出同一個字串。
# ─────────────────────────────────────────────────────────────────────────────

def _canon(value: object) -> object:
    """把一個欄位值變成穩定的 JSON-safe 表示。

    時間一律轉成 UTC ISO —— psycopg2 回傳的 tz-aware datetime 帶的是
    session 的 TimeZone GUC,直接 str() 會讓同一列在不同連線算出不同雜湊。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row_line(table: str, columns: tuple[str, ...], row: tuple) -> str:
    payload = [table] + [_canon(v) for v in row]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def compute_day_digest(
    db: Session, day: date, *, version: int = DIGEST_VERSION,
) -> tuple[str, dict[str, int]]:
    """重算某一 UTC 日的摘要。回傳 ``(digest_hex, per-table row counts)``。

    純讀取。檢查點產生與事後驗證走的是**同一支函式**,所以「產生時算得出、
    驗證時算不出」這種假綠燈不會發生。
    """
    columns_by_table = digest_columns(version)
    start, end = _day_bounds(day)
    hasher = hashlib.sha256()
    counts: dict[str, int] = {}
    for table in AUDIT_EVENT_TABLES:
        columns = columns_by_table[table]
        col_sql = ", ".join(columns)
        rows = db.execute(
            text(
                f"SELECT {col_sql} FROM {table} "
                "WHERE created_at >= :start AND created_at < :end ORDER BY id"
            ),
            {"start": start, "end": end},
        ).all()
        counts[table] = len(rows)
        for row in rows:
            hasher.update(_row_line(table, columns, tuple(row)).encode("utf-8"))
            hasher.update(b"\n")
    return hasher.hexdigest(), counts


def link(prev_hash: str, day: date, day_digest: str) -> str:
    """鏈結函式:``H(prev_hash | day | day_digest)``。"""
    material = f"{prev_hash}|{day.isoformat()}|{day_digest}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 檢查點產生
# ─────────────────────────────────────────────────────────────────────────────

def _latest_checkpoint(db: Session, *, today: date):
    """最後一個**已經過完的**日子的檢查點。

    ⚠ 這裡刻意寫 ``day < :today`` 而不是單純 ``MAX(day)``。

    無條件相信 ``MAX(day)`` 會製造一個安靜的殺手:只要帳本裡出現一列日期在
    未來的檢查點,游標就會跳到今天之後,``while cursor < today`` 永遠為假,
    **封存從此停擺、而且一行日誌都不會寫**(``if sealed:`` 不成立),匯出檔
    照樣印著一個再也不前進的鏈頭。負責抓弊的東西自己死了還不出聲,
    正是這個專案花了一週在消滅的失敗形狀。

    未來日期的檢查點現在由 ``_assert_no_future_checkpoints`` 大聲報出來,
    而寫入權限本身也已經收掉(見 ``_ledger_write_session``)。
    """
    return db.execute(
        text(
            "SELECT id, day, digest_version, day_digest, prev_hash, chain_head "
            "FROM audit_checkpoints WHERE day < :today "
            "ORDER BY day DESC LIMIT 1"
        ),
        {"today": today},
    ).first()


def _future_checkpoint_days(db: Session, *, today: date) -> list[date]:
    """日期在今天或未來的檢查點 —— 正常運作下不可能存在。"""
    return [
        row.day
        for row in db.execute(
            text(
                "SELECT day FROM audit_checkpoints WHERE day >= :today ORDER BY day"
            ),
            {"today": today},
        ).all()
    ]


def _warn_about_future_checkpoints(db: Session, *, today: date) -> None:
    """看到未來日期的檢查點就大聲喊,不要默默繞過去。

    封存本身已經不會被它卡住了(``_latest_checkpoint`` 只看過完的日子),
    但它的存在本身就是有人動過帳本的證據 —— 只有 owner/superuser 寫得進去。
    """
    future = _future_checkpoint_days(db, today=today)
    if future:
        logger.error(
            "[audit-ledger] 帳本裡有 %d 個日期在今天或未來的檢查點 (%s) —— "
            "正常運作寫不出這種列,請立刻跑 verify_audit_chain 並追查來源。",
            len(future), ", ".join(d.isoformat() for d in future),
        )


def _earliest_event_day(db: Session) -> date | None:
    earliest: datetime | None = None
    for table in AUDIT_EVENT_TABLES:
        got = db.execute(text(f"SELECT MIN(created_at) FROM {table}")).scalar()
        if got is None:
            continue
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        got = got.astimezone(timezone.utc)
        if earliest is None or got < earliest:
            earliest = got
    return earliest.date() if earliest else None


def seal_due_checkpoints(db: Session, *, today: date | None = None) -> list[date]:
    """把所有「已經過完、還沒封存」的日子封成檢查點。回傳新封的日期。

    只 INSERT,不 UPDATE。這支函式跑在 **migration 身分** 上(見
    ``_ledger_write_session``)—— runtime role ``csp_app`` 對
    ``audit_checkpoints`` 只有 SELECT,連 INSERT 都沒有,所以拿到 runtime
    憑證的人沒有任何辦法往帳本裡塞檢查點。
    """
    today = today or datetime.now(timezone.utc).date()
    _warn_about_future_checkpoints(db, today=today)
    latest = _latest_checkpoint(db, today=today)
    if latest is not None:
        cursor = latest.day + timedelta(days=1)
        prev_hash = latest.chain_head
    else:
        first = _earliest_event_day(db)
        if first is None:
            return []
        cursor = first
        prev_hash = GENESIS_HASH

    sealed: list[date] = []
    while cursor < today:
        digest, counts = compute_day_digest(db, cursor)
        head = link(prev_hash, cursor, digest)
        db.execute(
            text(
                "INSERT INTO audit_checkpoints "
                "(day, digest_version, day_digest, prev_hash, chain_head, "
                " row_counts, created_at) "
                "VALUES (:day, :ver, :digest, :prev, :head, :counts, :now)"
            ),
            {
                "day": cursor,
                "ver": DIGEST_VERSION,
                "digest": digest,
                "prev": prev_hash,
                "head": head,
                "counts": json.dumps(counts, sort_keys=True),
                "now": datetime.now(timezone.utc),
            },
        )
        sealed.append(cursor)
        prev_hash = head
        cursor += timedelta(days=1)

    if sealed:
        db.commit()
        logger.info(
            "[audit-ledger] 已封存 %d 個檢查點 (%s..%s);鏈頭=%s",
            len(sealed), sealed[0], sealed[-1], prev_hash,
        )
    return sealed


# ─────────────────────────────────────────────────────────────────────────────
# 錨點 —— 匯出檔頁首要印的那幾行
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Anchor:
    chain_head: str
    first_day: date | None
    last_day: date | None
    checkpoint_count: int
    row_counts: dict[str, int]

    @property
    def anchored(self) -> bool:
        return self.checkpoint_count > 0


def current_anchor(db: Session) -> Anchor:
    """匯出檔要嵌的鏈頭與涵蓋範圍。"""
    row = db.execute(
        text(
            "SELECT COUNT(*) AS n, MIN(day) AS first_day, MAX(day) AS last_day "
            "FROM audit_checkpoints"
        )
    ).first()
    count = int(row.n or 0)
    if not count:
        return Anchor(GENESIS_HASH, None, None, 0, {})
    latest = _latest_checkpoint(db)
    totals: dict[str, int] = {t: 0 for t in AUDIT_EVENT_TABLES}
    for (counts,) in db.execute(text("SELECT row_counts FROM audit_checkpoints")):
        parsed = json.loads(counts) if isinstance(counts, str) else (counts or {})
        for table, n in parsed.items():
            totals[table] = totals.get(table, 0) + int(n)
    return Anchor(
        chain_head=latest.chain_head,
        first_day=row.first_day,
        last_day=row.last_day,
        checkpoint_count=count,
        row_counts=totals,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 驗證
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VerificationResult:
    ok: bool
    checkpoints: int = 0
    first_day: date | None = None
    last_day: date | None = None
    chain_head: str = GENESIS_HASH
    tampered_days: list[date] = field(default_factory=list)
    #: 已達保留期、整日被清空的日子 —— 對得上「合法清除」的形狀,不算竄改。
    purged_days: list[date] = field(default_factory=list)
    #: 日期在今天或未來的檢查點 —— 封存停擺的成因,也是被動過帳本的證據。
    future_days: list[date] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    unanchored_rows: dict[str, int] = field(default_factory=dict)
    expected_head: str | None = None
    head_matches: bool | None = None


def verify_chain(db: Session, *, expected_head: str | None = None) -> VerificationResult:
    """重算整條鏈,回報「有沒有被動過」以及**是哪一天**。

    三種可偵測的動作:

    * 改寫/刪除已封存日的稽核列 → 該日 ``day_digest`` 對不上。
    * 事後補插進已封存日 → 同上(日摘要涵蓋整天所有列)。
    * 動檢查點列本身(改鏈頭、刪掉某一天) → 鏈結斷點 / 日期缺口。

    ``expected_head`` 給的是任何一份已發出的稽核匯出檔上印的鏈頭 —— 那份
    副本在維運者手外,所以這是唯一能對付 superuser 的比對基準。
    """
    rows = db.execute(
        text(
            "SELECT id, day, digest_version, day_digest, prev_hash, chain_head, "
            "row_counts FROM audit_checkpoints ORDER BY day"
        )
    ).all()
    result = VerificationResult(ok=True, checkpoints=len(rows))
    today = datetime.now(timezone.utc).date()
    if not rows:
        result.problems.append("沒有任何檢查點 —— 稽核帳尚未錨定,無法判斷是否被動過。")
        result.ok = False
    else:
        result.first_day = rows[0].day
        result.last_day = rows[-1].day

    # 未來日期的檢查點:正常運作寫不出來(runtime role 對 audit_checkpoints
    # 只有 SELECT),而且它會讓封存游標永遠跳過今天。單獨列為問題,不要被
    # 「日期不連續」蓋掉 —— 那兩件事的處置完全不同。
    for day in _future_checkpoint_days(db, today=today):
        result.future_days.append(day)
        result.problems.append(
            f"檢查點 {day} 的日期在今天或未來 —— 只有 owner/superuser 寫得進去,"
            "而且會讓每日封存從此停擺"
        )
        result.ok = False

    prev_hash = GENESIS_HASH
    prev_day: date | None = None
    for row in rows:
        if prev_day is not None and row.day != prev_day + timedelta(days=1):
            result.problems.append(
                f"檢查點日期不連續:{prev_day} 之後直接跳到 {row.day}"
                "(中間的檢查點被刪掉了)"
            )
            result.ok = False
        if row.prev_hash != prev_hash:
            result.problems.append(
                f"{row.day} 的 prev_hash 接不回前一個檢查點的鏈頭 —— 鏈被改過"
            )
            result.ok = False
        try:
            recomputed, counts = compute_day_digest(
                db, row.day, version=int(row.digest_version),
            )
        except ValueError as exc:
            result.problems.append(f"{row.day}: {exc}")
            result.ok = False
            prev_hash = row.chain_head
            prev_day = row.day
            continue
        if recomputed != row.day_digest:
            sealed_counts = row.row_counts
            if isinstance(sealed_counts, str):
                sealed_counts = json.loads(sealed_counts)
            sealed_total = sum(int(v) for v in (sealed_counts or {}).values())
            expired = (today - row.day).days > AUDIT_RETENTION_DAYS
            if expired and sealed_total and not sum(counts.values()):
                # 整天被清空、而且早就過了保留期 —— 這是合法清除的形狀
                # (SYSTEM-MAP §8「留半年」),不是竄改。誠實分開報,
                # 否則第一次清理之後驗證器就永遠紅燈,等於沒有控制。
                result.purged_days.append(row.day)
            else:
                result.tampered_days.append(row.day)
                result.problems.append(
                    f"{row.day} 的稽核列與當日封存的摘要不符 —— 該日資料被改過、"
                    "被刪過,或被事後補插"
                )
                result.ok = False
        expected_link = link(row.prev_hash, row.day, row.day_digest)
        if expected_link != row.chain_head:
            result.problems.append(f"{row.day} 的 chain_head 與其自身欄位不自洽")
            result.ok = False
        prev_hash = row.chain_head
        prev_day = row.day

    result.chain_head = prev_hash

    # 最後一個檢查點之後的列還沒被錨定 —— 這是設計上就存在的 ≤24h 盲區,
    # 誠實報出來,不假裝涵蓋。
    if rows:
        after = datetime(
            result.last_day.year, result.last_day.month, result.last_day.day,
            tzinfo=timezone.utc,
        ) + timedelta(days=1)
        for table in AUDIT_EVENT_TABLES:
            n = db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE created_at >= :after"),
                {"after": after},
            ).scalar()
            if n:
                result.unanchored_rows[table] = int(n)

    if expected_head:
        result.expected_head = expected_head
        result.head_matches = any(
            r.chain_head == expected_head for r in rows
        )
        if not result.head_matches:
            result.problems.append(
                f"已發出報告上的鏈頭 {expected_head} 不等於本機任何一個檢查點的鏈頭 —— "
                "資料庫被換過、被回捲,或該區間被改寫"
            )
            result.ok = False

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 背景工作 —— 每天 00:05 UTC 封存前一日
# ─────────────────────────────────────────────────────────────────────────────

#: 每天封存的時刻(UTC 午夜後幾秒)。留一點餘裕給 23:59:59 那批寫入落地。
_SEAL_AFTER_MIDNIGHT_SECONDS = 5 * 60


def _seconds_until_next_seal(now: datetime) -> float:
    target = datetime(
        now.year, now.month, now.day, tzinfo=timezone.utc,
    ) + timedelta(seconds=_SEAL_AFTER_MIDNIGHT_SECONDS)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


@contextmanager
def _ledger_write_session() -> Iterator[Session]:
    """開一個有資格寫檢查點的 session —— 也就是 **migration 身分**。

    為什麼不用 runtime session:``audit_checkpoints`` 只給 ``csp_app``
    ``SELECT``。若它有 ``INSERT``,任何拿到 runtime 憑證的人都能塞一列日期在
    未來的檢查點(摘要沒有祕密,他有 SELECT 就算得出自洽的鏈頭),封存從此
    永久停擺而且不出聲。與其事後偵測,不如直接把那個能力拿掉。

    engine 用完就 dispose:這是一天一次的工作,常駐的特權連線池是白放的風險。
    ``MIGRATION_DATABASE_URL`` 沒設時(SQLite 單元測試、單一 role 的本機)
    退回 runtime session,行為與從前相同。
    """
    from app.database import SessionLocal, engine

    migration_url = os.environ.get("MIGRATION_DATABASE_URL", "").strip()
    if not migration_url or engine.dialect.name != "postgresql":
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
        return

    privileged = create_engine(
        migration_url, pool_pre_ping=True, poolclass=NullPool
    )
    try:
        db = sessionmaker(bind=privileged)()
        try:
            yield db
        finally:
            db.close()
    finally:
        privileged.dispose()


def seal_once() -> list[date]:
    """跑一輪封存(自己開 session)。背景迴圈與 CLI 共用。

    只在 PostgreSQL 上動作。稽核帳的防竄改姿態(role 分離、觸發器)本來就
    只有 PostgreSQL 有;在 SQLite(單元測試)上封存出來的檢查點沒有任何
    保護價值,寧可什麼都不做,也不要留一個看起來有在保護的假象。
    """
    from app.database import engine

    if engine.dialect.name != "postgresql":
        return []
    with _ledger_write_session() as db:
        return seal_due_checkpoints(db)


async def _checkpoint_loop() -> None:
    import asyncio

    while True:
        try:
            await asyncio.to_thread(seal_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            # fail-soft:封存失敗不能拖垮平台。少一個檢查點會在下一輪補上,
            # 而且 verify 會把日期缺口報出來,不會靜默成功。
            logger.exception("[audit-ledger] 檢查點封存失敗,下一輪重試")
        delay = _seconds_until_next_seal(datetime.now(timezone.utc))
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise


async def start_audit_checkpointer():
    """啟動每日檢查點背景工作。

    熱路徑成本 = 0:``log_audit_event`` 完全不知道這件事存在。
    """
    import asyncio

    logger.info(
        "[audit-ledger] 每日檢查點背景任務已啟動(UTC 00:0%d 封存前一日)",
        _SEAL_AFTER_MIDNIGHT_SECONDS // 60,
    )
    return asyncio.create_task(_checkpoint_loop())


def format_verification(result: VerificationResult) -> str:
    lines: list[str] = []
    if result.ok:
        lines.append("結果:未偵測到竄改。")
    else:
        lines.append("結果:偵測到問題 —— 稽核帳與封存的摘要不一致。")
    lines.append(
        f"檢查點:{result.checkpoints} 個"
        + (
            f"({result.first_day} .. {result.last_day})"
            if result.first_day else ""
        )
    )
    lines.append(f"目前鏈頭:{result.chain_head}")
    if result.expected_head:
        lines.append(
            f"與報告鏈頭比對:{'相符' if result.head_matches else '不符'}"
            f"({result.expected_head})"
        )
    if result.tampered_days:
        lines.append(
            "被動過的日期:" + ", ".join(d.isoformat() for d in result.tampered_days)
        )
    if result.purged_days:
        lines.append(
            "已達保留期被清除(非竄改跡象):"
            + ", ".join(d.isoformat() for d in result.purged_days)
        )
    if result.future_days:
        lines.append(
            "⚠ 未來日期的檢查點(封存會停擺,且只有 owner/superuser 寫得進去):"
            + ", ".join(d.isoformat() for d in result.future_days)
        )
    for problem in result.problems:
        lines.append(f"  - {problem}")
    if result.unanchored_rows:
        lines.append(
            "尚未錨定(最後一個檢查點之後,設計上的 ≤24h 盲區):"
            + ", ".join(f"{t}={n}" for t, n in sorted(result.unanchored_rows.items()))
        )
    return "\n".join(lines)
