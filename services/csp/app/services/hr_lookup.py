"""向 hr-lookup 查一個人，並把回傳的列收成一筆 StaffRecord。

CSP 不載入 oracledb。呼叫端要先放開自己的資料庫連線，再呼叫 lookup_staff。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from anila_core.security.url_guard import UnsafeEndpointError, validate_outbound_url

HR_LOOKUP_URL = "http://hr-lookup:8091/lookup"
HTTP_TIMEOUT_SECONDS = 12
ROW_CAP = 32
DEPT_NAME_LIMIT = 100
PERSON_NAME_LIMIT = 100
EMAIL_LIMIT = 255

TABLE_NAME_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_$#]*(\.[A-Za-z][A-Za-z0-9_$#]*)?$"
)
_TITLE_SPLIT_RE = re.compile(r"[\s、，,;；/|]+")


class HrUnavailable(Exception):
    """人資查不到以外的失敗。登入要繼續，不能把這個例外丟給使用者。"""

    def __init__(self, error_type: str, message: str, oracle_code: str | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.oracle_code = oracle_code


@dataclass(frozen=True)
class HrConnection:
    host: str
    port: int
    service_name: str
    user: str
    password: str
    table_name: str


@dataclass(frozen=True)
class StaffRecord:
    employee_no: str
    name: str | None
    email: str | None
    dept1: str | None
    dept2: str | None
    titles: tuple[str, ...]


def normalize_text(value, limit: int) -> str | None:
    """去掉控制字元、收斂空白、截斷。不能用的值當成沒有。"""
    if value is None:
        return None
    text = str(value)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text[:limit]


def normalize_email(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > EMAIL_LIMIT:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in text):
        return None
    if text.count("@") != 1:
        return None
    local, _, domain = text.partition("@")
    if not local or "." not in domain:
        return None
    return text


def split_titles(value) -> list[str]:
    """一格裡可能有多個職稱。分隔符含空白與換行。"""
    if value is None:
        return []
    text = str(value)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text if ch not in "\n\r\t"):
        return []
    found: list[str] = []
    for part in _TITLE_SPLIT_RE.split(text.strip()):
        title = part.strip()
        if title and title not in found:
            found.append(title)
    return found


def oracle_probe_url(host: str, port: int) -> str:
    cleaned = (host or "").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        literal = cleaned
    elif ":" in cleaned:
        literal = f"[{cleaned}]"
    else:
        literal = cleaned
    return f"https://{literal}:{int(port)}/"


def enforce_oracle_host(host: str, port: int) -> None:
    """跟其他內網端點同一道信任主機檢查。Oracle 不是 HTTP，用 https URL 只為了檢查主機。"""
    validate_outbound_url(oracle_probe_url(host, port))


def _with_trust_hint(message: str) -> str:
    if "信任主機" in message:
        return message
    return f"{message}。請把這台主機加到信任主機"


def _scrub(text: str, password: str) -> str:
    if password and text and password in text:
        return text.replace(password, "（已隱藏）")
    return text


def staff_from_rows(employee_no: str, rows: list[dict] | None) -> StaffRecord | None:
    """列數為 0 回 None。姓名或單位不一致則不可用。職稱跨列收集。"""
    if not rows:
        return None
    if len(rows) > ROW_CAP:
        raise HrUnavailable("too_many_rows", "人資回傳的列數超過上限（too_many_rows）")
    identities: list[tuple[str | None, str | None, str | None]] = []
    titles: list[str] = []
    email: str | None = None
    for row in rows:
        if not isinstance(row, dict):
            raise HrUnavailable("row_disagreement", "人資回傳的內容無法採用（row_disagreement）")
        pno = str(row.get("ovc_PNO") or "").strip()
        if pno != employee_no:
            raise HrUnavailable(
                "pno_mismatch",
                "人資回傳的員工編號與查詢不符（pno_mismatch）",
            )
        name = normalize_text(row.get("ovc_NAME"), PERSON_NAME_LIMIT)
        dept1 = normalize_text(row.get("ovc_DEPT1_NAME"), DEPT_NAME_LIMIT)
        dept2 = normalize_text(row.get("ovc_DEPT2_NAME"), DEPT_NAME_LIMIT)
        if dept1 is None:
            dept2 = None
        identities.append((name, dept1, dept2))
        if email is None:
            email = normalize_email(row.get("ovc_EMAIL"))
        for title in split_titles(row.get("ovc_DUTY_DS")):
            if title not in titles:
                titles.append(title)
    first = identities[0]
    if any(item != first for item in identities[1:]):
        raise HrUnavailable(
            "row_disagreement",
            "同一員工編號的人資資料不一致（row_disagreement）",
        )
    return StaffRecord(
        employee_no=employee_no,
        name=first[0],
        email=email,
        dept1=first[1],
        dept2=first[2],
        titles=tuple(titles),
    )


def query_hr_service(connection: HrConnection, employee_no: str) -> list[dict]:
    body = {
        "host": connection.host,
        "port": connection.port,
        "service_name": connection.service_name,
        "user": connection.user,
        "password": connection.password,
        "table_name": connection.table_name,
        "employee_no": employee_no,
    }
    raw_body = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        HR_LOOKUP_URL,
        data=raw_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(1_000_000)
            status_ok = True
    except urllib.error.HTTPError as exc:
        raw = exc.read(1_000_000)
        status_ok = False
    except Exception as exc:
        text = _scrub(str(getattr(exc, "reason", exc)), connection.password)
        timed_out = "timed out" in text.lower() or "timeout" in text.lower()
        if timed_out:
            raise HrUnavailable("timeout", "人資查詢逾時（timeout）") from exc
        raise HrUnavailable("unreachable", "人資查詢服務無法連線（unreachable）") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HrUnavailable("unreachable", "人資查詢服務回應無法解讀（unreachable）") from exc
    if not isinstance(payload, dict):
        raise HrUnavailable("unreachable", "人資查詢服務回應無法解讀（unreachable）")
    if payload.get("ok") is True and status_ok:
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise HrUnavailable("unreachable", "人資查詢服務回應無法解讀（unreachable）")
        return rows
    error_type = str(payload.get("error_type") or "unavailable")
    if connection.password and connection.password in error_type:
        error_type = "unavailable"
    oracle_code = payload.get("oracle_code")
    if oracle_code is not None:
        oracle_code = str(oracle_code)
    if oracle_code and connection.password and (
        connection.password in oracle_code or oracle_code in connection.password
    ):
        oracle_code = None
    message = _scrub(str(payload.get("message") or "人資資料庫無法查詢"), connection.password)
    if error_type not in message:
        message = f"{message}（{error_type}）"
    raise HrUnavailable(error_type, message, oracle_code)


def lookup_staff(employee_no: str, connection: HrConnection) -> StaffRecord | None:
    """查這個員工編號。找不到回 None。連不上或資料不一致丟 HrUnavailable。"""
    try:
        enforce_oracle_host(connection.host, connection.port)
    except UnsafeEndpointError as exc:
        raise HrUnavailable("untrusted_host", _with_trust_hint(str(exc))) from exc
    if not TABLE_NAME_RE.fullmatch(connection.table_name or ""):
        raise HrUnavailable("invalid_table", "資料表名稱不符合規定（invalid_table）")
    rows = query_hr_service(connection, employee_no)
    return staff_from_rows(employee_no, rows)
