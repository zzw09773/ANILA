"""院內差勤 MCP 範本：JWT 本人查詢、SQLite/PostgreSQL 唯讀連線。"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import socket
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

PROTOCOL_VERSION = "2025-03-26"
TOOL_NAME = "attendance_my_records"
TOOL = {
    "name": TOOL_NAME,
    "description": "查詢提問者本人指定月份的差勤紀錄，最多 100 筆。",
    "inputSchema": {
        "type": "object",
        "properties": {"month": {"type": "string", "minLength": 7, "maxLength": 7}},
        "required": ["month"],
        "additionalProperties": False,
    },
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


@dataclass(frozen=True)
class Settings:
    service_id: int
    jwks_url: str
    database: str = "sqlite"
    sqlite_path: Path | None = None
    postgres_service: str | None = None


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


def verify_jwt(token: str, jwks: dict, *, service_id: int, tool_name: str) -> dict:
    """只採平台公布的 RS256 active/retiring 公鑰，拒絕其他用途權杖。"""
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise ValueError
        key = next(k for k in jwks["keys"] if k.get("kid") == header["kid"])
        if (
            key.get("kty") != "RSA"
            or key.get("alg") != "RS256"
            or key.get("anila_key_state") not in ("active", "retiring")
        ):
            raise ValueError
        audience = f"urn:anila:quick-mcp:{service_id}:{tool_name}"
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer="anila-csp",
            options={
                "require_exp": True,
                "require_iat": True,
                "require_sub": True,
                "require_aud": True,
                "require_iss": True,
            },
        )
        iat, exp = claims["iat"], claims["exp"]
        if (
            claims.get("aud") != audience
            or claims.get("type") != "quick_mcp"
            or claims.get("scope") != "mcp:read"
            or claims.get("service_id") != service_id
            or claims.get("tool") != tool_name
            or type(iat) is not int
            or type(exp) is not int
            or not 0 < exp - iat <= 60
            or iat > time.time()
            or not isinstance(claims["sub"], str)
            or not claims["sub"].isdecimal()
            or int(claims["sub"]) <= 0
            or type(claims.get("assistant_id")) is not int
            or claims["assistant_id"] < 0
            or not isinstance(claims.get("jti"), str)
            or not claims["jti"]
        ):
            raise ValueError
        if key["anila_key_state"] == "retiring" and (
            type(key.get("anila_iat_not_after")) is not int
            or iat >= key["anila_iat_not_after"]
        ):
            raise ValueError
        return claims
    except (JWTError, ValueError, TypeError, KeyError, StopIteration) as exc:
        raise HTTPException(
            401, detail={"error_type": "invalid_identity", "message": "身分憑證無效或已過期"}
        ) from exc


def _private_jwks_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("JWKS 必須是院內平台的固定 HTTP(S) 位址")
    networks = [
        ipaddress.ip_network(n)
        for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    ]
    addresses = {
        ipaddress.ip_address(item[4][0])
        for item in socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    }
    if not addresses or any(
        not any(a.version == n.version and a in n for n in networks) for a in addresses
    ):
        raise ValueError("JWKS 位址必須解析到院內私有位址")


class Verifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def verify(self, token: str, tool_name: str) -> dict:
        try:
            async with asyncio.timeout(10):
                await run_in_threadpool(_private_jwks_url, self.settings.jwks_url)
                async with httpx.AsyncClient(
                    timeout=5,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client:
                    async with client.stream("GET", self.settings.jwks_url) as response:
                        if (
                            response.status_code != 200
                            or response.headers.get("content-encoding", "identity")
                            != "identity"
                        ):
                            raise ValueError
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > 64 * 1024:
                                raise ValueError
            return verify_jwt(
                token,
                json.loads(data),
                service_id=self.settings.service_id,
                tool_name=tool_name,
            )
        except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
            raise HTTPException(
                503,
                detail={"error_type": "identity_unavailable", "message": "暫時無法確認平台憑證"},
            ) from exc


def read_records(settings: Settings, subject: str, query: Query) -> list[dict]:
    """SQL 固定於程式；本人編號與查詢期間均使用綁定參數。"""
    start = date.fromisoformat(query.month + "-01")
    end = date(
        start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1
    )
    parameters = (int(subject), start.isoformat(), end.isoformat())
    sql = "SELECT work_date, status FROM attendance WHERE anila_user_id = {p} AND work_date >= {p} AND work_date < {p} ORDER BY work_date LIMIT 100"
    if settings.database == "sqlite":
        if settings.sqlite_path is None:
            raise ValueError("尚未指定 SQLite 檔案")
        uri = settings.sqlite_path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as connection:
            connection.execute("PRAGMA query_only = ON")
            deadline = time.monotonic() + 10
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 1000
            )
            rows = connection.execute(sql.format(p="?"), parameters).fetchall()
    elif settings.database == "postgresql":
        import psycopg2

        if not settings.postgres_service:
            raise ValueError("尚未指定 PostgreSQL libpq service")
        connection = None
        try:
            connection = psycopg2.connect(
                service=settings.postgres_service,
                connect_timeout=5,
                options="-c statement_timeout=10000",
            )
            connection.set_session(readonly=True, autocommit=False)
            with connection.cursor() as cursor:
                cursor.execute(sql.format(p="%s"), parameters)
                rows = cursor.fetchall()
        except psycopg2.Error as exc:
            raise RuntimeError("PostgreSQL 資料來源暫時無法查詢") from exc
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()
    else:
        raise ValueError("資料庫種類僅支援 sqlite 或 postgresql")
    return [{"work_date": str(row[0]), "status": row[1]} for row in rows]


def create_app(settings: Settings) -> FastAPI:
    if settings.service_id <= 0:
        raise ValueError("service_id 必須是治理中心指派的編號")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    verifier = Verifier(settings)
    app.state.verifier = verifier

    @app.get("/mcp")
    async def no_server_events():
        return Response(status_code=405)

    @app.post("/mcp")
    async def mcp(request: Request):
        if request.headers.get("origin"):
            raise HTTPException(403, detail="此服務只接受院內平台後端呼叫")
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, detail="缺少平台身分憑證")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 32 * 1024:
                raise HTTPException(413, detail="查詢參數超過上限")
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, detail="請提供 JSON-RPC 查詢") from exc
        if (
            not isinstance(body, dict)
            or body.get("jsonrpc") != "2.0"
            or not isinstance(body.get("params", {}), dict)
        ):
            raise HTTPException(400, detail="JSON-RPC 格式不正確")
        method, params = body.get("method"), body.get("params", {})
        # list/initialize 的 token 仍須完整驗簽，且 audience 只能是此服務。
        token = authorization[7:]
        if method == "tools/call":
            tool_name = params.get("name")
            if tool_name != TOOL_NAME:
                raise HTTPException(403, detail="此工具未開放")
        else:
            try:
                tool_name = jwt.get_unverified_claims(token).get("tool")
            except JWTError as exc:
                raise HTTPException(401, detail="身分憑證格式不正確") from exc
            if tool_name not in ("__catalog__", TOOL_NAME):
                raise HTTPException(403, detail="此憑證不適用此工具")
        claims = await verifier.verify(token, tool_name)
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "attendance-readonly", "version": "1"},
            }
        elif method == "notifications/initialized":
            return Response(status_code=202)
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            if claims["assistant_id"] <= 0:
                raise HTTPException(403, detail="工具查詢缺少助手編號")
            try:
                query = Query.model_validate(params.get("arguments", {}))
                # DB 同步查詢交給 threadpool，避免阻塞其他請求。
                records = await run_in_threadpool(
                    read_records, settings, claims["sub"], query
                )
            except (ValueError, sqlite3.Error) as exc:
                raise HTTPException(
                    422,
                    detail={"error_type": "query_invalid", "message": "查詢期間或資料來源設定不正確"},
                ) from exc
            except RuntimeError as exc:
                raise HTTPException(
                    503,
                    detail={
                        "error_type": "query_unavailable",
                        "message": "資料來源暫時無法查詢，請通知資料管理員",
                    },
                ) from exc
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(records, ensure_ascii=False)}
                ],
                "isError": False,
            }
        else:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32601, "message": "未提供此方法"},
                }
            )
        output = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        if len(json.dumps(output, ensure_ascii=False).encode()) > 256 * 1024:
            raise HTTPException(413, detail="查詢結果過大，請縮小查詢範圍")
        return output

    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="院內本人差勤 MCP 唯讀範本")
    parser.add_argument("--service-id", type=int, required=True)
    parser.add_argument("--jwks-url", required=True)
    parser.add_argument(
        "--database", choices=("sqlite", "postgresql"), default="sqlite"
    )
    parser.add_argument("--sqlite-path", type=Path)
    parser.add_argument("--postgres-service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    args = parser.parse_args()
    settings = Settings(
        args.service_id,
        args.jwks_url,
        args.database,
        args.sqlite_path,
        args.postgres_service,
    )
    uvicorn.run(create_app(settings), host=args.host, port=args.port)
