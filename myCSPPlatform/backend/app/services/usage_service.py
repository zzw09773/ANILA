import csv
import io
from datetime import datetime, timezone
from sqlalchemy import func, literal_column, text
from sqlalchemy.orm import Session
from app.models.department import Department
from app.models.token_usage import TokenUsage
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.utils.time_helpers import get_time_range


def _get_model_ids_by_type(db: Session, model_type: str) -> list[int]:
    return [
        m.id
        for m in db.query(ModelRegistry.id)
        .filter(ModelRegistry.model_type == model_type)
        .all()
    ]


def _apply_usage_filters(
    query,
    db: Session,
    *,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
):
    if model_id:
        query = query.filter(TokenUsage.model_id == model_id)
    if user_id:
        query = query.filter(TokenUsage.user_id == user_id)
    if department_id is not None:
        query = query.filter(TokenUsage.department_id == department_id)
    if model_type:
        model_ids = _get_model_ids_by_type(db, model_type)
        query = query.filter(TokenUsage.model_id.in_(model_ids))
    return query


def get_usage_summary(
    db: Session,
    range_key: str = "24h",
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
) -> dict:
    """Get aggregate usage summary for the selected time range."""
    start_time, _ = get_time_range(range_key)

    query = db.query(
        func.count(TokenUsage.id).label("total_requests"),
        func.coalesce(func.sum(TokenUsage.prompt_tokens), 0).label(
            "total_prompt_tokens"
        ),
        func.coalesce(func.sum(TokenUsage.completion_tokens), 0).label(
            "total_completion_tokens"
        ),
        func.coalesce(func.sum(TokenUsage.total_tokens), 0).label("total_tokens"),
    ).filter(TokenUsage.request_timestamp >= start_time)
    query = _apply_usage_filters(
        query,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )
    result = query.first()

    active_models_q = db.query(
        func.count(func.distinct(TokenUsage.model_id))
    ).filter(TokenUsage.request_timestamp >= start_time)
    active_models_q = _apply_usage_filters(
        active_models_q,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )
    active_models = active_models_q.scalar() or 0

    # COUNT(DISTINCT) skips NULLs in SQL, so this counts only named API
    # keys — JWT-attributed traffic has api_key_id IS NULL and is surfaced
    # separately below as "web_ui_requests" so dashboards can split
    # SDK-originated vs SPA-originated traffic.
    active_keys_q = db.query(
        func.count(func.distinct(TokenUsage.api_key_id))
    ).filter(TokenUsage.request_timestamp >= start_time)
    active_keys_q = _apply_usage_filters(
        active_keys_q,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )
    active_keys = active_keys_q.scalar() or 0

    web_ui_req_q = db.query(func.count(TokenUsage.id)).filter(
        TokenUsage.request_timestamp >= start_time,
        TokenUsage.api_key_id.is_(None),
    )
    web_ui_req_q = _apply_usage_filters(
        web_ui_req_q,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )
    web_ui_requests = web_ui_req_q.scalar() or 0

    return {
        "total_requests": result.total_requests or 0,
        "total_prompt_tokens": result.total_prompt_tokens or 0,
        "total_completion_tokens": result.total_completion_tokens or 0,
        "total_tokens": result.total_tokens or 0,
        "active_models": int(active_models),
        "active_api_keys": int(active_keys),
        "web_ui_requests": int(web_ui_requests),
    }


def get_chart_data(
    db: Session,
    range_key: str,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    group_by: str = "total",
) -> dict:
    """Get time-series data for line charts."""
    start_time, bucket_seconds = get_time_range(range_key)
    start_ts = int(start_time.timestamp())
    now_ts = int(datetime.now(timezone.utc).timestamp())

    all_buckets = []
    ts = (start_ts // bucket_seconds) * bucket_seconds
    while ts <= now_ts:
        all_buckets.append(ts)
        ts += bucket_seconds

    # literal_column (not text) so SQLAlchemy lets us call .label() on it.
    bucket_expr = literal_column(
        f"(CAST(EXTRACT(EPOCH FROM request_timestamp) AS INTEGER) / {bucket_seconds}) * {bucket_seconds}"
    )

    if group_by == "model":
        group_col = TokenUsage.model_id
    elif group_by == "user":
        group_col = TokenUsage.user_id
    elif group_by == "department":
        group_col = TokenUsage.department_id
    else:
        group_col = None

    query = db.query(
        bucket_expr.label("bucket_ts"),
        func.sum(TokenUsage.total_tokens).label("tokens"),
    )
    if group_col is not None:
        query = query.add_columns(group_col.label("group_id"))

    query = query.filter(TokenUsage.request_timestamp >= start_time)
    query = _apply_usage_filters(
        query,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )

    query = query.group_by("bucket_ts")
    if group_col is not None:
        query = query.group_by(group_col)

    rows = query.order_by("bucket_ts").all()

    if group_by == "total" or group_col is None:
        data_map = {b: 0 for b in all_buckets}
        for row in rows:
            bucket = int(row.bucket_ts)
            if bucket in data_map:
                data_map[bucket] = int(row.tokens)
        return {
            "timestamps": all_buckets,
            "series": [{"name": "總計", "data": [data_map[b] for b in all_buckets]}],
        }

    if group_by == "model":
        name_map = {m.id: m.display_name for m in db.query(ModelRegistry).all()}
    elif group_by == "user":
        name_map = {u.id: u.username for u in db.query(User).all()}
    else:
        name_map = {
            d.id: d.name
            for d in db.query(Department).order_by(Department.name).all()
        }
        name_map[None] = "未指定部門"

    group_data: dict[object, dict[int, int]] = {}
    for row in rows:
        gid = row.group_id
        bucket = int(row.bucket_ts)
        if gid not in group_data:
            group_data[gid] = {b: 0 for b in all_buckets}
        if bucket in group_data[gid]:
            group_data[gid][bucket] = int(row.tokens)

    series = []
    for gid, data_map in group_data.items():
        series.append(
            {
                "name": name_map.get(gid, "未指定部門" if gid is None else str(gid)),
                "data": [data_map[b] for b in all_buckets],
            }
        )

    return {"timestamps": all_buckets, "series": series}


def get_top_models(
    db: Session,
    limit: int = 10,
    model_type: str | None = None,
    user_id: int | None = None,
    department_id: int | None = None,
) -> list[dict]:
    start_time, _ = get_time_range("30d")
    query = db.query(
        TokenUsage.model_id,
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.count(TokenUsage.id).label("total_requests"),
    ).filter(TokenUsage.request_timestamp >= start_time)
    query = _apply_usage_filters(
        query,
        db,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )

    results = (
        query.group_by(TokenUsage.model_id)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .limit(limit)
        .all()
    )

    model_map = {m.id: m for m in db.query(ModelRegistry).all()}
    return [
        {
            "model_id": r.model_id,
            "model_name": model_map.get(r.model_id, ModelRegistry()).display_name
            or "未知",
            "model_type": model_map.get(r.model_id, ModelRegistry()).model_type or "未知",
            "total_tokens": int(r.total_tokens),
            "total_requests": r.total_requests,
        }
        for r in results
    ]


def get_top_users(
    db: Session,
    limit: int = 10,
    model_type: str | None = None,
    department_id: int | None = None,
) -> list[dict]:
    start_time, _ = get_time_range("30d")
    query = db.query(
        TokenUsage.user_id,
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.count(TokenUsage.id).label("total_requests"),
    ).filter(TokenUsage.request_timestamp >= start_time)
    query = _apply_usage_filters(
        query,
        db,
        model_type=model_type,
        department_id=department_id,
    )

    results = (
        query.group_by(TokenUsage.user_id)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .limit(limit)
        .all()
    )

    user_map = {u.id: u.username for u in db.query(User).all()}
    return [
        {
            "user_id": r.user_id,
            "username": user_map.get(r.user_id, "未知"),
            "total_tokens": int(r.total_tokens),
            "total_requests": r.total_requests,
        }
        for r in results
    ]


def get_top_departments(
    db: Session,
    limit: int = 10,
    model_type: str | None = None,
    department_id: int | None = None,
) -> list[dict]:
    start_time, _ = get_time_range("30d")
    query = db.query(
        TokenUsage.department_id,
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.count(TokenUsage.id).label("total_requests"),
    ).filter(TokenUsage.request_timestamp >= start_time)
    query = _apply_usage_filters(
        query,
        db,
        model_type=model_type,
        department_id=department_id,
    )

    results = (
        query.group_by(TokenUsage.department_id)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .limit(limit)
        .all()
    )

    department_map = {d.id: d.name for d in db.query(Department).all()}
    return [
        {
            "department_id": r.department_id,
            "department_name": department_map.get(r.department_id, "未指定部門"),
            "total_tokens": int(r.total_tokens),
            "total_requests": r.total_requests,
        }
        for r in results
    ]


def export_usage_csv(
    db: Session,
    range_key: str,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
) -> str:
    """Export usage data as CSV string."""
    start_time, _ = get_time_range(range_key)

    query = (
        db.query(
            TokenUsage.request_timestamp,
            User.username,
            Department.name.label("department_name"),
            ModelRegistry.display_name.label("model_name"),
            ModelRegistry.model_type,
            TokenUsage.prompt_tokens,
            TokenUsage.completion_tokens,
            TokenUsage.total_tokens,
            TokenUsage.request_duration_ms,
        )
        .join(User, TokenUsage.user_id == User.id)
        .outerjoin(Department, TokenUsage.department_id == Department.id)
        .join(ModelRegistry, TokenUsage.model_id == ModelRegistry.id)
        .filter(TokenUsage.request_timestamp >= start_time)
    )
    query = _apply_usage_filters(
        query,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
    )

    rows = query.order_by(TokenUsage.request_timestamp.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "時間",
            "使用者",
            "部門",
            "模型",
            "類型",
            "輸入 Tokens",
            "輸出 Tokens",
            "總計 Tokens",
            "延遲 (ms)",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.request_timestamp.isoformat() if row.request_timestamp else "",
                row.username,
                row.department_name or "",
                row.model_name,
                row.model_type,
                row.prompt_tokens,
                row.completion_tokens,
                row.total_tokens,
                row.request_duration_ms or "",
            ]
        )

    return output.getvalue()


# ---------------------------------------------------------------------------
# Sprint 8 X / Phase G — caller attribution rollups.
#
# All four functions filter on ``token_usage.request_timestamp`` so a
# ``days`` parameter governs the lookback window. They return plain
# dicts so the API layer can pydantic-shape them; tests can assert
# against dicts without ORM gymnastics.
# ---------------------------------------------------------------------------


def get_top_agents(
    db: Session,
    *,
    days: int = 30,
    limit: int = 10,
) -> list[dict]:
    """Top-N agents by token consumption (CSP-forwarded + agent-callback)."""
    from datetime import timedelta as _td

    from app.models.agent import Agent

    start_time = datetime.now(timezone.utc) - _td(days=days)
    rows = (
        db.query(
            TokenUsage.caller_agent_id.label("agent_id"),
            func.sum(TokenUsage.total_tokens).label("total_tokens"),
            func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
            func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
            func.count(TokenUsage.id).label("total_requests"),
        )
        .filter(
            TokenUsage.request_timestamp >= start_time,
            TokenUsage.caller_agent_id.isnot(None),
        )
        .group_by(TokenUsage.caller_agent_id)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return []
    agents = {a.id: a for a in db.query(Agent).all()}
    out: list[dict] = []
    for r in rows:
        agent = agents.get(r.agent_id)
        out.append(
            {
                "agent_id": r.agent_id,
                "agent_name": agent.name if agent else "(已刪除)",
                "base_model_id": agent.base_model_id if agent else None,
                "total_tokens": int(r.total_tokens or 0),
                "prompt_tokens": int(r.prompt_tokens or 0),
                "completion_tokens": int(r.completion_tokens or 0),
                "total_requests": int(r.total_requests or 0),
            }
        )
    return out


def get_usage_by_base_model(db: Session, *, days: int = 30) -> list[dict]:
    """Group token usage by ``agents.base_model_id``.

    Joins ``token_usage`` → ``agents`` (on ``caller_agent_id``) →
    ``model_registry`` (on ``base_model_id``). Only attributable rows
    appear; orphan rows (no caller_agent_id) are excluded — they
    already aggregate under ``model_id`` directly via
    ``get_top_models``.
    """
    from datetime import timedelta as _td

    from app.models.agent import Agent

    start_time = datetime.now(timezone.utc) - _td(days=days)
    rows = (
        db.query(
            Agent.base_model_id.label("base_model_id"),
            func.sum(TokenUsage.total_tokens).label("total_tokens"),
            func.count(TokenUsage.id).label("total_requests"),
        )
        .join(Agent, Agent.id == TokenUsage.caller_agent_id)
        .filter(
            TokenUsage.request_timestamp >= start_time,
            Agent.base_model_id.isnot(None),
        )
        .group_by(Agent.base_model_id)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .all()
    )
    if not rows:
        return []
    models = {m.id: m for m in db.query(ModelRegistry).all()}
    return [
        {
            "base_model_id": r.base_model_id,
            "base_model_name": (
                models.get(r.base_model_id).display_name
                if models.get(r.base_model_id)
                else "(已刪除)"
            ),
            "total_tokens": int(r.total_tokens or 0),
            "total_requests": int(r.total_requests or 0),
        }
        for r in rows
    ]


def get_agent_usage(
    db: Session, *, agent_id: int, days: int = 30
) -> dict:
    """Per-agent rollup: total tokens / requests / time-series for one agent."""
    from datetime import timedelta as _td

    start_time = datetime.now(timezone.utc) - _td(days=days)
    rows = (
        db.query(
            func.date_trunc("day", TokenUsage.request_timestamp).label("bucket"),
            func.sum(TokenUsage.total_tokens).label("total_tokens"),
            func.count(TokenUsage.id).label("total_requests"),
            func.avg(TokenUsage.request_duration_ms).label("avg_duration_ms"),
        )
        .filter(
            TokenUsage.request_timestamp >= start_time,
            TokenUsage.caller_agent_id == agent_id,
        )
        .group_by("bucket")
        .order_by("bucket")
        .all()
    )

    series = [
        {
            "timestamp": r.bucket.isoformat() if r.bucket else None,
            "total_tokens": int(r.total_tokens or 0),
            "total_requests": int(r.total_requests or 0),
            "avg_duration_ms": float(r.avg_duration_ms) if r.avg_duration_ms else None,
        }
        for r in rows
    ]
    return {
        "agent_id": agent_id,
        "days": days,
        "total_tokens": sum(p["total_tokens"] for p in series),
        "total_requests": sum(p["total_requests"] for p in series),
        "series": series,
    }


def get_usage_by_client(db: Session, *, days: int = 30) -> list[dict]:
    """Group token usage by ``service_clients.id`` (Router / worker)."""
    from datetime import timedelta as _td

    from app.models.service_client import ServiceClient

    start_time = datetime.now(timezone.utc) - _td(days=days)
    rows = (
        db.query(
            TokenUsage.caller_client_id.label("client_id"),
            func.sum(TokenUsage.total_tokens).label("total_tokens"),
            func.count(TokenUsage.id).label("total_requests"),
        )
        .filter(
            TokenUsage.request_timestamp >= start_time,
            TokenUsage.caller_client_id.isnot(None),
        )
        .group_by(TokenUsage.caller_client_id)
        .order_by(func.sum(TokenUsage.total_tokens).desc())
        .all()
    )
    if not rows:
        return []
    clients = {c.id: c for c in db.query(ServiceClient).all()}
    return [
        {
            "client_id": r.client_id,
            "client_name": (
                clients.get(r.client_id).client_name
                if clients.get(r.client_id)
                else "(已刪除)"
            ),
            "client_type": (
                clients.get(r.client_id).client_type
                if clients.get(r.client_id)
                else None
            ),
            "total_tokens": int(r.total_tokens or 0),
            "total_requests": int(r.total_requests or 0),
        }
        for r in rows
    ]
