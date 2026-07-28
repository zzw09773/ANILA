import json
from datetime import datetime, timezone

from sqlalchemy import case, func, null
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.models.alert import Alert
from app.models.user import User


def _dialect_insert(bind: Engine | Connection):
    """回傳當前方言的 ``insert()`` 建構子(要有 ``on_conflict_do_update``)。

    只有 PostgreSQL(生產)與 SQLite(單元測試)兩種方言在用。刻意**不**在
    未知方言上退回 read-then-write —— 那正是本函式要修掉的競態,靜默退回等於
    把缺陷藏起來。
    """
    name = bind.dialect.name
    if name == "postgresql":
        return postgresql.insert
    if name == "sqlite":
        return sqlite.insert
    raise NotImplementedError(
        f"upsert_alert 需要支援 ON CONFLICT 的方言,收到 {name!r}。"
        "告警去重靠 alerts.fingerprint 的 UNIQUE 約束,不能在 Python 端補。"
    )


def upsert_alert(
    db: Session,
    *,
    fingerprint: str,
    category: str,
    severity: str,
    title: str,
    message: str,
    source_type: str | None = None,
    source_id: str | int | None = None,
    metadata: dict | None = None,
) -> Alert:
    """依 ``fingerprint`` 建立或更新告警,去重交給資料庫。

    W2-6:原本是 read-then-write(``SELECT ... first()`` 後才 INSERT 或改欄位)。
    ``health_checker`` 對每個 model / agent 每 60 秒跑一輪,而多個 CSP 實例
    (內網部署是多 worker)會同時跑同一輪 —— 兩路都 SELECT 不到、兩路都
    INSERT,結果同一個 fingerprint 出現多列,告警中心的「去重」在真正需要它的
    併發情境下失效。``alerts.fingerprint`` 的 UNIQUE 約束由 ``r1_0036``
    建立(在此之前生產庫根本沒有那個約束,ORM 卻宣告了 ``unique=True``)。

    現在改成單一 ``INSERT ... ON CONFLICT (fingerprint) DO UPDATE``:競態由 DB
    的唯一索引序列化,第二路會等第一路 commit 後轉成 UPDATE,結果恆為 1 列。
    """
    now = datetime.now(timezone.utc)
    table = Alert.__table__
    insert_stmt = _dialect_insert(db.get_bind())(table).values(
        fingerprint=fingerprint,
        category=category,
        severity=severity,
        title=title,
        message=message,
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        metadata_json=(
            json.dumps(metadata, ensure_ascii=False) if metadata else None
        ),
        status="open",
        first_seen_at=now,
        last_seen_at=now,
    )

    # ON CONFLICT DO UPDATE 的 SET 子句裡,``alerts.<col>`` 指的是**既有列**、
    # ``excluded.<col>`` 是這次要插入的值(PG 與 SQLite 語意相同)。
    #
    # 語意與舊實作逐條對齊:
    #   * 內容欄(category/severity/title/message/source_*/metadata)一律以新值
    #     覆蓋,``last_seen_at`` 更新為 now。
    #   * ``first_seen_at`` 不動 —— 那是「第一次看到」。
    #   * 只有原本已 ``resolved`` 的告警才重新開啟並清掉 resolved/ack 欄;
    #     ``open`` / ``acknowledged`` 的狀態與已有的 ack 紀錄必須保留,否則
    #     值班人員的簽收會被下一輪健康檢查抹掉。
    was_resolved = table.c.status == "resolved"
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=[table.c.fingerprint],
        set_={
            "category": insert_stmt.excluded.category,
            "severity": insert_stmt.excluded.severity,
            "title": insert_stmt.excluded.title,
            "message": insert_stmt.excluded.message,
            "source_type": insert_stmt.excluded.source_type,
            "source_id": insert_stmt.excluded.source_id,
            "metadata_json": insert_stmt.excluded.metadata_json,
            "last_seen_at": insert_stmt.excluded.last_seen_at,
            "status": case((was_resolved, "open"), else_=table.c.status),
            "resolved_at": case(
                (was_resolved, null()), else_=table.c.resolved_at
            ),
            "acknowledged_at": case(
                (was_resolved, null()), else_=table.c.acknowledged_at
            ),
            "acknowledged_by_user_id": case(
                (was_resolved, null()), else_=table.c.acknowledged_by_user_id
            ),
        },
    )
    db.execute(stmt)

    # ``populate_existing()``:identity map 裡若已有這個 fingerprint 的 Alert
    # (例如同一 session 前一輪讀過),不刷新就會拿到 UPDATE 前的舊值。
    return (
        db.query(Alert)
        .filter(Alert.fingerprint == fingerprint)
        .populate_existing()
        .one()
    )


def acknowledge_alert(db: Session, alert: Alert, actor: User | None = None) -> Alert:
    alert.status = "acknowledged"
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by_user_id = actor.id if actor else None
    return alert


def resolve_alert(db: Session, alert: Alert) -> Alert:
    alert.status = "resolved"
    alert.resolved_at = datetime.now(timezone.utc)
    alert.last_seen_at = alert.resolved_at
    return alert


def resolve_alert_by_fingerprint(db: Session, fingerprint: str) -> Alert | None:
    alert = (
        db.query(Alert)
        .filter(Alert.fingerprint == fingerprint, Alert.status != "resolved")
        .first()
    )
    if not alert:
        return None
    return resolve_alert(db, alert)


def summarize_alerts(db: Session) -> dict:
    rows = (
        db.query(Alert.status, func.count(Alert.id))
        .group_by(Alert.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    high_count = (
        db.query(func.count(Alert.id))
        .filter(Alert.status != "resolved", Alert.severity.in_(["high", "critical"]))
        .scalar()
    )
    return {
        "open_count": counts.get("open", 0),
        "acknowledged_count": counts.get("acknowledged", 0),
        "resolved_count": counts.get("resolved", 0),
        "high_count": high_count or 0,
    }


def parse_alert_metadata(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
