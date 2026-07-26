"""W2-12 驗收:結構化錯誤信封與 API 契約。

為什麼這支測試存在
------------------
在此之前 CSP 有 770 個 ``HTTPException`` 但**零** ``add_exception_handler``,
於是同一個後端會吐出至少三種互不相容的錯誤形狀:

1. ``HTTPException(detail="字串")``      → ``{"detail": "字串"}``
2. ``HTTPException(detail={...})``（4 處） → ``{"detail": {"code","message"}}``
3. ``RequestValidationError``（422）      → ``{"detail": [ {...}, ... ]}``

前端只能猜。`apps/csp-governance-ui` 有 102 處(22 檔)直接把 ``data.detail`` 插進字串
→ 形狀 2/3 渲染成 ``[object Object]``;`apps/anilalm` 的 ``explainError``
處理 string/array 但不處理 dict → 整條訊息退化成 ``"503 Request failed"``。
更糟的是 ``LoginView.vue`` 靠比對後端中文子字串 ``'等待核准'`` 判斷待核准帳號
——後端改一個字,登入 UX 就靜默壞掉。

本檔釘住兩件事:
- **信封不變式**:所有錯誤回應都符合同一個 schema(``error.code`` /
  ``error.message`` / ``error.details`` / ``error.request_id``)。
- **過渡雙寫**:legacy ``detail`` 的形狀與值**逐字不變**,舊前端不會被切死。
  這是刻意的;移除 ``detail`` 要等下一個 release。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errors import (
    ApiError,
    ErrorCode,
    register_exception_handlers,
)
from app.schemas.errors import ErrorEnvelope
from tests.conftest import make_user


# ── 信封 schema 斷言 ────────────────────────────────────────────────────────

def assert_envelope(payload: object) -> dict:
    """驗證 payload 符合信封 schema,回傳 ``error`` 物件。

    用 Pydantic model 驗證而非手寫 assert:schema 就是契約本身,
    多一個欄位少一個欄位都會在這裡爆。
    """
    assert isinstance(payload, dict), f"回應不是 JSON object: {payload!r}"
    envelope = ErrorEnvelope.model_validate(payload)
    error = envelope.error
    assert error.code, "error.code 不得為空"
    assert isinstance(error.code, str)
    assert error.message, "error.message 不得為空"
    assert isinstance(error.message, str)
    # request_id 欄位**必須存在**(W3-3 之後才有真值),序列化後不得消失。
    assert "request_id" in payload["error"], "error.request_id 欄位缺失"
    return payload["error"]


# ── ① 同一個信封 schema:HTTPException 與 422 ──────────────────────────────

def test_http_exception_string_detail_matches_envelope(client, db):
    """``HTTPException(detail="字串")`` → 信封 + legacy detail 原字串。"""
    make_user(db, username="bob")
    resp = client.post(
        "/api/auth/login",
        json={"username": "bob", "password": "wrong-password"},
    )
    assert resp.status_code == 401, resp.text
    error = assert_envelope(resp.json())
    assert error["code"] == ErrorCode.AUTH_INVALID_CREDENTIALS.value
    # ② 過渡雙寫:legacy 形狀逐字不變(字串,不是 dict、不是 list)
    assert resp.json()["detail"] == "帳號或密碼錯誤"


def test_request_validation_error_matches_same_envelope(client):
    """422 與 HTTPException **共用同一個 schema**——原本是兩種形狀。"""
    resp = client.post("/api/auth/login", json={"username": "no-password"})
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    error = assert_envelope(payload)
    assert error["code"] == ErrorCode.VALIDATION_ERROR.value
    # pydantic 的逐欄位錯誤進 details,不再是前端要自己認的 top-level array
    assert isinstance(error["details"], list) and error["details"], error
    assert any("password" in str(item.get("loc", "")) for item in error["details"])
    # ② 過渡雙寫:legacy detail 仍是 FastAPI 原本的 array,且與 details 等值
    assert isinstance(payload["detail"], list)
    assert payload["detail"] == error["details"]


def test_dict_detail_code_is_promoted_into_envelope():
    """既有 4 處 typed dict detail 的 code 要被拉進信封。

    3 處是字面 ``detail={...}``(``services/proxy/service.py:180,582``、
    ``api/proxy.py:326``),第 4 處經 ``detail`` 變數傳入(``api/models.py:357``
    的 ``untrusted_host``)。都不在本包可改範圍,所以用隔離 app 驗 handler
    的 passthrough 行為。
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/dict-detail")
    def _dict_detail():
        raise HTTPException(
            status_code=503,
            detail={"code": "model_unhealthy", "message": "模型已被標記 unhealthy"},
        )

    with TestClient(app) as c:
        resp = c.get("/dict-detail")
    assert resp.status_code == 503
    payload = resp.json()
    error = assert_envelope(payload)
    assert error["code"] == "model_unhealthy"
    assert error["message"] == "模型已被標記 unhealthy"
    # ② legacy dict 原封不動
    assert payload["detail"] == {
        "code": "model_unhealthy",
        "message": "模型已被標記 unhealthy",
    }


def test_bare_http_exception_gets_generic_code():
    """沒帶 detail 的 ``HTTPException(404)`` 也要有 code,不能是空字串。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/bare")
    def _bare():
        raise HTTPException(status_code=404)

    with TestClient(app) as c:
        resp = c.get("/bare")
    assert resp.status_code == 404
    error = assert_envelope(resp.json())
    assert error["code"] == ErrorCode.NOT_FOUND.value
    assert resp.json()["detail"] == "Not Found"


def test_framework_raised_404_and_405_are_also_enveloped(client):
    """Starlette 自己 raise 的 404 / 405 也必須進信封。

    這是一個真實踩到的坑:FastAPI 預設把 handler 註冊在
    ``starlette.exceptions.HTTPException`` 上,而 ``fastapi.HTTPException`` 是它的
    **子類**。只註冊子類的話,route-not-found / method-not-allowed 這些由框架
    直接 raise 的基底類例外會落回 FastAPI 預設 handler,回 ``{"detail": ...}``
    —— 前端最常撞到的兩個錯誤反而是唯一沒有 ``error.code`` 的。
    """
    missing = client.get("/api/definitely-not-a-route-xyz")
    assert missing.status_code == 404
    error = assert_envelope(missing.json())
    assert error["code"] == ErrorCode.NOT_FOUND.value
    assert missing.json()["detail"] == "Not Found"

    wrong_method = client.request("PATCH", "/health")
    assert wrong_method.status_code == 405
    error = assert_envelope(wrong_method.json())
    assert error["code"] == ErrorCode.HTTP_ERROR.value
    assert wrong_method.json()["detail"] == "Method Not Allowed"


def test_handler_preserves_headers_and_empty_body_statuses():
    """401 的 ``WWW-Authenticate`` 不能被信封吃掉;204 不得有 body。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/needs-auth")
    def _needs_auth():
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.get("/no-content")
    def _no_content():
        raise HTTPException(status_code=204)

    with TestClient(app) as c:
        auth = c.get("/needs-auth")
        empty = c.get("/no-content")

    assert auth.status_code == 401
    assert auth.headers["www-authenticate"] == "Bearer"
    assert_envelope(auth.json())

    assert empty.status_code == 204
    assert empty.content == b"", "204 不得帶 body(Starlette 原本行為)"


def test_api_error_carries_code_and_details():
    """``ApiError`` 是 raise site 掛 code 的正式管道。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/quota")
    def _quota():
        raise ApiError(
            status_code=429,
            code=ErrorCode.QUOTA_EXCEEDED,
            message="本月配額已用盡",
            details={"limit": 1000, "used": 1000},
        )

    with TestClient(app) as c:
        resp = c.get("/quota")
    assert resp.status_code == 429
    payload = resp.json()
    error = assert_envelope(payload)
    assert error["code"] == ErrorCode.QUOTA_EXCEEDED.value
    assert error["message"] == "本月配額已用盡"
    assert error["details"] == {"limit": 1000, "used": 1000}
    # legacy 前端只認 detail 字串 → ApiError 的 detail 必須是可讀訊息
    assert payload["detail"] == "本月配額已用盡"


def test_request_id_echoes_inbound_header():
    """``X-Request-ID`` 若已存在就回填(W3-3 之前 header 是唯一來源)。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    def _boom():
        raise HTTPException(status_code=400, detail="boom")

    with TestClient(app) as c:
        with_id = c.get("/boom", headers={"X-Request-ID": "req-abc-123"})
        without_id = c.get("/boom")

    assert with_id.json()["error"]["request_id"] == "req-abc-123"
    assert without_id.json()["error"]["request_id"] is None


# ── ④ 防再犯:登入分流靠 code,不靠中文訊息 ────────────────────────────────

def test_pending_approval_login_is_identified_by_code_not_message(client, db):
    """待核准登入必須給 ``AUTH_PENDING_APPROVAL``。

    這條是**防再犯**:`LoginView.vue:419` 原本比對 ``detail.includes('等待核准')``,
    後端改字就靜默壞掉。斷言刻意**不看訊息內容**,只看 code。
    """
    make_user(db, username="carol", is_approved=False)
    resp = client.post(
        "/api/auth/login",
        json={"username": "carol", "password": "password"},
    )
    assert resp.status_code == 403, resp.text
    error = assert_envelope(resp.json())
    assert error["code"] == ErrorCode.AUTH_PENDING_APPROVAL.value
    # legacy 雙寫仍在(舊前端過渡期靠它)
    assert isinstance(resp.json()["detail"], str)
    assert resp.json()["detail"]


def test_csrf_rejection_uses_the_same_envelope(client, db):
    """CSRF middleware 直接回 JSONResponse,原本繞過所有 handler。"""
    from tests.conftest import login

    make_user(db, username="dave")
    login(client, username="dave")
    # 帶 cookie 但不帶 X-CSRF-Token 的 mutating request
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 403, resp.text
    error = assert_envelope(resp.json())
    assert error["code"] == ErrorCode.AUTH_CSRF_INVALID.value
    assert isinstance(resp.json()["detail"], str)


# ── error code 列舉的護欄 ──────────────────────────────────────────────────

def test_error_codes_are_unique_and_screaming_snake():
    values = [c.value for c in ErrorCode]
    assert len(values) == len(set(values)), "ErrorCode 值重複"
    for code in ErrorCode:
        assert code.value == code.name, (
            f"{code.name} 的 wire 值與名稱不一致,前端對照表會漂"
        )


def test_envelope_schema_is_exported_for_openapi():
    """信封 schema 必須是可被 OpenAPI 引用的 Pydantic model。"""
    assert issubclass(ErrorEnvelope, BaseModel)
    schema = ErrorEnvelope.model_json_schema()
    assert "error" in schema["properties"]


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (400, ErrorCode.BAD_REQUEST),
        (401, ErrorCode.UNAUTHENTICATED),
        (403, ErrorCode.FORBIDDEN),
        (404, ErrorCode.NOT_FOUND),
        (409, ErrorCode.CONFLICT),
        (413, ErrorCode.PAYLOAD_TOO_LARGE),
        (429, ErrorCode.RATE_LIMITED),
        (500, ErrorCode.INTERNAL_ERROR),
        (503, ErrorCode.SERVICE_UNAVAILABLE),
        (418, ErrorCode.HTTP_ERROR),
    ],
)
def test_generic_status_to_code_fallback(status_code, expected):
    """767 個既有 raise site 不逐一改 → fallback 必須確定且可預測。"""
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/x")
    def _x():
        raise HTTPException(status_code=status_code, detail="whatever")

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/x")
    assert resp.status_code == status_code
    assert resp.json()["error"]["code"] == expected.value
