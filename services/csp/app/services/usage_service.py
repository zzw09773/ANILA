import csv
import io
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import func, literal_column, text
from sqlalchemy.orm import Session
from app.models.api_key import ApiKey
from app.models.department import Department
from app.models.token_usage import TokenUsage
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.auto_seed import PLATFORM_ROUTER_NAME
from app.services.department_tree import get_descendant_ids
from app.services.pricing import PriceBook, cost_rollup, format_micros, get_billing_currency, row_cost_micros
from app.utils.csv_formula import csv_formula_safe
from app.utils.time_helpers import get_time_range


# 台北 = UTC+8。csp 部署在台灣的中科院(NCSIST)內網,使用者在報表期望看
# UTC+8 顯示;DB 內 request_timestamp 是 UTC(server_default=now() AT TIME
# ZONE 'UTC')。在這層做轉換,而不是在前端 — 因為 CSV 是直接下載給 Excel
# / 第三方工具用,沒前端 JS 處理機會。
_TPE_TZ = timezone(timedelta(hours=8))

# 加總時排除、但列仍留在 token_usage。
# router_transport 是轉運跳；platform 是平台替使用者做的背景推理
# （記憶整理、思考進度）。兩者都不進使用者帳單，也不灌推理總量。
_UNBILLED_USAGE_KINDS = ("router_transport", "platform")


def _exclude_platform_chat_entry(query, db: Session):
    """排行與依模型圖表不列入平台對話入口。用量列本身不刪。"""
    entry_id = (
        db.query(ModelRegistry.id)
        .filter(ModelRegistry.name == PLATFORM_ROUTER_NAME)
        .scalar()
    )
    if entry_id is None:
        return query
    return query.filter(TokenUsage.model_id != entry_id)


def _exclude_unbilled(query):
    if hasattr(TokenUsage, "usage_kind"):
        query = query.filter(
            (TokenUsage.usage_kind.is_(None))
            | (TokenUsage.usage_kind.notin_(_UNBILLED_USAGE_KINDS))
        )
    return query


def _to_tpe_iso(dt: datetime | None) -> str:
    """Render `datetime` as `YYYY-MM-DDTHH:MM:SS+08:00` in Asia/Taipei.

    DB column timezone awareness varies (server_default=now() with
    PostgreSQL gives UTC-aware;舊 row 可能 naive)。對 naive datetime
    我們**假設它是 UTC**(這是 csp 既有的 convention)── pin 一致行為,
    避免一些 row 跑 8 小時、一些跑 0 小時的混淆。
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_TPE_TZ).isoformat()


def _get_model_ids_by_type(db: Session, model_type: str) -> list[int]:
    return [
        m.id
        for m in db.query(ModelRegistry.id)
        .filter(ModelRegistry.model_type == model_type)
        .all()
    ]


def _department_scope_ids(
    db: Session, department_id: int | None
) -> list[int] | None:
    """Read-time department filter scope: self + all descendants.

    Returns ``None`` when ``department_id`` is ``None`` (no filter).
    A nonexistent id yields ``[department_id]`` only, so queries match
    nothing — same empty-result behaviour as the former exact match.
    """
    if department_id is None:
        return None
    return sorted(
        get_descendant_ids(db, department_id, include_self=True)
    )


def _resolve_scope_ids(
    db: Session,
    *,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
) -> list[int] | None:
    """Combine caller-allowed ``scope_ids`` with an optional ``department_id``.

    When both are present, take the intersection: ``scope_ids`` is the
    caller's read ceiling, and ``department_id`` further narrows within
    that ceiling. Preferring either side alone would let a narrower
    filter be discarded (or a broader set leak past a constraint).
    An empty intersection stays empty — do not fall back.
    """
    expanded = _department_scope_ids(db, department_id)
    if scope_ids is not None and expanded is not None:
        return sorted(set(scope_ids) & set(expanded))
    if scope_ids is not None:
        return scope_ids
    return expanded


def _apply_usage_filters(
    query,
    db: Session,
    *,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
    api_key_ids: list[int] | None = None,
):
    if model_id:
        query = query.filter(TokenUsage.model_id == model_id)
    if user_id:
        query = query.filter(TokenUsage.user_id == user_id)
    if api_key_ids is not None:
        query = query.filter(TokenUsage.api_key_id.in_(api_key_ids))
    resolved = _resolve_scope_ids(
        db, department_id=department_id, scope_ids=scope_ids
    )
    if resolved is not None:
        query = query.filter(TokenUsage.department_id.in_(resolved))
    if model_type:
        model_ids = _get_model_ids_by_type(db, model_type)
        query = query.filter(TokenUsage.model_id.in_(model_ids))
    return _exclude_unbilled(query)


class ExportRangeError(ValueError):
    """匯出區間不合法。訊息直接給使用者。"""


def _parse_export_day(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ExportRangeError("日期格式須為 YYYY-MM-DD") from exc


def _plus_one_year(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:
        return day.replace(year=day.year + 1, day=28)


def _tpe_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=_TPE_TZ)


def _next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def resolve_export_window(
    range_key: str,
    *,
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> tuple[datetime, datetime | None]:
    """回傳 [start, end)。沒有自訂區間時 end 是 None，沿用原本的 range。"""
    if preset and (start or end):
        raise ExportRangeError("快捷鍵與自訂日期請擇一")
    if preset:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        today = moment.astimezone(_TPE_TZ).date()
        if preset == "this_month":
            start_day = today.replace(day=1)
            end_day = _next_month(start_day)
        elif preset == "last_month":
            this_start = today.replace(day=1)
            end_day = this_start
            if this_start.month == 1:
                start_day = date(this_start.year - 1, 12, 1)
            else:
                start_day = date(this_start.year, this_start.month - 1, 1)
        elif preset == "this_quarter":
            start_month = ((today.month - 1) // 3) * 3 + 1
            start_day = date(today.year, start_month, 1)
            end_month = start_month + 3
            end_year = today.year
            if end_month > 12:
                end_month -= 12
                end_year += 1
            end_day = date(end_year, end_month, 1)
        else:
            raise ExportRangeError("不認識的匯出快捷鍵")
        return _tpe_midnight(start_day), _tpe_midnight(end_day)
    if start or end:
        if not start or not end:
            raise ExportRangeError("請同時提供開始與結束日期")
        start_day = _parse_export_day(start)
        end_day = _parse_export_day(end)
        if end_day < start_day:
            raise ExportRangeError("結束日期不可早於開始日期")
        # 結束日含當天，所以用隔天的排除邊界跟「開始日加一年」比。
        exclusive = end_day + timedelta(days=1)
        if exclusive > _plus_one_year(start_day):
            raise ExportRangeError("單次匯出最長一年")
        return _tpe_midnight(start_day), _tpe_midnight(exclusive)
    start_time, _bucket = get_time_range(range_key)
    return start_time, None


def _filtered_usage_rows(
    db: Session,
    *,
    start_time: datetime,
    end_time: datetime | None,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    scope_ids: list[int] | None = None,
    api_key_ids: list[int] | None = None,
    columns: tuple = (),
):
    selected = (
        TokenUsage.model_id,
        TokenUsage.request_timestamp,
        TokenUsage.prompt_tokens,
        TokenUsage.completion_tokens,
        TokenUsage.reasoning_tokens,
        TokenUsage.total_tokens,
        *columns,
    )
    query = db.query(*selected).filter(TokenUsage.request_timestamp >= start_time)
    if end_time is not None:
        query = query.filter(TokenUsage.request_timestamp < end_time)
    return _apply_usage_filters(
        query,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        scope_ids=scope_ids,
        api_key_ids=api_key_ids,
    ).all()


def get_usage_summary(
    db: Session,
    range_key: str = "24h",
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
) -> dict:
    """Get aggregate usage summary for the selected time range."""
    start_time, _ = get_time_range(range_key)
    # P1.2 fold-in: resolve department edges at most once per request.
    resolved_scope = _resolve_scope_ids(
        db, department_id=department_id, scope_ids=scope_ids
    )

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
        scope_ids=resolved_scope,
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
        scope_ids=resolved_scope,
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
        scope_ids=resolved_scope,
    )
    active_keys = active_keys_q.scalar() or 0

    # Studio (簡報製作等產出) calls carry request_type='studio' — surfaced
    # on their own so the dashboard can answer「做簡報用掉多少」, and kept
    # out of the web-UI bucket they used to hide in (2026-09-02).
    studio_q = db.query(
        func.count(TokenUsage.id).label("n"),
        func.coalesce(func.sum(TokenUsage.total_tokens), 0).label("tokens"),
    ).filter(
        TokenUsage.request_timestamp >= start_time,
        TokenUsage.request_type == "studio",
    )
    studio_q = _apply_usage_filters(
        studio_q,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        scope_ids=resolved_scope,
    )
    studio_row = studio_q.first()
    web_ui_req_q = db.query(func.count(TokenUsage.id)).filter(
        TokenUsage.request_timestamp >= start_time,
        TokenUsage.api_key_id.is_(None),
        TokenUsage.request_type != "studio",
    )
    web_ui_req_q = _apply_usage_filters(
        web_ui_req_q,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        scope_ids=resolved_scope,
    )
    web_ui_requests = web_ui_req_q.scalar() or 0
    cost = cost_rollup(
        db,
        _filtered_usage_rows(
            db,
            start_time=start_time,
            end_time=None,
            model_id=model_id,
            user_id=user_id,
            model_type=model_type,
            scope_ids=resolved_scope,
        ),
    )

    return {
        "total_requests": result.total_requests or 0,
        "total_prompt_tokens": result.total_prompt_tokens or 0,
        "total_completion_tokens": result.total_completion_tokens or 0,
        "total_tokens": result.total_tokens or 0,
        "active_models": int(active_models),
        "active_api_keys": int(active_keys),
        "web_ui_requests": int(web_ui_requests),
        "studio_requests": int(studio_row.n or 0) if studio_row else 0,
        "studio_tokens": int(studio_row.tokens or 0) if studio_row else 0,
        "cost": cost["cost"],
        "cost_currency": cost["cost_currency"],
        "cost_state": cost["cost_state"],
    }


def get_chart_data(
    db: Session,
    range_key: str,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    group_by: str = "total",
    scope_ids: list[int] | None = None,
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
        scope_ids=scope_ids,
    )
    if group_by == "model":
        query = _exclude_platform_chat_entry(query, db)

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
    scope_ids: list[int] | None = None,
    range_key: str = "30d",
) -> list[dict]:
    start_time, _ = get_time_range(range_key)
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
        scope_ids=scope_ids,
    )
    query = _exclude_platform_chat_entry(query, db)

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
    scope_ids: list[int] | None = None,
    range_key: str = "30d",
) -> list[dict]:
    start_time, _ = get_time_range(range_key)
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
        scope_ids=scope_ids,
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
    scope_ids: list[int] | None = None,
    range_key: str = "30d",
) -> list[dict]:
    """Direct-attribution ranking per department (not subtree rollup).

    Subtree rollup is obtained via the ``department_id`` filter path
    (``_department_scope_ids`` / ``_apply_usage_filters``).
    """
    start_time, _ = get_time_range(range_key)
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
        scope_ids=scope_ids,
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


def iter_usage_csv(
    db: Session,
    range_key: str,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
):
    """一筆一筆寫 CSV。價格表只載一次；列用 yield_per，不把一年資料一次放進記憶體。"""
    start_time, end_time = resolve_export_window(
        range_key, preset=preset, start=start, end=end, now=now
    )

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
            TokenUsage.model_id,
            TokenUsage.reasoning_tokens,
        )
        .join(User, TokenUsage.user_id == User.id)
        .outerjoin(Department, TokenUsage.department_id == Department.id)
        .join(ModelRegistry, TokenUsage.model_id == ModelRegistry.id)
        .filter(TokenUsage.request_timestamp >= start_time)
    )
    if end_time is not None:
        query = query.filter(TokenUsage.request_timestamp < end_time)
    query = _apply_usage_filters(
        query,
        db,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        department_id=department_id,
        scope_ids=scope_ids,
    )
    book = PriceBook.load(db)

    header = io.StringIO()
    csv.writer(header).writerow(
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
            "成本",
        ]
    )
    # UTF-8 BOM:Excel 開 CSV 預設用系統編碼(台灣 Windows = Big5/CP950),
    # 沒 BOM 中文表頭/內容會亂碼。前置 U+FEFF 讓 Excel 辨識成 UTF-8。
    yield chr(0xFEFF) + header.getvalue()
    for row in query.order_by(TokenUsage.request_timestamp.desc()).yield_per(500):
        cost = row_cost_micros(row, book)
        line = io.StringIO()
        csv.writer(line).writerow(
            [
                _to_tpe_iso(row.request_timestamp),
                csv_formula_safe(row.username),
                csv_formula_safe(row.department_name or ""),
                csv_formula_safe(row.model_name),
                csv_formula_safe(row.model_type),
                row.prompt_tokens,
                row.completion_tokens,
                row.total_tokens,
                row.request_duration_ms or "",
                format_micros(cost) if cost is not None else "未計價",
            ]
        )
        yield line.getvalue()


def export_usage_csv(
    db: Session,
    range_key: str,
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
    preset: str | None = None,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> str:
    """Export usage data as CSV string."""
    return "".join(
        iter_usage_csv(
            db,
            range_key,
            model_id=model_id,
            user_id=user_id,
            model_type=model_type,
            department_id=department_id,
            scope_ids=scope_ids,
            preset=preset,
            start=start,
            end=end,
            now=now,
        )
    )


def _bucket_cost(db: Session, rows, *, book=None, currency=None) -> dict:
    return cost_rollup(db, rows, book=book, currency=currency)


def get_usage_by_api_key(
    db: Session,
    range_key: str = "24h",
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
) -> list[dict]:
    """依 API 金鑰。沒有金鑰的網頁呼叫單獨一列；簡報製作也不併進網頁。"""
    start_time, _bucket = get_time_range(range_key)
    resolved = _resolve_scope_ids(db, department_id=department_id, scope_ids=scope_ids)
    book = PriceBook.load(db)
    currency = get_billing_currency(db)
    rows = _filtered_usage_rows(
        db,
        start_time=start_time,
        end_time=None,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        scope_ids=resolved,
        columns=(TokenUsage.api_key_id, TokenUsage.request_type),
    )
    names = {key.id: key.name for key in db.query(ApiKey).all()}
    groups: dict[tuple, list] = {}
    for row in rows:
        if row.api_key_id is not None:
            slot = ("key", row.api_key_id)
        elif row.request_type == "studio":
            slot = ("studio", None)
        else:
            slot = ("web", None)
        groups.setdefault(slot, []).append(row)
    out = []
    for (kind, key_id), grouped in groups.items():
        if kind == "key":
            label = names.get(key_id) or f"金鑰 #{key_id}"
        elif kind == "studio":
            label = "簡報／報告"
        else:
            label = "網頁"
        tokens = sum(int(item.total_tokens or 0) for item in grouped)
        cost = _bucket_cost(db, grouped, book=book, currency=currency)
        out.append(
            {
                "api_key_id": key_id,
                "label": label,
                "total_tokens": tokens,
                "total_requests": len(grouped),
                **cost,
            }
        )
    out.sort(key=lambda item: item["total_tokens"], reverse=True)
    return out


def get_usage_by_unit(
    db: Session,
    range_key: str = "24h",
    model_id: int | None = None,
    user_id: int | None = None,
    model_type: str | None = None,
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
) -> list[dict]:
    """每一列是該單位加上所有下層，所以列與列不能相加。"""
    start_time, _bucket = get_time_range(range_key)
    resolved = _resolve_scope_ids(db, department_id=department_id, scope_ids=scope_ids)
    book = PriceBook.load(db)
    currency = get_billing_currency(db)
    rows = _filtered_usage_rows(
        db,
        start_time=start_time,
        end_time=None,
        model_id=model_id,
        user_id=user_id,
        model_type=model_type,
        scope_ids=resolved,
        columns=(TokenUsage.department_id,),
    )
    by_dept: dict[int | None, list] = {}
    for row in rows:
        by_dept.setdefault(row.department_id, []).append(row)
    departments = db.query(Department).all()
    children: dict[int | None, list[int]] = {}
    for dept in departments:
        children.setdefault(dept.parent_id, []).append(dept.id)

    def descendants(root: int) -> set[int]:
        out = {root}
        stack = [root]
        while stack:
            current = stack.pop()
            for child in children.get(current, []):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out

    result = []
    for dept in departments:
        if resolved is not None and dept.id not in resolved:
            continue
        grouped = []
        for dept_id in descendants(dept.id):
            grouped.extend(by_dept.get(dept_id, []))
        if not grouped:
            continue
        cost = _bucket_cost(db, grouped, book=book, currency=currency)
        result.append(
            {
                "department_id": dept.id,
                "department_name": dept.name,
                "label": f"{dept.name}（含下層）",
                "total_tokens": sum(int(item.total_tokens or 0) for item in grouped),
                "total_requests": len(grouped),
                **cost,
            }
        )
    result.sort(key=lambda item: item["total_tokens"], reverse=True)
    return result


def month_usage_for_api_keys(
    db: Session,
    key_ids: list[int],
    now: datetime | None = None,
) -> dict[int, dict]:
    """每把金鑰本月用量。沒有列就是 0 token、未計價。"""
    currency = get_billing_currency(db)
    empty = {
        "month_tokens": 0,
        "month_cost": None,
        "month_cost_state": "unpriced",
        "month_cost_currency": currency,
    }
    if not key_ids:
        return {}
    book = PriceBook.load(db)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(_TPE_TZ)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    rows = _filtered_usage_rows(
        db,
        start_time=start_local.astimezone(timezone.utc),
        end_time=end_local.astimezone(timezone.utc),
        api_key_ids=list(key_ids),
        columns=(TokenUsage.api_key_id,),
    )
    grouped: dict[int, list] = {}
    wanted = set(key_ids)
    for row in rows:
        if row.api_key_id in wanted:
            grouped.setdefault(row.api_key_id, []).append(row)
    out = {key_id: dict(empty) for key_id in key_ids}
    for key_id, grouped_rows in grouped.items():
        cost = _bucket_cost(db, grouped_rows, book=book, currency=currency)
        out[key_id] = {
            "month_tokens": sum(int(item.total_tokens or 0) for item in grouped_rows),
            "month_cost": cost["cost"],
            "month_cost_state": cost["cost_state"],
            "month_cost_currency": cost["cost_currency"],
        }
    return out


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
    department_id: int | None = None,
    scope_ids: list[int] | None = None,
) -> list[dict]:
    """Top-N agents by token consumption (CSP-forwarded + agent-callback)."""
    from datetime import timedelta as _td

    from app.models.agent import Agent

    start_time = datetime.now(timezone.utc) - _td(days=days)
    query = db.query(
        TokenUsage.caller_agent_id.label("agent_id"),
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
        func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
        func.count(TokenUsage.id).label("total_requests"),
    ).filter(
        TokenUsage.request_timestamp >= start_time,
        TokenUsage.caller_agent_id.isnot(None),
    )
    query = _apply_usage_filters(
        query,
        db,
        department_id=department_id,
        scope_ids=scope_ids,
    )
    rows = (
        query.group_by(TokenUsage.caller_agent_id)
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
            # chart 的 bucket 對齊匯出 CSV,同樣輸出 Asia/Taipei 時區。
            "timestamp": _to_tpe_iso(r.bucket) or None,
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


def _caller_usage_rows(db: Session, *, user_id: int, start_time: datetime):
    query = db.query(TokenUsage).filter(
        TokenUsage.user_id == user_id,
        TokenUsage.request_timestamp >= start_time,
    )
    return _exclude_unbilled(query).all()


def _sum_reasoning(rows) -> int:
    return sum(int(row.reasoning_tokens or 0) for row in rows)


def get_caller_usage(db: Session, *, user_id: int, range_key: str) -> dict:
    """Self-scoped usage for the signed-in caller (no other users, no api keys)."""
    start_time, _ = get_time_range(range_key)
    rows = _caller_usage_rows(db, user_id=user_id, start_time=start_time)
    names = {
        model.id: (model.display_name or model.name)
        for model in db.query(ModelRegistry).all()
    }
    by_model: dict[int, dict] = {}
    by_kind: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    prompt = completion = 0
    for row in rows:
        prompt += int(row.prompt_tokens or 0)
        completion += int(row.completion_tokens or 0)
        model_entry = by_model.setdefault(
            row.model_id,
            {
                "model_id": row.model_id,
                "model_name": names.get(row.model_id)
                or row.model_name_snapshot
                or str(row.model_id),
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        model_entry["requests"] += 1
        model_entry["prompt_tokens"] += int(row.prompt_tokens or 0)
        model_entry["completion_tokens"] += int(row.completion_tokens or 0)
        model_entry["reasoning_tokens"] += int(row.reasoning_tokens or 0)

        kind = row.request_type or "chat"
        kind_entry = by_kind.setdefault(
            kind,
            {
                "kind": kind,
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        )
        kind_entry["requests"] += 1
        kind_entry["prompt_tokens"] += int(row.prompt_tokens or 0)
        kind_entry["completion_tokens"] += int(row.completion_tokens or 0)
        kind_entry["reasoning_tokens"] += int(row.reasoning_tokens or 0)
        kind_entry["total_tokens"] += int(row.total_tokens or 0)

        day = _to_tpe_iso(row.request_timestamp)[:10]
        day_entry = by_day.setdefault(
            day,
            {
                "date": day,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        day_entry["prompt_tokens"] += int(row.prompt_tokens or 0)
        day_entry["completion_tokens"] += int(row.completion_tokens or 0)
        day_entry["reasoning_tokens"] += int(row.reasoning_tokens or 0)

    return {
        "range": range_key,
        "requests": len(rows),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": _sum_reasoning(rows),
        "total_tokens": prompt + completion,
        "by_model": sorted(by_model.values(), key=lambda item: item["model_id"]),
        "by_day": [by_day[key] for key in sorted(by_day)],
        "by_kind": sorted(by_kind.values(), key=lambda item: item["kind"]),
    }


def get_conversation_usage(
    db: Session, *, user_id: int, conversation_id: str
) -> dict:
    """Owner-only conversation totals. ``conversation_id`` compared as string."""
    query = db.query(TokenUsage).filter(
        TokenUsage.user_id == user_id,
        TokenUsage.conversation_id == str(conversation_id),
    )
    rows = _exclude_unbilled(query).all()
    prompt = sum(int(row.prompt_tokens or 0) for row in rows)
    completion = sum(int(row.completion_tokens or 0) for row in rows)
    return {
        "conversation_id": str(conversation_id),
        "requests": len(rows),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": _sum_reasoning(rows),
        "total_tokens": prompt + completion,
    }
