"""治理稽核查詢的可用性驗收:時間範圍、keyset 分頁、CSV 串流匯出、strict 寫入。

W1-5。四件事各自的機械判準:

1. date-range + cursor 分頁**跨頁不重不漏**(固定 fixture 逐筆比對,含同一
   ``created_at`` 多列以驗 id tiebreak)。
2. CSV 端點回 ``text/csv`` 且是**串流**(逐列 yield,不是一次組完整個 body)。
3. ``ANILA_AUDIT_STRICT=1`` 時稽核寫入失敗 → 請求 503(注入寫入失敗)。
4. 未取得 ``can_view_inference_audit`` 的 admin 讀不到推論列(prompt 內容)。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import audit_logs as audit_api
from app.models.audit_log import AuditLog
from app.services import audit_service
from app.services.auth_service import create_tokens
from tests.conftest import make_user


BASE = datetime(2026, 7, 1, 3, 0, 0)


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _iso(dt: datetime) -> str:
    """本地 helper:naive-UTC → 帶 offset 的 ISO8601(API 只吃帶時區的值)。"""
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _add_row(
    db: Session,
    *,
    created_at: datetime,
    action: str = "user.update",
    resource_type: str = "user",
    status: str = "success",
    detail: str | None = None,
    actor_username: str = "governance_actor",
    ip_address: str = "198.51.100.5",
    metadata_json: str | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_user_id=None,
        actor_username=actor_username,
        action=action,
        resource_type=resource_type,
        resource_id="7",
        status=status,
        detail=detail if detail is not None else f"事件 {created_at.isoformat()}",
        ip_address=ip_address,
        metadata_json=metadata_json,
        created_at=created_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def fixed_rows(db: Session) -> list[AuditLog]:
    """10 列固定 fixture。

    第 4/5 列刻意共用同一個 ``created_at``:只用 created_at 排序時,分頁邊界
    會在這裡重複或漏列 —— 這正是要擋的 bug。
    """
    rows = []
    for i in range(10):
        minute = 3 if i in (3, 4) else i
        rows.append(_add_row(db, created_at=BASE + timedelta(minutes=minute)))
    return rows


def _expected_order(rows: list[AuditLog]) -> list[int]:
    return [
        row.id
        for row in sorted(rows, key=lambda r: (r.created_at, r.id), reverse=True)
    ]


# ── 時間範圍 ────────────────────────────────────────────────────────────────


def test_date_range_is_inclusive_on_both_ends(
    client: TestClient, db: Session, fixed_rows
):
    admin = make_user(db, username="audit_range_admin", role="admin")
    resp = client.get(
        "/api/audit-logs",
        headers=_bearer(admin),
        params={
            "from": _iso(BASE + timedelta(minutes=2)),
            "to": _iso(BASE + timedelta(minutes=5)),
        },
    )
    assert resp.status_code == 200, resp.text
    got = {row["id"] for row in resp.json()}
    expected = {
        row.id
        for row in fixed_rows
        if BASE + timedelta(minutes=2)
        <= row.created_at
        <= BASE + timedelta(minutes=5)
    }
    assert got == expected
    # fixture 的 minute 序列是 [0,1,2,3,3,5,6,7,8,9](第 4/5 列同秒),
    # 所以 [2,5] 這個窗剛好 4 列:minute 2、3、3、5。
    assert len(expected) == 4


def test_naive_datetime_filter_is_rejected(client: TestClient, db: Session):
    admin = make_user(db, username="audit_naive_admin", role="admin")
    resp = client.get(
        "/api/audit-logs",
        headers=_bearer(admin),
        params={"from": "2026-07-01T03:00:00"},
    )
    assert resp.status_code == 422, resp.text
    assert "時區" in resp.json()["detail"]


def test_created_at_is_serialized_with_utc_offset(
    client: TestClient, db: Session, fixed_rows
):
    """沒有 offset 的話瀏覽器會當 local time,而 client 也無法拿它當 cursor。"""
    admin = make_user(db, username="audit_tz_admin", role="admin")
    resp = client.get("/api/audit-logs", headers=_bearer(admin), params={"limit": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["created_at"].endswith(("Z", "+00:00"))


# ── keyset 分頁 ─────────────────────────────────────────────────────────────


def test_cursor_pagination_covers_every_row_exactly_once(
    client: TestClient, db: Session, fixed_rows
):
    admin = make_user(db, username="audit_page_admin", role="admin")
    seen: list[int] = []
    cursor: dict[str, object] = {}
    for _ in range(10):  # 上限,避免寫錯時無限迴圈
        params = {"limit": 3, **cursor}
        resp = client.get("/api/audit-logs", headers=_bearer(admin), params=params)
        assert resp.status_code == 200, resp.text
        page = resp.json()
        if not page:
            break
        seen.extend(row["id"] for row in page)
        last = page[-1]
        cursor = {
            "cursor_created_at": last["created_at"],
            "cursor_id": last["id"],
        }
    assert seen == _expected_order(fixed_rows)
    assert len(seen) == len(set(seen)) == 10


def test_cursor_pagination_respects_date_range(
    client: TestClient, db: Session, fixed_rows
):
    admin = make_user(db, username="audit_page_range_admin", role="admin")
    window = {
        "from": _iso(BASE + timedelta(minutes=1)),
        "to": _iso(BASE + timedelta(minutes=3)),
    }
    in_window = [
        row
        for row in fixed_rows
        if BASE + timedelta(minutes=1)
        <= row.created_at
        <= BASE + timedelta(minutes=3)
    ]
    seen: list[int] = []
    cursor: dict[str, object] = {}
    for _ in range(10):
        resp = client.get(
            "/api/audit-logs",
            headers=_bearer(admin),
            params={"limit": 2, **window, **cursor},
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        if not page:
            break
        seen.extend(row["id"] for row in page)
        cursor = {
            "cursor_created_at": page[-1]["created_at"],
            "cursor_id": page[-1]["id"],
        }
    assert seen == _expected_order(in_window)
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize(
    "params",
    [
        {"cursor_created_at": _iso(BASE)},
        {"cursor_id": 3},
    ],
)
def test_half_cursor_is_rejected(client: TestClient, db: Session, params):
    admin = make_user(db, username="audit_halfcursor_admin", role="admin")
    resp = client.get("/api/audit-logs", headers=_bearer(admin), params=params)
    assert resp.status_code == 422, resp.text
    assert "成對" in resp.json()["detail"]


def test_detail_keyword_search_matches_literal_substring(
    client: TestClient, db: Session
):
    admin = make_user(db, username="audit_q_admin", role="admin")
    hit = _add_row(db, created_at=BASE, detail="改了模型設定 gpt-oss-20b")
    _add_row(db, created_at=BASE + timedelta(minutes=1), detail="無關事件")
    # ``%`` 是 ILIKE 的萬用字元,必須被當字面值處理(不能變成 match-all)。
    _add_row(db, created_at=BASE + timedelta(minutes=2), detail="100%完成")
    resp = client.get(
        "/api/audit-logs", headers=_bearer(admin), params={"q": "模型設定"}
    )
    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()] == [hit.id]

    wildcard = client.get(
        "/api/audit-logs", headers=_bearer(admin), params={"q": "%"}
    )
    assert wildcard.status_code == 200
    assert [row["detail"] for row in wildcard.json()] == ["100%完成"]


# ── CSV 串流匯出 ────────────────────────────────────────────────────────────


def test_export_returns_streaming_csv(client: TestClient, db: Session, fixed_rows):
    admin = make_user(db, username="audit_export_admin", role="admin")
    resp = client.get("/api/audit-logs/export", headers=_bearer(admin))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/csv")
    assert "audit-logs.csv" in resp.headers["content-disposition"]
    # 串流(chunked)的證據之一:body 不是先組完再量長度。
    assert "content-length" not in {k.lower() for k in resp.headers}
    assert resp.content.startswith(b"\xef\xbb\xbf")  # Excel 要的 UTF-8 BOM
    reader = csv.reader(io.StringIO(resp.content.decode("utf-8-sig")))
    header = next(reader)
    assert tuple(header) == audit_api._CSV_FIELDS
    body = list(reader)
    assert [int(row[0]) for row in body] == _expected_order(fixed_rows)


async def test_export_yields_row_by_row_not_one_assembled_body(
    db: Session, fixed_rows
):
    """直接檢查 generator:每列一個 chunk,而不是一次組完。"""
    admin = make_user(db, username="audit_stream_admin", role="admin")
    response = audit_api.export_audit_logs(
        action=None,
        resource_type=None,
        actor_username=None,
        status=None,
        q=None,
        from_=None,
        to=None,
        admin=admin,
        db=db,
    )
    assert isinstance(response, StreamingResponse)
    # StreamingResponse 會把同步 generator 包成 async iterator。
    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks[0] == "\ufeff"
    assert chunks[1].startswith("id,created_at")
    # BOM + header + 每列一個 chunk
    assert len(chunks) == 2 + len(fixed_rows)


def test_export_keyset_loop_crosses_batches_without_duplicates(
    client: TestClient, db: Session, fixed_rows, monkeypatch
):
    monkeypatch.setattr(audit_api, "EXPORT_BATCH_SIZE", 2)
    admin = make_user(db, username="audit_batch_admin", role="admin")
    resp = client.get("/api/audit-logs/export", headers=_bearer(admin))
    assert resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    ids = [int(row["id"]) for row in rows]
    assert ids == _expected_order(fixed_rows)
    assert len(ids) == len(set(ids))


def test_export_without_date_range_is_row_capped(
    client: TestClient, db: Session, fixed_rows, monkeypatch
):
    monkeypatch.setattr(audit_api, "EXPORT_MAX_ROWS_WITHOUT_DATE", 3)
    admin = make_user(db, username="audit_cap_admin", role="admin")
    capped = client.get("/api/audit-logs/export", headers=_bearer(admin))
    text = capped.content.decode("utf-8-sig")
    assert "# truncated at 3" in text
    rows = [
        row
        for row in csv.DictReader(io.StringIO(text))
        if not row["id"].startswith("#")
    ]
    assert len(rows) == 3

    dated = client.get(
        "/api/audit-logs/export",
        headers=_bearer(admin),
        params={"from": _iso(BASE - timedelta(days=1))},
    )
    dated_text = dated.content.decode("utf-8-sig")
    assert "# truncated" not in dated_text
    assert len(list(csv.DictReader(io.StringIO(dated_text)))) == 10


def test_export_masks_owner_only_columns_and_neutralizes_formulas(
    client: TestClient, db: Session
):
    owner = make_user(db, username="audit_export_owner", role="owner")
    admin = make_user(db, username="audit_export_masked", role="admin")
    _add_row(
        db,
        created_at=BASE,
        detail="=1+1",
        actor_username="=HYPERLINK",
        ip_address="203.0.113.9",
        metadata_json='{"x":1}',
    )
    masked = list(
        csv.DictReader(
            io.StringIO(
                client.get("/api/audit-logs/export", headers=_bearer(admin))
                .content.decode("utf-8-sig")
            )
        )
    )
    assert masked[0]["detail"] == "'=1+1"
    assert masked[0]["actor_username"] == "'=HYPERLINK"
    assert masked[0]["ip_address"] == "<owner-only>"
    assert masked[0]["metadata_json"] == ""

    raw = list(
        csv.DictReader(
            io.StringIO(
                client.get("/api/audit-logs/export", headers=_bearer(owner))
                .content.decode("utf-8-sig")
            )
        )
    )
    assert raw[0]["ip_address"] == "203.0.113.9"
    assert raw[0]["metadata_json"] == '{"x":1}'


# ── 推論列(prompt 內容)只給有授權的人 ─────────────────────────────────────


def test_inference_rows_hidden_from_admin_without_grant(
    client: TestClient, db: Session
):
    admin = make_user(db, username="audit_inf_plain_admin", role="admin")
    governance = _add_row(db, created_at=BASE, action="user.create")
    _add_row(
        db,
        created_at=BASE + timedelta(minutes=1),
        action="inference.chat",
        resource_type="inference",
        detail="使用者的完整 prompt",
    )
    listed = client.get("/api/audit-logs", headers=_bearer(admin))
    assert [row["id"] for row in listed.json()] == [governance.id]

    exported = client.get("/api/audit-logs/export", headers=_bearer(admin))
    assert "使用者的完整 prompt" not in exported.content.decode("utf-8-sig")


def test_inference_rows_visible_with_grant_and_to_owner(
    client: TestClient, db: Session
):
    granted = make_user(db, username="audit_inf_granted_admin", role="admin")
    granted.can_view_inference_audit = True
    owner = make_user(db, username="audit_inf_owner", role="owner")
    db.commit()
    inference = _add_row(
        db,
        created_at=BASE,
        action="inference.chat",
        resource_type="inference",
        detail="使用者的完整 prompt",
    )
    for user in (granted, owner):
        listed = client.get("/api/audit-logs", headers=_bearer(user))
        assert [row["id"] for row in listed.json()] == [inference.id], user.username


# ── ANILA_AUDIT_STRICT:寫入失敗不再靜默 ────────────────────────────────────


def _break_audit_writes(monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("audit_logs insert exploded")

    monkeypatch.setattr(audit_service, "_persist_audit_row", _boom)


def test_strict_audit_write_failure_rejects_the_request(
    client: TestClient, db: Session, monkeypatch
):
    make_user(db, username="strict_login_user")
    monkeypatch.setattr(audit_service.settings, "ANILA_AUDIT_STRICT", True)
    _break_audit_writes(monkeypatch)
    resp = client.post(
        "/api/auth/login",
        json={"username": "strict_login_user", "password": "wrong-password"},
    )
    assert resp.status_code == 503, resp.text
    assert "審計" in resp.json()["detail"]


def test_fail_open_default_keeps_the_original_status_code(
    client: TestClient, db: Session, monkeypatch
):
    """預設(非 strict)仍是 fail-soft:401 不該被 audit 失敗蓋成 5xx。"""
    make_user(db, username="failopen_login_user")
    monkeypatch.setattr(audit_service.settings, "ANILA_AUDIT_STRICT", False)
    _break_audit_writes(monkeypatch)
    resp = client.post(
        "/api/auth/login",
        json={"username": "failopen_login_user", "password": "wrong-password"},
    )
    assert resp.status_code == 401, resp.text


def test_strict_flag_is_read_at_call_time_not_import_time(
    db: Session, monkeypatch
):
    from fastapi import HTTPException

    _break_audit_writes(monkeypatch)
    monkeypatch.setattr(audit_service.settings, "ANILA_AUDIT_STRICT", False)
    assert (
        audit_service.log_audit_event(
            db, action="user.create", resource_type="user", commit=True
        )
        is None
    )
    monkeypatch.setattr(audit_service.settings, "ANILA_AUDIT_STRICT", True)
    with pytest.raises(HTTPException) as excinfo:
        audit_service.log_audit_event(
            db, action="user.create", resource_type="user", commit=True
        )
    assert excinfo.value.status_code == 503
