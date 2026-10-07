"""人資 Oracle 查詢。只做一件事：用固定的六個欄位查一個員工編號。

沒有自己的設定。連線參數由 CSP 每次請求帶來，用完即丟。
不記請求本文。thin mode，不呼叫 init_oracle_client。
"""
from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "0.0.0.0"
PORT = 8091
CONNECT_TIMEOUT_SECONDS = 5
CALL_TIMEOUT_MS = 5000
ROW_CAP = 32
MAX_BODY_BYTES = 16384
# 同時進行的查詢上限。多出來的直接回 503，避免一次打開太多 Oracle 連線。
MAX_IN_FLIGHT = 4
_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)

TABLE_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_$#]*(\.[A-Za-z][A-Za-z0-9_$#]*)?$"
)
COLUMNS = (
    "ovc_PNO",
    "ovc_NAME",
    "ovc_DEPT1_NAME",
    "ovc_DEPT2_NAME",
    "ovc_EMAIL",
    "ovc_DUTY_DS",
)
# 建表時若欄名加了雙引號，未加引號的查詢會 ORA-00904。只重試這六個常數。
QUOTED_COLUMNS = (
    '"ovc_PNO"',
    '"ovc_NAME"',
    '"ovc_DEPT1_NAME"',
    '"ovc_DEPT2_NAME"',
    '"ovc_EMAIL"',
    '"ovc_DUTY_DS"',
)
_ORA_CODE_RE = re.compile(r"ORA-\d+")
_KNOWN_PATHS = {"/health", "/lookup"}


def build_sql(table_name: str, *, quoted: bool = False) -> str:
    """固定查詢。table_name 必須先通過 TABLE_NAME_RE。員工編號只走 :pno。"""
    if not TABLE_NAME_RE.fullmatch(table_name or ""):
        raise ValueError("invalid table")
    cols = ", ".join(QUOTED_COLUMNS if quoted else COLUMNS)
    pno = '"ovc_PNO"' if quoted else "ovc_PNO"
    return f"SELECT {cols} FROM {table_name} WHERE {pno} = :pno"


def _scrub(text: str, password: str) -> str:
    if password and text and password in text:
        return text.replace(password, "（已隱藏）")
    return text


def _ora_code(text: str, password: str) -> str | None:
    scrubbed = _scrub(text or "", password)
    match = _ORA_CODE_RE.search(scrubbed)
    if match is None:
        return None
    code = match.group(0)
    if password and code in password:
        return None
    return code


def _error(error_type: str, message: str, oracle_code: str | None = None) -> dict:
    return {
        "ok": False,
        "error_type": error_type,
        "oracle_code": oracle_code,
        "message": message,
    }


def _cell(value):
    if value is None:
        return None
    if hasattr(value, "read"):
        try:
            value = value.read()
        except Exception:
            return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value)


def _classify(exc: BaseException, password: str, *, connected: bool) -> dict:
    text = _scrub(str(exc), password)
    code = _ora_code(str(exc), password)
    low = text.lower()
    if "timeout" in low or "timed out" in low or "dpy-4011" in low or "dpy-4027" in low:
        kind = "timeout"
        message = "人資查詢逾時"
    elif not connected:
        kind = "connect_failed"
        message = "無法連上人資資料庫"
    else:
        kind = "query_failed"
        message = "人資查詢失敗"
    if code:
        message = f"{message}（{kind}，{code}）"
    else:
        message = f"{message}（{kind}）"
    message = _scrub(message, password)
    return _error(kind, message, code)


def query_staff(payload: dict) -> dict:
    """跑固定查詢。成功回 rows；失敗回 error_type 與 Oracle 代碼，不含密碼。"""
    if not isinstance(payload, dict):
        return _error("bad_request", "查詢要求不完整（bad_request）")
    password = payload.get("password")
    password = password if isinstance(password, str) else ""
    table_name = payload.get("table_name")
    if not isinstance(table_name, str) or not TABLE_NAME_RE.fullmatch(table_name):
        return _error("invalid_table", "資料表名稱不符合規定（invalid_table）")
    employee_no = payload.get("employee_no")
    if (
        not isinstance(employee_no, str)
        or not employee_no.strip()
        or len(employee_no) > 64
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in employee_no)
    ):
        return _error("bad_request", "員工編號不符合規定（bad_request）")
    employee_no = employee_no.strip()
    host = payload.get("host")
    service_name = payload.get("service_name")
    db_user = payload.get("user")
    port = payload.get("port", 1521)
    if (
        not isinstance(host, str)
        or not host.strip()
        or len(host) > 255
        or not isinstance(service_name, str)
        or not service_name.strip()
        or len(service_name) > 128
        or not isinstance(db_user, str)
        or not db_user.strip()
        or len(db_user) > 128
        or len(password) > 256
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in host + service_name + db_user)
    ):
        return _error("bad_request", "連線參數不完整（bad_request）")
    try:
        port = int(port)
    except (TypeError, ValueError):
        return _error("bad_request", "連線參數不完整（bad_request）")
    if port < 1 or port > 65535:
        return _error("bad_request", "連線參數不完整（bad_request）")

    try:
        import oracledb  # 延遲載入：沒裝驅動時服務仍能回答 /health
    except ImportError:
        return _error(
            "driver_missing",
            "人資查詢服務缺少 Oracle 驅動，無法查詢（driver_missing）",
        )

    def _fetch(conn, sql: str):
        cursor = conn.cursor()
        try:
            cursor.execute(sql, {"pno": employee_no})
            return cursor.fetchmany(ROW_CAP + 1)
        finally:
            cursor.close()

    conn = None
    connected = False
    fetched = None
    try:
        conn = oracledb.connect(
            user=db_user.strip(),
            password=password,
            host=host.strip(),
            port=port,
            service_name=service_name.strip(),
            tcp_connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
        connected = True
        conn.call_timeout = CALL_TIMEOUT_MS
        try:
            fetched = _fetch(conn, build_sql(table_name, quoted=False))
        except Exception as exc:
            if _ora_code(str(exc), password) != "ORA-00904":
                raise
            fetched = _fetch(conn, build_sql(table_name, quoted=True))
    except Exception as exc:
        return _classify(exc, password, connected=connected)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                sys.stderr.write("hr-lookup close failed\n")

    if fetched is None:
        fetched = []
    if len(fetched) > ROW_CAP:
        return _error("too_many_rows", "人資回傳的列數超過上限（too_many_rows）")
    rows = []
    for raw in fetched:
        if len(raw) < len(COLUMNS):
            return _error("query_failed", "人資查詢失敗（query_failed）")
        row = {COLUMNS[i]: _cell(raw[i]) for i in range(len(COLUMNS))}
        pno = (row["ovc_PNO"] or "").strip()
        if pno != employee_no:
            return _error(
                "pno_mismatch",
                "人資回傳的員工編號與查詢不符（pno_mismatch）",
            )
        rows.append(row)
    return {"ok": True, "rows": rows}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def handle(self):
        try:
            self.connection.settimeout(10)
        except Exception:
            pass
        super().handle()

    def log_message(self, fmt, *args):
        path = self.path.split("?", 1)[0]
        if path in _KNOWN_PATHS:
            sys.stderr.write(f"{self.command} {path}\n")

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            self._send(404, _error("not_found", "沒有這個路徑（not_found）"))
            return
        self._send(200, {"status": "ok"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/lookup":
            self._send(404, _error("not_found", "沒有這個路徑（not_found）"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(400, _error("bad_request", "查詢要求不完整（bad_request）"))
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._send(400, _error("bad_request", "查詢要求不完整（bad_request）"))
            return
        if not _slots.acquire(blocking=False):
            self._send(503, _error("busy", "人資查詢忙碌，請稍後再試（busy）"))
            return
        try:
            result = query_staff(payload if isinstance(payload, dict) else {})
        finally:
            _slots.release()
        status = 200 if result.get("ok") else 400
        if result.get("error_type") in {"connect_failed", "query_failed", "timeout", "driver_missing", "too_many_rows", "pno_mismatch"}:
            status = 502
        self._send(status, result)


def serve(host: str = HOST, port: int = PORT) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
