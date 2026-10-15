"""已核准 MCP 查詢工具、本人身分委託與有限額的 HTTP 傳輸。"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from jose import jwt
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_core.security import UnsafeEndpointError, validate_outbound_url
from anila_core.security.url_guard import _env_trusted_hosts, _resolve_host
from app.models.department import Department
from app.models.quick_mcp import QuickMCPService, QuickMCPTool
from app.models.user import User
from app.schemas.quick_mcp import MCPServiceCreate
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import is_admin_tier
from app.services.endpoint_author_service import (
    require_endpoint_address_author,
    visible_endpoint_url,
)
from app.services.unit_admin_service import get_unit_admin_scope_ids
from app.services.trusted_host_service import get_cached_hosts
from app.utils.security import ALGORITHM, _claims_if_issuance_allowed

MAX_ARGUMENT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_TOOLS = 100
MCP_TIMEOUT_SECONDS = 20
MCP_TOTAL_SECONDS = 30
MCP_ENDPOINT_TIMEOUT_SECONDS = 8
MCP_JWT_TTL_SECONDS = 60
MCP_JWT_ISSUER = "anila-csp"
MCP_PROTOCOL_VERSION = "2025-03-26"
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "title",
        "description",
        "default",
        "examples",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "$schema",
    }
)


def _validate_schema(schema: dict, depth: int = 0) -> None:
    """有限的 JSON Schema 子集；不解析遠端 $ref 或可執行擴充。"""
    if not isinstance(schema, dict) or depth > 8 or set(schema) - _SCHEMA_KEYS:
        raise _error(502, "mcp_schema_unsupported", "MCP 工具參數含不支援的結構或過深巢狀")
    if schema.get("type") not in {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
        "null",
    }:
        raise _error(502, "mcp_schema_unsupported", "MCP 參數必須明確指定基本資料型別")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise _error(502, "mcp_schema_invalid", "MCP 參數選項格式不正確")
    for name in ("minItems", "maxItems", "minLength", "maxLength"):
        if name in schema and (type(schema[name]) is not int or schema[name] < 0):
            raise _error(502, "mcp_schema_invalid", "MCP 參數長度限制格式不正確")
    for name in ("minimum", "maximum"):
        if name in schema and type(schema[name]) not in (int, float):
            raise _error(502, "mcp_schema_invalid", "MCP 參數數值限制格式不正確")
    if schema["type"] == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or len(properties) > 128
            or not isinstance(required, list)
            or not all(isinstance(k, str) and k in properties for k in required)
            or type(schema.get("additionalProperties", True)) is not bool
        ):
            raise _error(502, "mcp_schema_invalid", "MCP 物件參數格式不正確")
        for child in properties.values():
            _validate_schema(child, depth + 1)
    elif schema["type"] == "array":
        _validate_schema(schema.get("items"), depth + 1)


def _validate_arguments(schema: dict, value, depth: int = 0) -> None:
    expected = schema["type"]
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if (
        depth > 8
        or not isinstance(value, types[expected])
        or (expected in ("integer", "number") and isinstance(value, bool))
    ):
        raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數型別不符")
    if "enum" in schema and not any(
        type(value) is type(item) and value == item for item in schema["enum"]
    ):
        raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數不在允許選項中")
    if "const" in schema and (
        type(value) is not type(schema["const"]) or value != schema["const"]
    ):
        raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數不符固定值")
    if expected == "object":
        properties = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])) or (
            schema.get("additionalProperties") is False
            and set(value) - properties.keys()
        ):
            raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數缺少必要欄位或含未允許欄位")
        for key, item in value.items():
            if key in properties:
                _validate_arguments(properties[key], item, depth + 1)
    if expected == "array":
        for item in value:
            _validate_arguments(schema["items"], item, depth + 1)
    if expected in ("array", "string"):
        lower, upper = (
            ("minItems", "maxItems")
            if expected == "array"
            else ("minLength", "maxLength")
        )
        if len(value) < schema.get(lower, 0) or len(value) > schema.get(
            upper, MAX_ARGUMENT_BYTES
        ):
            raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數長度超過允許範圍")
    if expected in ("integer", "number") and (
        value < schema.get("minimum", float("-inf"))
        or value > schema.get("maximum", float("inf"))
    ):
        raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數數值超過允許範圍")


def _error(code: int, kind: str, message: str) -> HTTPException:
    return HTTPException(code, detail={"error_type": kind, "message": message})


def _json_bytes(value) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise _error(422, "invalid_json", "資料必須是有效的 JSON") from exc


def tool_fingerprint(tool: dict) -> str:
    """指紋含工具說明、schema 與 annotations，任一變更都須重新核准。"""
    return hashlib.sha256(
        _json_bytes(
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "inputSchema": tool["inputSchema"],
                "annotations": tool.get("annotations", {}),
            }
        )
    ).hexdigest()


def mcp_audience(service_id: int, tool_name: str) -> str:
    return f"urn:anila:quick-mcp:{service_id}:{tool_name}"


def issue_mcp_token(
    db: Session, user: User, service_id: int, tool_name: str, *, assistant_id: int
) -> str:
    """每次操作簽發一分鐘委託，使用平台既有 active signing material。"""
    from app.services.jwt_keyring import active_signing_material

    now = int(time.time())
    claims = {
        "iss": MCP_JWT_ISSUER,
        "aud": mcp_audience(service_id, tool_name),
        "sub": str(user.id),
        "username": user.username,
        "department_id": user.department_id,
        "tv": int(user.token_version or 0),
        "assistant_id": assistant_id,
        "service_id": service_id,
        "tool": tool_name,
        "scope": "mcp:read",
        "type": "quick_mcp",
        "iat": now,
        "nbf": now,
        "exp": now + MCP_JWT_TTL_SECONDS,
        "jti": uuid.uuid4().hex,
    }
    material = active_signing_material(db)
    return jwt.encode(
        claims,
        material.private_pem,
        algorithm=ALGORITHM,
        headers={"kid": material.kid, "typ": "JWT"},
    )


def verify_mcp_token(
    token: str, *, service_id: int, tool_name: str, db=None
) -> dict | None:
    """院內共用驗證端；限定 issuer、audience、type、短效時間與真實 sub。"""
    claims = _claims_if_issuance_allowed(
        token, db, audience=mcp_audience(service_id, tool_name), issuer=MCP_JWT_ISSUER
    )
    if (
        not claims
        or claims.get("type") != "quick_mcp"
        or claims.get("scope") != "mcp:read"
    ):
        return None
    iat, exp = claims.get("iat"), claims.get("exp")
    sub = claims.get("sub")
    if (
        claims.get("aud") != mcp_audience(service_id, tool_name)
        or type(iat) is not int
        or type(exp) is not int
        or not 0 < exp - iat <= MCP_JWT_TTL_SECONDS
        or iat > time.time()
        or not isinstance(sub, str)
        or not sub.isdecimal()
        or int(sub) <= 0
        or claims.get("service_id") != service_id
        or claims.get("tool") != tool_name
        or type(claims.get("assistant_id")) is not int
        or claims["assistant_id"] < 0
        or not isinstance(claims.get("jti"), str)
        or not claims["jti"]
    ):
        return None
    return claims


def _live_user(db: Session, user: User) -> User:
    current = db.query(User).filter(User.id == user.id).populate_existing().first()
    if (
        not current
        or not current.is_active
        or not current.is_approved
        or current.role == "system"
    ):
        raise _error(403, "mcp_user_denied", "目前帳號不能使用 MCP 資料來源")
    return current


def _admin(db: Session, user: User) -> User:
    current = _live_user(db, user)
    if not is_admin_tier(current):
        raise _error(403, "mcp_admin_required", "只有平台管理員可以登錄或核准 MCP 服務")
    return current


def _can_use(db: Session, user: User, service: QuickMCPService) -> bool:
    if is_admin_tier(user) or service.is_public:
        return True
    if service.department_id is None:
        return False
    if user.department_id == service.department_id:
        return True
    scope = get_unit_admin_scope_ids(db, user)
    return scope is not None and service.department_id in scope


def _guard_endpoint(url: str) -> None:
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise _error(422, "mcp_endpoint_invalid", "服務位址格式不正確") from exc
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _error(422, "mcp_endpoint_invalid", "服務位址不能含帳密、查詢參數或片段")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _error(422, "mcp_endpoint_invalid", "服務位址必須是完整的 HTTP 或 HTTPS 位址")
    trusted = get_cached_hosts() | _env_trusted_hosts()
    host = parsed.hostname.lower()
    if host not in trusted:
        try:
            literal = ipaddress.ip_address(host)
            if not any(
                ipaddress.ip_address(item) == literal
                for item in trusted
                if ":" in item or item.replace(".", "").isdigit()
            ):
                raise ValueError
        except ValueError as exc:
            raise _error(
                422, "mcp_endpoint_untrusted", "MCP 服務必須先登錄為治理中心的院內信任主機"
            ) from exc
    try:
        validate_outbound_url(url, dns_timeout=3)
        # 中央驗證允許一般公開服務；快製平台的 MCP 額外限定院內私有位址。
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            addresses = [
                ipaddress.ip_address(info[4][0]) for info in _resolve_host(host, 3)
            ]
        networks = tuple(
            ipaddress.ip_network(n)
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
        )
        if not addresses or any(
            not any(addr.version == net.version and addr in net for net in networks)
            for addr in addresses
        ):
            raise ValueError("MCP 僅限院內私有位址")
    except (UnsafeEndpointError, ValueError, OSError, TimeoutError) as exc:
        raise _error(
            422, "mcp_endpoint_untrusted", "服務位址未通過平台出向安全驗證，請確認治理中心的信任主機"
        ) from exc


async def _guard_endpoint_async(url: str) -> None:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_guard_endpoint, url),
            timeout=MCP_ENDPOINT_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise _error(504, "mcp_endpoint_timeout", "MCP 服務位址的安全驗證逾時") from exc


class _MCPClient:
    """限定單一登錄端點，忽略伺服器要求與通知，禁止重導向。"""

    def __init__(self, endpoint: str, token: str):
        self.endpoint = endpoint
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "identity",
        }
        self.client = httpx.AsyncClient(
            timeout=MCP_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        )
        self.counter = 0

    async def __aenter__(self):
        try:
            result = await self.rpc(
                "initialize",
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "anila-quick-builder", "version": "1"},
                },
            )
            capabilities = result.get("capabilities")
            if (
                result.get("protocolVersion") != MCP_PROTOCOL_VERSION
                or not isinstance(capabilities, dict)
                or not isinstance(capabilities.get("tools"), dict)
            ):
                raise _error(502, "mcp_protocol_unsupported", "MCP 服務未提供相容的查詢工具協定")
            await self.rpc("notifications/initialized", {}, notification=True)
            return self
        except BaseException:
            await self.client.aclose()
            raise

    async def __aexit__(self, *args):
        await self.client.aclose()

    @staticmethod
    def _result(message, request_id):
        if (
            not isinstance(message, dict)
            or message.get("id") != request_id
            or message.get("jsonrpc") != "2.0"
        ):
            return None
        if "error" in message:
            raise _error(502, "mcp_remote_error", "MCP 服務拒絕處理查詢，請通知資料來源管理員")
        if not isinstance(message.get("result"), dict):
            raise _error(502, "mcp_response_invalid", "MCP 服務回覆格式不正確")
        return message["result"]

    async def rpc(
        self,
        method: str,
        params: dict,
        *,
        notification: bool = False,
        before_send: Callable[[], None] | None = None,
    ) -> dict:
        if method == "tools/call" and not callable(before_send):
            raise _error(403, "mcp_runtime_authorization_required", "MCP 查詢須重新確認流程授權")
        await _guard_endpoint_async(self.endpoint)
        self.counter += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.counter
        # DNS 等待完成後同步重驗；回呼至開始送出之間不可再等待。
        if before_send is not None:
            before_send()
        try:
            async with self.client.stream(
                "POST", self.endpoint, json=body, headers=self.headers
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise _error(502, "mcp_http_error", "MCP 服務連線失敗，請通知資料來源管理員")
                if (
                    response.headers.get("content-encoding", "identity").strip().lower()
                    != "identity"
                ):
                    raise _error(502, "mcp_response_encoding", "MCP 服務須提供未壓縮的有限大小回覆")
                session_id = response.headers.get("mcp-session-id")
                if method == "initialize" and session_id:
                    if len(session_id) > 256 or not all(
                        0x21 <= ord(c) <= 0x7E for c in session_id
                    ):
                        raise _error(502, "mcp_session_invalid", "MCP 服務回傳的連線編號不正確")
                    self.headers["Mcp-Session-Id"] = session_id
                if notification:
                    return {}
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip()
                )
                if content_type not in ("application/json", "text/event-stream"):
                    raise _error(502, "mcp_response_invalid", "MCP 服務須回傳 JSON 或 SSE 資料")
                data, buffer = bytearray(), b""
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise _error(
                            502, "mcp_response_too_large", "MCP 回覆超過大小上限，請縮小查詢範圍"
                        )
                    if content_type == "text/event-stream":
                        buffer += chunk
                        buffer = buffer.replace(b"\r\n", b"\n")
                        while b"\n\n" in buffer:
                            event, buffer = buffer.split(b"\n\n", 1)
                            payload = b"\n".join(
                                line[5:].lstrip(b" ")
                                for line in event.split(b"\n")
                                if line.startswith(b"data:")
                            )
                            if payload:
                                found = self._result(json.loads(payload), self.counter)
                                if found is not None:
                                    return found
                if content_type == "application/json":
                    found = self._result(json.loads(data), self.counter)
                    if found is not None:
                        return found
                raise _error(502, "mcp_response_invalid", "MCP 服務未回傳對應的查詢結果")
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise _error(504, "mcp_timeout", "MCP 查詢逾時，請稍後重試") from exc
        except (httpx.HTTPError, ValueError, UnicodeError, RecursionError) as exc:
            raise _error(502, "mcp_response_invalid", "MCP 服務連線或回覆格式不正確") from exc

    async def tools(self) -> list[dict]:
        tools, seen = [], set()
        cursor = None
        for _ in range(MAX_TOOLS):
            result = await self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            page = result.get("tools")
            if not isinstance(page, list):
                raise _error(502, "mcp_schema_invalid", "MCP 工具清單格式不正確")
            for raw in page:
                if (
                    not isinstance(raw, dict)
                    or not isinstance(raw.get("name"), str)
                    or not _TOOL_NAME.fullmatch(raw["name"])
                    or raw["name"] in seen
                    or raw["name"] == "__catalog__"
                ):
                    raise _error(502, "mcp_schema_invalid", "MCP 工具名稱不正確或重複")
                schema, description, annotations = (
                    raw.get("inputSchema"),
                    raw.get("description", ""),
                    raw.get("annotations", {}),
                )
                if (
                    not isinstance(schema, dict)
                    or schema.get("type") != "object"
                    or not isinstance(description, str)
                    or len(description) > 8000
                    or not isinstance(annotations, dict)
                ):
                    raise _error(502, "mcp_schema_invalid", "MCP 工具說明或參數格式不正確")
                _validate_schema(schema)
                tool = {
                    "name": raw["name"],
                    "description": description,
                    "inputSchema": schema,
                    "annotations": annotations,
                }
                if len(_json_bytes(tool)) > MAX_ARGUMENT_BYTES:
                    raise _error(502, "mcp_schema_too_large", "MCP 工具參數結構超過上限")
                seen.add(raw["name"])
                tools.append(tool)
            if len(tools) > MAX_TOOLS:
                raise _error(502, "mcp_too_many_tools", "MCP 工具數量超過上限")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tools
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor == cursor
            ):
                break
            cursor = next_cursor
        raise _error(502, "mcp_pagination_invalid", "MCP 工具分頁超過上限或格式不正確")


async def _discover(db, user, service) -> list[dict]:
    token = issue_mcp_token(db, user, service.id, "__catalog__", assistant_id=0)
    try:
        async with asyncio.timeout(MCP_TOTAL_SECONDS):
            async with _MCPClient(service.endpoint_url, token) as client:
                return await client.tools()
    except TimeoutError as exc:
        raise _error(504, "mcp_timeout", "MCP 工具擷取逾時") from exc


def _tool_dict(tool: QuickMCPTool) -> dict:
    return {
        "id": tool.id,
        "service_id": tool.service_id,
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "annotations": tool.annotations,
        "schema_fingerprint": tool.schema_fingerprint,
        "fingerprint": tool.schema_fingerprint,
        "approved_fingerprint": tool.approved_fingerprint,
        "is_read_only": tool.is_read_only,
        "is_approved": tool.is_approved,
    }


def serialize_service(db, service, user) -> dict:
    names = (
        "id",
        "name",
        "description",
        "endpoint_url",
        "department_id",
        "is_public",
        "status",
        "governance_revision",
        "registered_by_user_id",
        "approved_by_user_id",
        "approved_at",
        "created_at",
        "updated_at",
    )
    result = {
        **{name: getattr(service, name) for name in names},
        "tools": [
            _tool_dict(t)
            for t in db.query(QuickMCPTool)
            .filter_by(service_id=service.id)
            .order_by(QuickMCPTool.id)
        ],
    }
    result["endpoint_url"] = visible_endpoint_url(
        service.endpoint_url, db=db, caller=user
    )
    return result


def list_services(db: Session, user: User) -> list[dict]:
    user = _admin(db, user)
    return [
        serialize_service(db, s, user)
        for s in db.query(QuickMCPService).order_by(QuickMCPService.id)
    ]


async def register_service(db: Session, user: User, body: MCPServiceCreate) -> dict:
    user = _admin(db, user)
    require_endpoint_address_author(db, user)
    await _guard_endpoint_async(body.endpoint_url)
    user = _admin(db, user)
    require_endpoint_address_author(db, user)
    if body.department_id is not None:
        dept = (
            db.query(Department)
            .filter_by(id=body.department_id, is_active=True)
            .first()
        )
        if dept is None:
            raise _error(422, "mcp_department_invalid", "指定的單位不存在或已停用")
    service = QuickMCPService(**body.model_dump(), registered_by_user_id=user.id)
    db.add(service)
    try:
        db.flush()
        log_audit_event_or_raise(
            db,
            action="quick_mcp.register",
            resource_type="quick_mcp_service",
            resource_id=service.id,
            actor=user,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise _error(409, "mcp_service_exists", "此 MCP 服務位址已登錄") from exc
    except BaseException:
        db.rollback()
        raise
    try:
        return await refresh_service(db, user, service.id)
    except HTTPException as exc:
        db.rollback()
        return {**serialize_service(db, service, user), "discovery_error": exc.detail}


def _get_service(db, service_id):
    service = (
        db.query(QuickMCPService).filter_by(id=service_id).populate_existing().first()
    )
    if service is None:
        raise _error(404, "mcp_service_missing", "找不到 MCP 服務")
    return service


def _claim_governance(db: Session, service_id: int, revision: int, **values) -> int:
    """先以服務列 CAS 取得交易鎖，再修改工具；舊的網路結果不得寫回。"""
    next_revision = revision + 1
    result = db.execute(
        update(QuickMCPService)
        .where(
            QuickMCPService.id == service_id,
            QuickMCPService.governance_revision == revision,
        )
        .values(governance_revision=next_revision, **values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        raise _error(
            409,
            "mcp_governance_changed",
            "MCP 服務已有較新的治理操作，請重新整理後再審核",
        )
    return next_revision


async def refresh_service(db: Session, user: User, service_id: int) -> dict:
    user = _admin(db, user)
    service = _get_service(db, service_id)
    # 在網路擷取之前先撤銷；擷取失敗也不能繼續使用舊核准。
    revision = _claim_governance(
        db,
        service_id,
        service.governance_revision,
        status="pending",
        approved_by_user_id=None,
        approved_at=None,
    )
    db.query(QuickMCPTool).filter_by(service_id=service.id).update(
        {"is_approved": False, "is_read_only": False, "approved_fingerprint": None}
    )
    log_audit_event_or_raise(
        db,
        action="quick_mcp.refresh",
        resource_type="quick_mcp_service",
        resource_id=service.id,
        actor=user,
    )
    db.commit()
    snapshots = await _discover(db, user, service)
    user = _admin(db, user)
    _claim_governance(db, service_id, revision)
    service = _get_service(db, service_id)
    known = {
        tool.name: tool
        for tool in db.query(QuickMCPTool)
        .filter_by(service_id=service.id)
        .populate_existing()
    }
    for snapshot in snapshots:
        tool = known.get(snapshot["name"])
        if tool is None:
            tool = QuickMCPTool(service_id=service.id, name=snapshot["name"])
            db.add(tool)
        tool.description = snapshot["description"]
        tool.input_schema = snapshot["inputSchema"]
        tool.annotations = snapshot["annotations"]
        tool.schema_fingerprint = tool_fingerprint(snapshot)
        tool.is_approved = tool.is_read_only = False
        tool.approved_fingerprint = None
    db.commit()
    return serialize_service(db, service, user)


async def approve_service(
    db: Session, user: User, service_id: int, approved_tool_ids: list[int]
) -> dict:
    user = _admin(db, user)
    service = _get_service(db, service_id)
    revision = _claim_governance(db, service_id, service.governance_revision)
    tools = (
        db.query(QuickMCPTool)
        .filter_by(service_id=service.id)
        .populate_existing()
        .all()
    )
    selected = set(approved_tool_ids)
    if (
        not selected
        or len(selected) != len(approved_tool_ids)
        or not selected <= {t.id for t in tools}
    ):
        db.rollback()
        raise _error(422, "mcp_allowlist_invalid", "請明確選取此服務的唯讀查詢工具")
    expected = {
        tool.id: (tool.name, tool.schema_fingerprint)
        for tool in tools
        if tool.id in selected
    }
    # 不跨網路請求持有服務／工具鎖。版本號保留為不可變的區域變數。
    db.commit()
    current = {
        t["name"]: tool_fingerprint(t) for t in await _discover(db, user, service)
    }
    user = _admin(db, user)
    _claim_governance(
        db,
        service_id,
        revision,
        status="approved",
        approved_by_user_id=user.id,
        approved_at=datetime.now(timezone.utc),
    )
    service = _get_service(db, service_id)
    tools = (
        db.query(QuickMCPTool)
        .filter_by(service_id=service_id)
        .populate_existing()
        .all()
    )
    stored = {tool.id: (tool.name, tool.schema_fingerprint) for tool in tools}
    if any(
        stored.get(tool_id) != snapshot or current.get(snapshot[0]) != snapshot[1]
        for tool_id, snapshot in expected.items()
    ):
        db.rollback()
        raise _error(409, "mcp_schema_changed", "MCP 工具已變更，請重新登錄並審核工具內容")
    for tool in tools:
        tool.is_approved = tool.is_read_only = tool.id in selected
        tool.approved_fingerprint = (
            tool.schema_fingerprint if tool.is_approved else None
        )
    log_audit_event_or_raise(
        db,
        action="quick_mcp.approve",
        resource_type="quick_mcp_service",
        resource_id=service.id,
        actor=user,
        metadata={"approved_tool_ids": sorted(selected)},
    )
    db.commit()
    return serialize_service(db, service, user)


def disable_service(db: Session, user: User, service_id: int) -> dict:
    user = _admin(db, user)
    service = _get_service(db, service_id)
    _claim_governance(db, service_id, service.governance_revision, status="disabled")
    db.query(QuickMCPTool).filter_by(service_id=service.id).update(
        {"is_approved": False, "is_read_only": False, "approved_fingerprint": None}
    )
    log_audit_event_or_raise(
        db,
        action="quick_mcp.disable",
        resource_type="quick_mcp_service",
        resource_id=service.id,
        actor=user,
    )
    db.commit()
    service = _get_service(db, service_id)
    return serialize_service(db, service, user)


def list_tools(db: Session, user: User) -> list[dict]:
    user = _live_user(db, user)
    result = []
    for tool, service in (
        db.query(QuickMCPTool, QuickMCPService)
        .join(QuickMCPService)
        .filter(
            QuickMCPService.status == "approved",
            QuickMCPTool.is_approved.is_(True),
            QuickMCPTool.is_read_only.is_(True),
        )
        .order_by(QuickMCPTool.id)
    ):
        if tool.approved_fingerprint == tool.schema_fingerprint and _can_use(
            db, user, service
        ):
            result.append({**_tool_dict(tool), "service_name": service.name})
    return result


def _authorized_tool(db, user, tool_id, expected_fingerprint):
    user = _live_user(db, user)
    tool = db.query(QuickMCPTool).filter_by(id=tool_id).populate_existing().first()
    if not tool:
        raise _error(404, "mcp_tool_missing", "找不到可用的 MCP 工具")
    service = _get_service(db, tool.service_id)
    if (
        service.status != "approved"
        or not tool.is_approved
        or not tool.is_read_only
        or tool.approved_fingerprint != tool.schema_fingerprint
        or not _can_use(db, user, service)
    ):
        raise _error(403, "mcp_tool_denied", "MCP 工具未核准、已停用或不在目前授權範圍")
    if (
        expected_fingerprint is not None
        and expected_fingerprint != tool.schema_fingerprint
    ):
        raise _error(409, "mcp_version_changed", "助手核准時的 MCP 工具版本已變更，請重新送審")
    return user, tool, service


async def call_tool(
    db: Session,
    user: User,
    tool_id: int,
    arguments: dict,
    *,
    assistant_id: int,
    before_send: Callable[[], User],
    expected_fingerprint: str | None = None,
) -> dict:
    """回呼須同步重查流程、真實提問者、來源憑證與助手的當下授權。"""
    if not callable(before_send):
        raise _error(403, "mcp_runtime_authorization_required", "MCP 查詢須重新確認流程授權")
    if (
        type(assistant_id) is not int
        or assistant_id <= 0
        or type(tool_id) is not int
        or tool_id <= 0
    ):
        raise _error(422, "mcp_call_invalid", "助手與工具編號必須是正整數")
    if (
        not isinstance(arguments, dict)
        or len(_json_bytes(arguments)) > MAX_ARGUMENT_BYTES
    ):
        raise _error(422, "mcp_arguments_invalid", "MCP 查詢參數超過上限或格式不正確")
    if {"user_id", "sub", "actor_user_id", "requester_id"} & arguments.keys():
        raise _error(422, "mcp_identity_override", "查詢者身分由平台憑證提供，不能填入查詢參數")
    caller_id = user.id
    arguments = json.loads(_json_bytes(arguments))
    user, tool, service = _authorized_tool(db, user, tool_id, expected_fingerprint)
    _validate_schema(tool.input_schema)
    _validate_arguments(tool.input_schema, arguments)
    token = issue_mcp_token(db, user, service.id, tool.name, assistant_id=assistant_id)
    try:
        async with asyncio.timeout(MCP_TOTAL_SECONDS):
            async with _MCPClient(service.endpoint_url, token) as client:
                current = {t["name"]: tool_fingerprint(t) for t in await client.tools()}
                if current.get(tool.name) != tool.approved_fingerprint:
                    raise _error(409, "mcp_schema_changed", "MCP 工具已變更，請通知管理員重新審核")
                call_name, call_fingerprint = tool.name, tool.schema_fingerprint

                def authorize_send():
                    nonlocal user, tool, service
                    current_user = before_send()
                    if (
                        not isinstance(current_user, User)
                        or current_user.id != caller_id
                    ):
                        raise _error(403, "mcp_identity_changed", "MCP 查詢者身分不能在流程中變更")
                    user, tool, service = _authorized_tool(
                        db, current_user, tool_id, expected_fingerprint
                    )
                    if (
                        tool.name != call_name
                        or tool.schema_fingerprint != call_fingerprint
                        or current.get(tool.name) != tool.approved_fingerprint
                    ):
                        raise _error(409, "mcp_schema_changed", "MCP 工具已變更，請通知管理員重新審核")
                    if client.endpoint != service.endpoint_url:
                        raise _error(409, "mcp_endpoint_changed", "MCP 服務位址已變更，請重新送審")
                    client.headers[
                        "Authorization"
                    ] = f"Bearer {issue_mcp_token(db, user, service.id, tool.name, assistant_id=assistant_id)}"
                    log_audit_event_or_raise(
                        db,
                        action="quick_mcp.call",
                        resource_type="quick_mcp_tool",
                        resource_id=tool.id,
                        actor=user,
                        metadata={
                            "assistant_id": assistant_id,
                            "fingerprint": tool.schema_fingerprint,
                        },
                    )
                    db.commit()

                result = await client.rpc(
                    "tools/call",
                    {"name": call_name, "arguments": arguments},
                    before_send=authorize_send,
                )
    except TimeoutError as exc:
        raise _error(504, "mcp_timeout", "MCP 查詢逾時，請稍後重試") from exc
    # 只保留結果資料欄；伺服器不能提供身分、指令、模型訊息或新增工具。
    data = {
        key: result[key]
        for key in ("content", "structuredContent", "isError")
        if key in result
    }
    return {
        "tool_id": tool.id,
        "fingerprint": tool.schema_fingerprint,
        "trust": "untrusted_external_data",
        "data": data,
    }
