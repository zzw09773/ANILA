# -*- coding: utf-8 -*-
"""設定登錄表 —— 這個平台讀得到的每一顆設定，都在這裡宣告過一次。

為什麼要有這張表
================
2026-08-08 的環境變數全盤點戳破一件事：folklore 說的「41 顆」是 compose 的鍵
數，**csp 這個行程實際讀 95 顆**，其中 **57 顆從未出現在 compose**——它們吃程
式內的預設值，今天的管理員根本不知道它們存在。設定頁的價值因此有兩層：
**可見性**（95 顆全上畫面，包含那 57 顆隱形的）與 **可修性**（所有宣告都能從畫
面送出，差別只在生效時機與提醒）。這兩層都靠同一份宣告：**本檔是拼法、類別、
值域與預設值的唯一權威**。

宣告是「全部」不是「可編輯的那些」
==================================
A（祕密）與 SEC（安全類）也在表裡。overview 端點靠 ``setting_class`` 分區，
少宣告一顆 = 畫面上少一列 = 又一個看不見的開關。反過來，**零讀取點的變數不入
表**（盤點 §2.6 的 FLUX_* 三顆）：把一個沒有人讀的變數畫成活的，就是
``config.py:10-12`` 拿掉 ``ENABLE_API_DOCS`` 時寫下的那種病。

env 的字串怎麼讀，逐顆不同 —— 所以 ``value_type`` 是規則不是型別
================================================================
這個 codebase 至少有五種互不相容的真值判準，而且每一種都在跑：

* ``url_guard._env_flag``：``strip() == "1"``（SSRF 旗標族）
* ``zh_normalize_service``／``search_expansion``：``!= "0"``
* ``ocr.py``：``.lower() == "true"``
* ``card_auth``：``strip().lower() in ("1", "true", "yes")``
* ``config.py`` 那 53 顆：pydantic 的 bool 解析

⚠ 用一套「通用」的 bool 解析會出這種事：``ANILA_ALLOW_HTTP_ENDPOINT=true`` 時
url_guard 認定 **關**，設定頁卻顯示 **開**。那正是本包要消滅的形狀（畫面上的值
與生效的值不一致），出現在最不該出現的地方（SSRF 開關）。因此每一筆宣告都帶著
**它自己那個讀取點的字串規則**，``format``／``parse`` 成對，存進 DB 再讀回來必
須是同一個值。

值域函式（``domain_fn``）
=========================
**寫入端與解析端共用同一個函式物件**（不變式 2）。兩端各寫一份是
``6666fbc8`` 那個缺陷：``PUT`` 回 200、值真的存進 DB，解析端卻判定它不可用，
於是跑預設值、畫面說「沒有人校準過」——19 支測試全綠。

門檻那一顆是**別名**
====================
``institutional_kb.score_threshold`` 已經在 ``app/models/platform_setting.py``
以完整形狀落地（那組函式已關板，本檔一行不改）。這裡只是把它**重新宣告**進登
錄表，``domain_fn`` 直接指向範本那個函式物件本身——不是抄一份規則過來。它是唯
一 ``env_name is None`` 的條目：它本來就只住在 DB，沒有 env 回退層。
"""

from __future__ import annotations

import codecs
import ipaddress
import json
import re
from dataclasses import dataclass
from enum import Enum
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from anila_core.ingestion.ocr import _DEFAULT_VISION_PROMPT
from app.config import Settings
from app.models.platform_setting import (
    KB_THRESHOLD_DEFAULT,
    KB_THRESHOLD_KEY,
    _is_usable_kb_threshold,
)

# ⚠ 這一顆的預設值**不能寫死**。``config.py:125`` 是
# ``STATIC_DIR: str = str(Path(__file__).parent / "static")`` —— 一個隨安裝位置變動的
# **絕對**路徑。抄一個 ``"app/static"`` 進來，畫面上「程式預設」欄就會印出一個這個行程
# 從來沒有用過的相對路徑，而 ``main.py:460`` 是拿 CWD 去解相對路徑的：差別不是外觀，
# 是行為。直接取欄位宣告的那個值，宣告與現實就由**建構方式**保證是同一個。
# （其餘 52 顆 pydantic 欄位的預設值仍然逐顆字面宣告，由測試比對 —— 那才是真的核對。）
_STATIC_DIR_DEFAULT: str = Settings.model_fields["STATIC_DIR"].default


class UnknownSettingError(KeyError):
    """登錄表裡沒有這個 key。**猜一個回去比報錯危險。**"""


class SettingClass(Enum):
    """畫面上的分區與生效時機；是否可寫由這張 registry 統一宣告。

    * ``C``       —— 每請求讀 DB，改完下一個請求生效。
    * ``B_EDIT``  —— boot 讀定，但可以存進 DB、開機時覆蓋 env（重啟後生效）。
    * ``B_LOCKED``—— boot 讀定且有設計 §3.2 的具名提醒；仍可保存，但未必由 csp
      這次啟動流程套用。
    * ``SEC``     —— 安全類；仍可保存，寫入時必須通過該列的值域，安全語意與
      生效通道由 ``locked_reason`` 說明。
    * ``A``       —— 祕密。只有固定 username ``admin`` 可寫；畫面只顯示「已設定／
      未設定」，永不顯示值。
    """

    C = "C"
    B_EDIT = "B_EDIT"
    B_LOCKED = "B_LOCKED"
    SEC = "SEC"
    A = "A"


#: 設定頁的可寫類別全集。這個集合保留為 registry/API 的單一契約，不在端點或
#: 前端另抄一份 class 名單；A 類的帳號閘門另由 ``SECRET_WRITE_USERNAME`` 控制。
EDITABLE_CLASSES = frozenset(SettingClass)

#: 擁有者 Q45 指定的是**帳號名**，不是 role。不要改成 ``is_owner`` 或讀可變的
#: ``ADMIN_USERNAME``；那會把裁決的固定維運帳號悄悄變成另一套權限規則。
SECRET_WRITE_USERNAME = "admin"


# ── 字串規則：每一顆用它自己那個讀取點的判準 ──────────────────────────────


@dataclass(frozen=True)
class SettingType:
    """一顆設定的「值怎麼寫成字串、字串怎麼讀回值」。

    ``parse`` 與 ``format`` 必須互為反函數（測試逐顆釘）：存進去的「開啟」被讀
    成「關閉」是不會有錯誤訊息的那種壞掉。
    """

    name: str
    py_type: type
    parse: Callable[[str], Any]
    format: Callable[[Any], str]


def _parse_int(raw: str) -> int:
    return int(raw.strip())


def _parse_float(raw: str) -> float:
    return float(raw.strip())


def _parse_str(raw: str) -> str:
    return raw


_PYDANTIC_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_PYDANTIC_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


def _parse_pydantic_bool(raw: str) -> bool:
    """``config.py`` 那 53 顆走的判準（pydantic v2 的 bool 解析）。"""
    token = raw.strip().lower()
    if token in _PYDANTIC_TRUE:
        return True
    if token in _PYDANTIC_FALSE:
        return False
    raise ValueError(f"不是可以解讀的布林值：{raw!r}")


def _parse_flag_eq_1(raw: str) -> bool:
    """``anila_core.security.url_guard._env_flag``：``strip() == "1"``。

    ⚠ ``"true"`` 在這裡是 **False**。畫面若用別的判準去讀，管理員會看到一個
    他以為打開了的 SSRF 旗標。
    """
    return raw.strip() == "1"


def _parse_flag_ne_0(raw: str) -> bool:
    """``zh_normalize_service._enabled``／``search_expansion``：``!= "0"``。"""
    return raw != "0"


def _parse_lower_eq_true(raw: str) -> bool:
    """``anila_core.ingestion.ocr``：``.lower() == "true"``（``"1"`` 不算開啟）。"""
    return raw.lower() == "true"


_CARD_TRUTHY = frozenset({"1", "true", "yes"})


def _parse_card_truthy(raw: str) -> bool:
    """``card_auth:109``（``CARD_DEV_TRUST_TEST_CA``）：``strip().lower() in (...)``。"""
    return raw.strip().lower() in _CARD_TRUTHY


def _parse_card_truthy_nostrip(raw: str) -> bool:
    """``card_auth:121``（``CARD_DEV_SKIP_NONCE_BINDING``）：``lower() in (...)``，**沒有 strip**。

    ⚠ 兩顆是兄弟旗標，判準卻差一個 ``strip()``。共用「差不多」的那一份規則會讓
    ``CARD_DEV_SKIP_NONCE_BINDING=" true "`` 在畫面上顯示成已開啟，而模組層那個
    flag 其實是關的 —— 一個 SEC 類（反 replay 綁定）的顯示謊言。差一個字的規則
    要各自宣告，不是四捨五入成同一個。
    """
    return raw.lower() in _CARD_TRUTHY


def _format_flag_eq_1(value: bool) -> str:
    return "1" if value else "0"


def _format_lowercase_bool(value: bool) -> str:
    return "true" if value else "false"


T_STR = SettingType("str", str, _parse_str, str)
T_INT = SettingType("int", int, _parse_int, lambda v: str(int(v)))
T_FLOAT = SettingType("float", float, _parse_float, lambda v: repr(float(v)))
T_BOOL_PYDANTIC = SettingType(
    "bool(pydantic)", bool, _parse_pydantic_bool, _format_lowercase_bool
)
T_BOOL_EQ_1 = SettingType("bool(== '1')", bool, _parse_flag_eq_1, _format_flag_eq_1)
T_BOOL_NE_0 = SettingType("bool(!= '0')", bool, _parse_flag_ne_0, _format_flag_eq_1)
T_BOOL_LOWER_TRUE = SettingType(
    "bool(lower() == 'true')", bool, _parse_lower_eq_true, _format_lowercase_bool
)
T_BOOL_CARD_TRUTHY = SettingType(
    "bool(strip, in 1/true/yes)", bool, _parse_card_truthy, _format_lowercase_bool
)
T_BOOL_CARD_TRUTHY_NOSTRIP = SettingType(
    "bool(no strip, in 1/true/yes)", bool, _parse_card_truthy_nostrip, _format_lowercase_bool
)


# ── 值域函式 ───────────────────────────────────────────────────────────────


_MAX_SETTING_TEXT_LENGTH = 2048
_MAX_PATH_LENGTH = 1024
_MAX_MODEL_NAME_LENGTH = 256
_MAX_SECRET_JSON_LENGTH = 65536
_EMPLOYEE_ID_RE = re.compile(r"\A\d{6,9}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@+\-/]{0,255}\Z")
_OCR_LANGUAGE_RE = re.compile(r"\A[A-Za-z0-9_-]{2,32}\Z")
_HOST_LABEL_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN ED25519 PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
)


def _is_safe_text(
    value: Any,
    *,
    allow_empty: bool = False,
    max_length: int = _MAX_SETTING_TEXT_LENGTH,
    reject_edge_whitespace: bool = True,
) -> bool:
    """Reject values that cannot safely survive an env/DB/runtime round-trip."""
    if not isinstance(value, str):
        return False
    if not value:
        return allow_empty
    if len(value) > max_length or value.strip() == "":
        return False
    if reject_edge_whitespace and value != value.strip():
        return False
    return "\x00" not in value and "\r" not in value and "\n" not in value


def _is_token(value: Any, *, max_length: int = _MAX_SETTING_TEXT_LENGTH) -> bool:
    if not _is_safe_text(value, max_length=max_length):
        return False
    return bool(_TOKEN_RE.fullmatch(value))


def _is_host_token(value: Any, *, allow_wildcard: bool = False) -> bool:
    if not _is_safe_text(value, max_length=253, reject_edge_whitespace=False):
        return False
    token = value.strip().lower().rstrip(".")
    if not token or "*" in token:
        if not (allow_wildcard and token.startswith("*.") and token.count("*") == 1):
            return False
        token = token[2:]
    if not token or "/" in token or "://" in token:
        return False
    if token.startswith("[") and token.endswith("]"):
        token = token[1:-1]
    try:
        ipaddress.ip_address(token)
        return True
    except ValueError:
        pass
    if len(token) > 253:
        return False
    labels = token.split(".")
    return all(_HOST_LABEL_RE.fullmatch(label) for label in labels)


def _is_host_entry(
    value: str, *, allow_wildcard: bool = False, allow_port: bool = False
) -> bool:
    entry = value.strip()
    host = entry
    if allow_port:
        if entry.startswith("["):
            closing = entry.find("]")
            if closing < 0:
                return False
            host = entry[: closing + 1]
            suffix = entry[closing + 1 :]
            if suffix:
                if not suffix.startswith(":") or not suffix[1:].isdigit():
                    return False
                port = int(suffix[1:])
                if not 1 <= port <= 65535:
                    return False
        elif entry.count(":") == 1:
            host, port_text = entry.rsplit(":", 1)
            if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
                return False
    if allow_wildcard and host.startswith("*.") and host != entry:
        return False
    return _is_host_token(host, allow_wildcard=allow_wildcard)


def _is_host_csv(
    value: Any, *, allow_wildcard: bool = False, allow_port: bool = False
) -> bool:
    if not _is_safe_text(value, reject_edge_whitespace=False):
        return False
    entries = [part.strip() for part in value.split(",")]
    if not entries or any(not entry for entry in entries):
        return False
    if allow_wildcard and entries == ["*"]:
        return True
    return all(
        _is_host_entry(
            entry,
            allow_wildcard=allow_wildcard,
            allow_port=allow_port,
        )
        for entry in entries
    )


def _is_trusted_host_csv(value: Any) -> bool:
    return _is_host_csv(value, allow_wildcard=False)


def _is_allowed_host_csv(value: Any) -> bool:
    # Starlette's TrustedHostMiddleware compares host names, not host:port
    # patterns.  Keep the registry grammar aligned with parse_allowed_hosts;
    # accepting a port here would create a stored value that never matches.
    return _is_host_csv(value, allow_wildcard=True, allow_port=False)


def _is_origin_csv(value: Any) -> bool:
    if not _is_safe_text(value, reject_edge_whitespace=False):
        return False
    entries = [part.strip() for part in value.split(",")]
    if not entries or any(not entry for entry in entries):
        return False
    for entry in entries:
        try:
            parsed = urlsplit(entry)
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not _is_host_token(parsed.hostname)
            or (port is not None and not 1 <= port <= 65535)
        ):
            return False
    return True


def _is_initial_owner_csv(value: Any) -> bool:
    if not _is_safe_text(value, reject_edge_whitespace=False):
        return False
    entries = [part.strip() for part in value.split(",")]
    return bool(entries) and all(_EMPLOYEE_ID_RE.fullmatch(entry) for entry in entries)


def _is_ocr_languages(value: Any) -> bool:
    if not _is_safe_text(value, reject_edge_whitespace=False):
        return False
    entries = [part.strip() for part in value.split(",")]
    return bool(entries) and all(_OCR_LANGUAGE_RE.fullmatch(entry) for entry in entries)


def _is_model_name(value: Any) -> bool:
    if not _is_safe_text(value, max_length=_MAX_MODEL_NAME_LENGTH):
        return False
    # Model identifiers may contain an organisation slash, but path traversal
    # and display-name whitespace are not model identifiers consumed by OCR.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value)) and ".." not in value


def _is_email_address(value: Any) -> bool:
    if not _is_safe_text(value, max_length=320):
        return False
    display, address = parseaddr(value)
    if display or address != value or address.count("@") != 1:
        return False
    local, host = address.rsplit("@", 1)
    return bool(local) and _is_host_token(host)


def _is_existing_file_path(value: Any) -> bool:
    if not _is_safe_text(value, max_length=_MAX_PATH_LENGTH):
        return False
    try:
        path = Path(value)
        if not path.is_file():
            return False
        with path.open("rb"):
            return True
    except (OSError, ValueError):
        return False


def _is_existing_directory_path(value: Any) -> bool:
    if not _is_safe_text(value, max_length=_MAX_PATH_LENGTH):
        return False
    try:
        return Path(value).is_dir()
    except (OSError, ValueError):
        return False


def _is_certificate_bundle_path(value: Any) -> bool:
    """A csp-visible, readable PEM certificate bundle without private keys."""
    if not _is_safe_text(value, max_length=_MAX_PATH_LENGTH):
        return False
    try:
        pem_bytes = Path(value).read_bytes()
    except (OSError, ValueError):
        return False
    if any(marker in pem_bytes for marker in _PRIVATE_KEY_MARKERS):
        return False
    try:
        from cryptography import x509

        loader = getattr(x509, "load_pem_x509_certificates", None)
        if loader is not None:
            certificates = loader(pem_bytes)
        else:  # pragma: no cover - compatibility with older cryptography wheels
            marker = b"-----BEGIN CERTIFICATE-----"
            end = b"-----END CERTIFICATE-----"
            certificates = []
            start = 0
            while True:
                begin = pem_bytes.find(marker, start)
                if begin < 0:
                    break
                finish = pem_bytes.find(end, begin)
                if finish < 0:
                    return False
                block = pem_bytes[begin : finish + len(end)] + b"\n"
                certificates.append(x509.load_pem_x509_certificate(block))
                start = finish + len(end)
        return bool(certificates)
    except (AttributeError, ValueError, TypeError):
        return False


def _is_csv(value: Any) -> bool:
    """通用的非空 CSV；專用安全清單再用各自的語法函式。"""
    if not _is_safe_text(value, reject_edge_whitespace=False):
        return False
    parts = [part.strip() for part in value.split(",")]
    return bool(parts) and all(parts)


def _is_url_or_empty(value: Any) -> bool:
    """可選的 HTTP(S) URL；不在這裡放寬 host/IP，SSRF guard 仍是另一道門。"""
    if not _is_safe_text(value, allow_empty=True, reject_edge_whitespace=False):
        return False
    if value == "":
        return True
    if not value.strip():
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_redis_url(value: Any) -> bool:
    """Redis 連線字串；密碼與 host 的安全性仍由連線層及部署環境負責。"""
    if not _is_safe_text(value, reject_edge_whitespace=False):
        return False
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return parsed.scheme in {"redis", "rediss"} and bool(parsed.netloc)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _one_of(*choices: str) -> Callable[[Any], bool]:
    allowed = frozenset(choices)

    def _matches(value: Any) -> bool:
        return value in allowed

    _matches.__doc__ = f"只能是：{'、'.join(choices)}。"
    _matches.choices = allowed
    return _matches


def _closed_int_range(low: int, high: int) -> Callable[[Any], bool]:
    def _in_range(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high

    _in_range.__doc__ = f"整數，閉區間 [{low}, {high}]。"
    # ⚠ 掛在函式上而不是複製到別處：測試靠它反問「有值域的條目，說明文字有沒有把
    # 值域寫出來」。值域的**唯一**來源仍然是這個閉包本身。
    _in_range.bounds = (low, high)
    return _in_range


def _closed_float_range(low: float, high: float) -> Callable[[Any], bool]:
    def _in_range(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and (
            low <= float(value) <= high
        )

    _in_range.__doc__ = f"數值，閉區間 [{low}, {high}]。"
    _in_range.bounds = (low, high)
    return _in_range


def _is_supported_text_encoding(value: Any) -> bool:
    """空字串（＝用程式內建的 cp950/gbk 序）或 Python 認得的碼頁名。

    填一個不存在的碼頁只會讓解檔名整串靜默落回原值，管理員不會知道自己填錯。
    """
    if not isinstance(value, str):
        return False
    if value == "":
        return True
    try:
        codecs.lookup(value)
    except LookupError:
        return False
    return True


def _is_non_empty_str(value: Any) -> bool:
    return _is_safe_text(value)


def _is_str(value: Any) -> bool:
    return _is_safe_text(value)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _json_list_of_objects(
    value: Any,
    *,
    allow_empty: bool = True,
    max_length: int = _MAX_SECRET_JSON_LENGTH,
    required_non_empty: tuple[str, ...] = (),
    required_strings: tuple[str, ...] = (),
    string_list_fields: tuple[str, ...] = (),
) -> bool:
    """Validate the shape consumed by the startup seed readers.

    An empty string means "no seed" and remains valid.  The seed consumers
    index required fields and iterate optional model/agent lists directly; a
    JSON string or an object with missing fields therefore has the same
    dangerous shape as invalid JSON: the setting can be stored while startup
    quietly skips the intended work.
    """
    if not _is_safe_text(
        value,
        allow_empty=allow_empty,
        max_length=max_length,
        reject_edge_whitespace=False,
    ):
        return False
    if not value.strip():
        return allow_empty
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, list):
        return False
    for item in parsed:
        if not isinstance(item, dict):
            return False
        for key in required_non_empty:
            if not isinstance(item.get(key), str) or not item[key].strip():
                return False
        for key in required_strings:
            if not isinstance(item.get(key), str):
                return False
        for key in string_list_fields:
            entries = item.get(key, [])
            if not isinstance(entries, list) or not all(
                isinstance(entry, str) for entry in entries
            ):
                return False
    return True


def _is_seed_models(value: Any) -> bool:
    return _json_list_of_objects(
        value,
        required_non_empty=("name",),
        required_strings=("endpoint_url",),
    )


def _is_seed_agents(value: Any) -> bool:
    return _json_list_of_objects(
        value,
        required_non_empty=("name",),
        required_strings=("endpoint_url",),
    )


def _is_seed_links(value: Any) -> bool:
    return _json_list_of_objects(
        value,
        required_non_empty=("name", "url"),
    )


def _is_seed_api_keys(value: Any) -> bool:
    return _json_list_of_objects(
        value,
        required_non_empty=("username", "key"),
        string_list_fields=("models", "agents"),
    )


# ── 條目 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SettingSpec:
    """一顆設定的完整宣告。**欄位全必填** —— 省略一欄就等於少一段畫面上的話。"""

    #: 點號命名空間。設定頁靠前綴分群，所以群名跟著**消費模組**走，不是跟著
    #: env 名的字首走（``ANILA_ACTION_*`` 兩顆都在 ``limits.``，因為消費它們的
    #: 是同一個模組）。
    key: str
    #: 對應的環境變數名。回退鏈用。``None`` = 這顆本來就只住在 DB。
    env_name: str | None
    setting_class: SettingClass
    #: 值怎麼寫成字串、字串怎麼讀回值 —— 用**這顆自己那個讀取點**的規則。
    value_type: SettingType
    #: 值域判準。**寫入端與解析端共用這一個函式物件**（不變式 2）。
    domain_fn: Callable[[Any], bool]
    #: 程式內的預設值（env 與 DB 都沒有時真正生效的那個）。
    default: Any
    #: 改了要不要重啟才生效。照讀取點的實際時機填，不照類別猜。
    restart_required: bool
    #: 上畫面的繁中說明。可編輯的條目要在這裡把值域講成人話 —— 被拒時的錯誤
    #: 訊息會原樣送到畫面上。
    description: str
    #: 提醒理由（B_LOCKED／SEC／A 必填，其餘必須是空字串）。這句話會原樣上畫面；
    #: A 類帳號不符時，API 會另外補上固定 username 閘門的理由。
    locked_reason: str


def _spec(
    key: str,
    env_name: str | None,
    setting_class: SettingClass,
    value_type: SettingType,
    domain_fn: Callable[[Any], bool],
    default: Any,
    restart_required: bool,
    description: str,
    locked_reason: str = "",
) -> SettingSpec:
    return SettingSpec(
        key=key,
        env_name=env_name,
        setting_class=setting_class,
        value_type=value_type,
        domain_fn=domain_fn,
        default=default,
        restart_required=restart_required,
        description=description,
        locked_reason=locked_reason,
    )


_SECRET_REASON = (
    "祕密類 —— 畫面只顯示已設定／未設定，值永不出後端；寫入僅限 username=admin。"
    "合法值會在下一次 CSP 開機重新驗證後同步到該行程的 Settings／env；"
    "若消費者在其他行程或 import 時已凍結，仍須依本列提醒走對應部署通道，"
    "不能只按儲存就視為所有 consumer 都已套用"
)
_SEC_REASON = (
    "安全類 —— 可保存但要先想清楚安全影響；值域由本列 domain_fn 把關，"
    "合法值會在下一次 CSP 開機重新驗證後同步到該行程的 Settings／env；"
    "若讀取點在其他行程或 import 時已凍結，請循本列提醒變更 consumer 使用的來源"
)
_SEC_CARD_REASON = (
    "安全類 —— 卡登信任鏈，改錯等於放行偽卡。"
    "合法值會在下一次 CSP 開機重新驗證後同步；卡片驗章仍會重新檢查信任錨與 dev 閘門"
)
_SEC_TICKET_REASON = (
    "安全類 —— 延長票期等於延長被竊 token 的有效期（設計 §3.2）。"
    "合法值會在下一次 CSP 開機重新驗證後同步；改動前請確認票期政策與重啟結果"
)
_SMTP_REASON = (
    "SMTP_HOST 是出向連線目標＝SSRF 鄰接面，且 relay 方案未定（設計 §3.2）。"
    "合法值會在下一次 CSP 開機重新驗證後同步到 Settings／env"
)
# Task 4 的 C1 裁決。這些提醒現在仍然保留，但語意從「不能保存」改成「CSP boot
# bridge 能同步到哪裡，以及哪個 consumer 仍然早於它」。逐顆的證據在
# tests/test_settings_boot_override.py 的 BOOT_CHANNEL_EVIDENCE 表。
_BOOT_ORDER_REASON = (
    "CSP 會在開機重新驗證後同步這顆，但 consumer 在 import 期已讀走，"
    "所以已建立的 engine／mount／模組常數不會被回溯改寫；要改該 consumer 請走部署通道"
)
# 另一種情況：值不住在 ``Settings`` 上，讀取點直接讀 os.environ。現在 boot bridge
# 會同步這個 env，但不會替它創造一個 Settings 欄位；若讀取點早於 bridge，仍需走部署通道。
_ENV_ONLY_CHANNEL_REASON = (
    "讀取點直接讀 os.environ、Settings 上沒有這個欄位；CSP 開機會同步合法 DB 值到該 env，"
    "但不會替早於 bridge 的 consumer 回溯改寫"
)
_INTERPRETER_REASON = (
    "CSP 會同步合法 DB 值到 env，但這個旗標由 CPython 在行程啟動時消費，"
    "早於一切應用程式碼，執行中的 interpreter 不會被回溯改寫"
)
_BOOT_SYNC_REASON = (
    "CSP 會在下一次開機重新驗證後同步合法值到 Settings／env；"
    "若實際 consumer 在其他行程，仍須同步該行程的部署設定"
)


def _compose_hint(env_name: str) -> str:
    """提醒 import-time／interpreter consumer 的真正部署通道。"""
    return (
        f"。改法：在 compose 的 csp 服務 environment 設 {env_name}"
        "（現在沒有這一行就自己加），改完 up -d 重建容器"
    )


_OCR_REASON = (
    "CSP 會同步合法值到自己的 Settings／env；主要消費者是 ingestion-worker（另一行程，"
    "讀不到 csp DB），因此要改 worker 的實際行為仍需 worker 側設定通道（設計 §3.2）"
)


SETTINGS: tuple[SettingSpec, ...] = (
    # ── app.* —— 平台自我描述 ────────────────────────────────────────────
    _spec("app.name", "APP_NAME", SettingClass.B_EDIT, T_STR, _is_non_empty_str,
          "CSP Platform", True, "平台名稱（顯示用）。"),
    _spec("app.version", "APP_VERSION", SettingClass.B_EDIT, T_STR, _is_non_empty_str,
          "1.0.0", True, "平台版本字串（顯示用，anila-studio 也讀）。"),
    _spec("app.debug", "DEBUG", SettingClass.B_LOCKED, T_BOOL_PYDANTIC, _is_bool,
          False, True, "FastAPI 除錯姿態。內網正式部署一律關閉。"
          "⚠ 唯一讀取點是 database.py:10 的 engine ``echo``，engine 在 import 期就建好了。",
          _BOOT_ORDER_REASON + _compose_hint("DEBUG")),
    _spec("app.site_url", "SITE_URL", SettingClass.B_EDIT, T_STR, _is_non_empty_str,
          "http://localhost", True, "平台對外網址，產生連結時用。"),
    _spec("app.static_dir", "STATIC_DIR", SettingClass.B_LOCKED, T_STR, _is_non_empty_str,
          _STATIC_DIR_DEFAULT, True,
          "靜態檔目錄。import 期由 config.py 算成絕對路徑，改了要重建容器；"
          "填相對路徑的話是相對於行程的工作目錄解析的。",
          _BOOT_ORDER_REASON + _compose_hint("STATIC_DIR")),
    _spec("app.host", "ANILA_HOST", SettingClass.B_LOCKED, T_STR, _is_host_token,
          "", True, "部署主機名。csp 本身不消費，只在開機檢查它不是 placeholder。",
          _ENV_ONLY_CHANNEL_REASON + _compose_hint("ANILA_HOST")),
    _spec("app.python_unbuffered", "PYTHONUNBUFFERED", SettingClass.B_LOCKED, T_STR,
          _one_of("0", "1"), "", True,
          "CPython 的 stdout 緩衝旗標，由直譯器消費，不經應用程式。",
          _INTERPRETER_REASON + _compose_hint("PYTHONUNBUFFERED")),

    # ── db.* ─────────────────────────────────────────────────────────────
    _spec("db.url", "DATABASE_URL", SettingClass.A, T_STR, _is_str,
          "postgresql://csp:csp_password@localhost:5432/csp", True,
          "主資料庫 DSN（內嵌帳密）。", _SECRET_REASON),
    _spec("db.migration_url", "MIGRATION_DATABASE_URL", SettingClass.A, T_STR, _is_str,
          "", True, "跑 migration 用的高權限 DSN，開機後即棄。", _SECRET_REASON),
    _spec("db.app_role_password", "CSP_APP_DB_PASSWORD", SettingClass.A, T_STR, _is_str,
          "csp", True, "runtime 使用的 csp_app role 密碼（migration 0014 建 role 時用）。",
          _SECRET_REASON),
    _spec("db.legacy_sqlite_path", "LEGACY_SQLITE_PATH", SettingClass.B_LOCKED, T_STR,
          _is_existing_file_path, "", True,
          "舊 SQLite 資料檔位置；未設時走程式內建的候選路徑清單。"
          "⚠ 讀取點是 startup_migrations 的 ``os.environ``（不是 Settings 欄位），"
          "時機雖然在覆蓋之後，通道卻接不上。",
          _ENV_ONLY_CHANNEL_REASON + _compose_hint("LEGACY_SQLITE_PATH")),

    # ── auth.* ───────────────────────────────────────────────────────────
    _spec("auth.secret_key", "SECRET_KEY", SettingClass.A, T_STR, _is_str,
          "your-secret-key-change-this-in-production", True,
          "對稱祕密（憑證加密與開機檢查用；access/refresh 已走 RS256）。", _SECRET_REASON),
    _spec("auth.secret_key_fallback", "CSP_SECRET_KEY", SettingClass.A, T_STR, _is_str,
          "", False, "SECRET_KEY 缺席時的憑證加密備援來源。", _SECRET_REASON),
    _spec("auth.jwt_algorithm", "ALGORITHM", SettingClass.SEC, T_STR,
          _one_of("HS256", "RS256"), "HS256", True,
          "legacy JWT 演算法欄位；只允許 HS256 或 RS256，實際簽發走 RS256。", _SEC_REASON),
    _spec("auth.access_token_expire_minutes", "ACCESS_TOKEN_EXPIRE_MINUTES",
          SettingClass.SEC, T_INT, _positive_int, 60, True,
          "access token 有效分鐘數。必須是正整數。", _SEC_TICKET_REASON),
    _spec("auth.refresh_token_expire_days", "REFRESH_TOKEN_EXPIRE_DAYS",
          SettingClass.SEC, T_INT, _positive_int, 30, True,
          "refresh token 有效天數。必須是正整數。", _SEC_TICKET_REASON),
    _spec("auth.jwt_private_key_path", "JWT_PRIVATE_KEY_PATH", SettingClass.SEC, T_STR,
          _is_non_empty_str, "secrets/jwt-private.pem", True, "RS256 簽章私鑰位置。", _SEC_REASON),
    _spec("auth.jwt_public_key_path", "JWT_PUBLIC_KEY_PATH", SettingClass.SEC, T_STR,
          _is_non_empty_str, "secrets/jwt-public.pem", True, "JWKS 公開的公鑰位置。", _SEC_REASON),
    _spec("auth.jwt_kid", "JWT_KID", SettingClass.SEC, T_STR, _is_non_empty_str,
          "anila-v1", True, "JWT 金鑰識別碼；三個服務必須同值。", _SEC_REASON),
    _spec("auth.allow_auto_keygen", "ALLOW_AUTO_KEYGEN", SettingClass.SEC, T_BOOL_PYDANTIC,
          _is_bool, False, True, "缺金鑰時自動生成；正式部署一律關閉。", _SEC_REASON),
    _spec("auth.cookie_secure", "COOKIE_SECURE", SettingClass.SEC, T_BOOL_PYDANTIC,
          _is_bool, True, True, "session cookie 的 Secure 旗標。", _SEC_REASON),
    _spec("auth.allow_dev_secret", "ANILA_ALLOW_DEV_SECRET", SettingClass.SEC, T_BOOL_EQ_1,
          _is_bool, False, True,
          "開發模式豁免：略過開機的 dev 預設祕密檢查。", _SEC_REASON),

    # ── admin.* ──────────────────────────────────────────────────────────
    _spec("admin.username", "ADMIN_USERNAME", SettingClass.B_EDIT, T_STR, _is_non_empty_str,
          "admin", True, "開機 auto-seed 建立的管理員帳號名。"),
    _spec("admin.password", "ADMIN_PASSWORD", SettingClass.A, T_STR, _is_str,
          "changeme", True, "開機 auto-seed 的管理員密碼。", _SECRET_REASON),

    # ── card.* —— 自然人憑證卡登入 ──────────────────────────────────────
    _spec("card.enabled", "ENABLE_CARD_LOGIN", SettingClass.SEC, T_BOOL_PYDANTIC,
          _is_bool, False, True, "是否註冊卡登 endpoints。", _SEC_CARD_REASON),
    _spec("card.require_card_only", "REQUIRE_CARD_LOGIN_ONLY", SettingClass.SEC,
          T_BOOL_PYDANTIC, _is_bool, False, True,
          "卡登為唯一登入路徑（內網正式部署必開）。", _SEC_CARD_REASON),
    _spec("card.initial_owners", "CARD_INITIAL_OWNERS", SettingClass.SEC, T_STR,
          _is_initial_owner_csv, "", True,
          "bootstrap owner 的員工編號 CSV；填錯會變成沒有人能核准。", _SEC_CARD_REASON),
    _spec("card.ca_bundle_path", "CARD_CA_BUNDLE_PATH", SettingClass.SEC, T_STR,
          _is_certificate_bundle_path, "", True,
          "卡片 CA bundle 路徑；空值＝用釘死的那份。⚠ 首次驗章後有行程級快取"
          "（card_auth._ca_anchor_cache），所以改了要重啟。", _SEC_CARD_REASON),
    _spec("card.dev_trust_test_ca", "CARD_DEV_TRUST_TEST_CA", SettingClass.SEC,
          T_BOOL_CARD_TRUTHY, _is_bool, False, False,
          "開發用：信任測試 CA（卡登唯一入口時強制失效）。", _SEC_CARD_REASON),
    _spec("card.dev_skip_nonce_binding", "CARD_DEV_SKIP_NONCE_BINDING", SettingClass.SEC,
          T_BOOL_CARD_TRUTHY_NOSTRIP, _is_bool, False, True,
          "開發用：跳過 nonce 綁定（反 replay）。內網一律不可設。", _SEC_CARD_REASON),

    # ── network.* —— 入向白名單與出向 SSRF 閘 ───────────────────────────
    _spec("network.allowed_origins", "ALLOWED_ORIGINS", SettingClass.SEC, T_STR,
          _is_origin_csv,
          "http://localhost:5173,http://localhost:3001,http://localhost:80,"
          "http://localhost,https://localhost,https://localhost:4443",
          True, "CORS 來源白名單（逗號分隔）。", _SEC_REASON),
    _spec("network.allowed_hosts", "ALLOWED_HOSTS", SettingClass.SEC, T_STR,
          _is_allowed_host_csv,
          "*", True, "入向 Host 標頭白名單；\"*\" ＝關閉檢查。", _SEC_REASON),
    _spec("network.trusted_hosts", "ANILA_TRUSTED_HOSTS", SettingClass.SEC, T_STR,
          _is_trusted_host_csv, "", True,
          "出向 SSRF 白名單（逗號分隔）。⚠ 兩個讀取點：開機 backfill 與 url_guard "
          "per-call，取保守值標為需重啟。另有 DB 表 trusted_hosts 做同一件事，"
          "雙重來源的收斂是獨立 follow-up。", _SEC_REASON),
    _spec("network.allow_http_model_endpoint", "ANILA_ALLOW_HTTP_ENDPOINT",
          SettingClass.SEC, T_BOOL_EQ_1, _is_bool, False, False,
          "放行 http:// 的模型端點（內網 gateway 用）。", _SEC_REASON),
    _spec("network.allow_http_agent_endpoint", "ANILA_ALLOW_HTTP_AGENT_ENDPOINT",
          SettingClass.SEC, T_BOOL_EQ_1, _is_bool, False, False,
          "放行 http:// 的 agent 端點（MLSteam NodePort 用）。", _SEC_REASON),
    _spec("network.allow_grpc_endpoint", "ANILA_ALLOW_GRPC_ENDPOINT", SettingClass.SEC,
          T_BOOL_EQ_1, _is_bool, False, False,
          "放行明文 grpc:// 的模型端點。", _SEC_REASON),
    _spec("network.allow_private_endpoint", "ANILA_ALLOW_PRIVATE_ENDPOINT",
          SettingClass.SEC, T_BOOL_EQ_1, _is_bool, False, False,
          "放行指向私網位址的出向端點。", _SEC_REASON),
    _spec("network.environment", "ANILA_ENV", SettingClass.SEC, T_STR,
          _one_of("production", "prod"),
          "", False, "部署姿態字串（production/prod 視為正式）。", _SEC_REASON),
    _spec("network.ssl_cert_file", "SSL_CERT_FILE", SettingClass.SEC, T_STR,
          _is_certificate_bundle_path,
          "", True,
          "出向 TLS 的信任庫檔案。⚠ 它是**取代**整個信任庫而不是疊加，"
          "指到空或壞檔會讓所有出向 https 全掛。", _SEC_REASON),

    # ── proxy.* —— 出向模型呼叫的逾時與重試 ─────────────────────────────
    _spec("proxy.llm_timeout", "LLM_TIMEOUT", SettingClass.C, T_INT,
          _closed_int_range(1, 3600), 120, False,
          "LLM 呼叫逾時秒數。允許 1–3600 秒（下限 1 秒是防呆生命線：填 0 等於"
          "從畫面把平台打掛）。"),
    _spec("proxy.embedding_timeout", "EMBEDDING_TIMEOUT", SettingClass.C, T_INT,
          _closed_int_range(1, 3600), 30, False,
          "嵌入呼叫逾時秒數。允許 1–3600 秒。"),
    _spec("proxy.max_retries", "PROXY_MAX_RETRIES", SettingClass.C, T_INT,
          _closed_int_range(0, 10), 3, False,
          "出向呼叫失敗的重試次數。允許 0–10 次。"),
    _spec("proxy.retry_base_delay", "PROXY_RETRY_BASE_DELAY", SettingClass.C, T_FLOAT,
          _closed_float_range(0.0, 60.0), 0.5, False,
          "重試退避的基礎秒數。允許 0–60 秒。"),
    _spec("proxy.model_gateway_api_key", "MODEL_GATEWAY_API_KEY", SettingClass.A, T_STR,
          _is_str, "", True,
          "出向模型 gateway 的 Bearer key（只注入模型呼叫，不給第三方 agent）。",
          _SECRET_REASON),

    # ── health.* / alerts.* / usage.* ────────────────────────────────────
    _spec("health.check_interval", "HEALTH_CHECK_INTERVAL", SettingClass.B_EDIT, T_INT,
          _closed_int_range(1, 86400), 60, True, "健康檢查輪詢週期（秒）。允許 1–86400。"),
    # ⚠ 下界是 **15**，不是 1 —— 那個 15 不是這裡發明的：消費端
    # ``alert_detectors.py:577`` 是 ``max(15, …)``（背景迴圈啟動時算一次）。宣告寫 1 的
    # 時候，平台會**收下一個它不會照辦的值**（存 7、畫面說 7、迴圈跑 15），而且不說 ——
    # 那正是這個頁面存在的理由所要消滅的東西。controller 2026-08-09 裁定：把宣告拉齊
    # 現實，而不是在顯示層抄一份消費端的規則（值域的唯一來源仍然是登錄表）。消費端那
    # 個 ``max`` 保留當安全帶 —— 它現在永遠不會再改變任何一個從畫面存進來的值。
    # 2026-08-09 最終審查（跨家雙票）：這一顆原本是 B_EDIT，而消費端讀的是
    # ``settings`` 上的原值再自己 clamp —— env 填 5 時畫面說 5、迴圈睡 15。現在
    # ``alert_detectors.resolve_check_interval()`` 每一輪走 ``get_setting``（同一條解析鏈、
    # 同一個 domain_fn），所以這一顆真的是 **C 類**：改完**下一輪**就生效，不必重啟。
    _spec("alerts.check_interval", "ALERT_CHECK_INTERVAL", SettingClass.C, T_INT,
          _closed_int_range(15, 86400), 60, False,
          "告警偵測輪詢週期（秒）。允許 15–86400。改完下一輪偵測就生效（最久等一個週期）。"
          "⚠ 下界 15 是消費端的硬樓地板（alert_detectors.ALERT_INTERVAL_FLOOR_SECONDS）："
          "填更小的值不會讓偵測更密，只會被靜默提到 15，所以這裡直接不收。"),
    _spec("alerts.smtp_enabled", "ANILA_ALERT_SMTP_ENABLED", SettingClass.B_LOCKED,
          T_BOOL_PYDANTIC, _is_bool, False, True, "是否寄送告警信。", _SMTP_REASON),
    _spec("alerts.smtp_host", "ANILA_ALERT_SMTP_HOST", SettingClass.B_LOCKED, T_STR,
          _is_host_token, "", True, "SMTP 主機。", _SMTP_REASON),
    _spec("alerts.smtp_port", "ANILA_ALERT_SMTP_PORT", SettingClass.B_LOCKED, T_INT,
          _closed_int_range(1, 65535), 587, True, "SMTP 埠。允許 1–65535。", _SMTP_REASON),
    _spec("alerts.smtp_user", "ANILA_ALERT_SMTP_USER", SettingClass.B_LOCKED, T_STR,
          _is_token, "", True, "SMTP 帳號。", _SMTP_REASON),
    _spec("alerts.smtp_password", "ANILA_ALERT_SMTP_PASSWORD", SettingClass.A, T_STR,
          _is_str, "", True, "SMTP 密碼。", _SECRET_REASON),
    _spec("alerts.smtp_from", "ANILA_ALERT_SMTP_FROM", SettingClass.B_LOCKED, T_STR,
          _is_email_address, "", True, "告警信寄件人。", _SMTP_REASON),
    _spec("alerts.smtp_to", "ANILA_ALERT_SMTP_TO", SettingClass.B_LOCKED, T_STR,
          _is_email_address, "", True, "告警信收件人（請用群組信箱，不要個人信箱）。", _SMTP_REASON),
    _spec("alerts.smtp_use_tls", "ANILA_ALERT_SMTP_USE_TLS", SettingClass.B_LOCKED,
          T_BOOL_PYDANTIC, _is_bool, True, True, "SMTP 是否走 TLS。", _SMTP_REASON),
    _spec("usage.batch_size", "USAGE_BATCH_SIZE", SettingClass.B_EDIT, T_INT,
          _closed_int_range(1, 10000), 100, True, "用量批次寫入筆數。允許 1–10000。"),
    _spec("usage.flush_interval", "USAGE_FLUSH_INTERVAL", SettingClass.B_EDIT, T_INT,
          _closed_int_range(1, 3600), 5, True, "用量批次寫入間隔（秒）。允許 1–3600。"),

    # ── service.* —— 服務間共享密 ───────────────────────────────────────
    _spec("service.csp_service_token", "CSP_SERVICE_TOKEN", SettingClass.A, T_STR, _is_str,
          "", True, "csp 對下游 agent 的服務間共享 token。", _SECRET_REASON),
    _spec("service.internal_platform_api_key", "INTERNAL_PLATFORM_API_KEY", SettingClass.A,
          T_STR, _is_str, "", True,
          "內部平台 API key。csp 不使用它，只在開機檢查它不是 dev 預設值。",
          _SECRET_REASON),
    _spec("service.codeserver_password", "CODESERVER_PASSWORD", SettingClass.A, T_STR,
          _is_str, "", True,
          "code-server 密碼。csp 不使用它，只在開機檢查它不是 dev 預設值。",
          _SECRET_REASON),

    # ── seed.* —— 開機自動註冊 ──────────────────────────────────────────
    # ⚠ 這三顆的「改了會怎樣」**各自不同**，所以說明不可以是同一句複製。以真碼為準
    # （auto_seed.py），不是照類別猜：一句「僅首次開機生效」抄三次，對後兩顆是假的。
    _spec("seed.models", "AUTO_REGISTER_MODELS", SettingClass.B_EDIT, T_STR, _is_seed_models,
          "", True,
          "開機自動註冊的模型清單（JSON 字串）。⚠ env **只負責建立**：清單裡的名字"
          "若已經在模型登錄裡，這一次開機不會動它任何一個欄位（連端點都不蓋回去，"
          "auto_seed.py:272 的 OE-2 B3）。所以改這裡只對**新加的名字**有效，"
          "既有模型請到治理中心改。"),
    _spec("seed.agents", "AUTO_REGISTER_AGENTS", SettingClass.B_EDIT, T_STR, _is_seed_agents,
          "", True,
          "開機自動註冊的 agent 清單（JSON 字串）。⚠ 端點以外的欄位（owner、"
          "描述、能力、核准狀態）**每次開機都會照這份清單重新同步**既有的 agent"
          "（auto_seed.py:383 起）；端點本身建立之後歸管理員，不會被蓋回去。"),
    _spec("seed.links", "AUTO_REGISTER_LINKS", SettingClass.B_EDIT, T_STR, _is_seed_links,
          "", True,
          "開機自動註冊的平台連結清單（JSON 字串）。⚠ **每次開機都會 upsert**："
          "env 擁有的欄位（網址、圖示、說明、排序、是否公開、可見角色）會照這份清單"
          "蓋回去（auto_seed.py:96 起）。兩種例外不會被蓋："
          "治理中心自建的連結（config_source=db 整列跳過），"
          "以及列在該列 db_editable_fields 裡的欄位（目前是「是否啟用」）—— "
          "管理員在畫面上改過的那些，seed 不會還原。"),
    _spec("seed.api_keys", "AUTO_SEED_API_KEYS", SettingClass.A, T_STR, _is_seed_api_keys,
          "", True,
          "開機自動建立的帳號與 API key 清單（JSON 字串）。⚠ 值裡面內嵌 API key，"
          "長得像設定但不是。", _SECRET_REASON),

    # ── storage.* / queue.* ─────────────────────────────────────────────
    _spec("storage.attachment_path", "ATTACHMENT_STORAGE_PATH", SettingClass.B_EDIT, T_STR,
          _is_non_empty_str, "data/attachments", True, "附件落地目錄（對應容器掛載）。"),
    _spec("storage.ingestion_upload_dir", "INGESTION_UPLOAD_DIR", SettingClass.B_LOCKED,
          T_STR, _is_non_empty_str, "/var/anila/ingestion-uploads", True,
          "文件上傳暫存目錄。",
          "檔案系統語意；三個讀取點（含模組層）時機不一，執行期改會讓它們對不齊（設計 §3.2）。"
          + _BOOT_SYNC_REASON),
    _spec("queue.redis_url", "REDIS_URL", SettingClass.B_LOCKED, T_STR, _is_redis_url,
          "redis://redis:6379", True,
          "Redis DSN。⚠ 三個讀取點的內建預設不一致，而且**多數是另一個值**："
          "ingestion_queue.py:24 是 redis://redis:6379，"
          "token_revocation_publisher.py:79 與 health_checker.py:634 都是 "
          "redis://redis:6379/0。此處宣告前者（收斂是獨立 follow-up）。",
          "跨服務基礎設施 DSN，執行期改＝事故製造機（設計 §3.2）。"
          + _BOOT_SYNC_REASON),
    # 2026-08-09 最終審查（跨家雙票）：這一顆原本是 B_LOCKED，理由是「import 期就算成
    # 模組常數、而且讀 os.environ」——那個模組常數已經拿掉了。現在由手上有 session 的
    # 呼叫端（``token_revocation.commit_token_revocation``，在 commit 之前）走
    # ``get_setting`` 解析，再以必填關鍵字傳給發布端，所以它真的是 **C 類**。
    # 舊狀態下 env 填 999：畫面照值域退回顯示 2.0、而 Redis 連線真的用 999。
    _spec("queue.token_revocation_redis_timeout", "TOKEN_REVOCATION_REDIS_TIMEOUT_SECONDS",
          SettingClass.C, T_FLOAT, _closed_float_range(0.1, 60.0), 2.0, False,
          "同步撤銷發布的 Redis 逾時秒數。允許 0.1–60 秒。改完下一次撤銷就生效，"
          "不必重啟（值在發布**之前**由呼叫端解析）。"),

    # ── limits.* —— 純數值調節鈕 ────────────────────────────────────────
    _spec("limits.department_max_depth", "ANILA_DEPARTMENT_MAX_DEPTH", SettingClass.C,
          T_INT, _closed_int_range(1, 10), 3, False,
          "部門樹最大層數。允許 1–10 層。⚠ 只影響新建與 re-parent 的檢查，"
          "調低不會回溯處理既有超深節點。"),
    _spec("limits.message_max_siblings", "ANILA_MESSAGE_MAX_SIBLINGS", SettingClass.C,
          T_INT, _closed_int_range(1, 1000), 20, False,
          "同一節點下的訊息分支上限。允許 1–1000 個。"),
    _spec("limits.action_invoke_per_min", "ANILA_ACTION_INVOKE_PER_MIN", SettingClass.C,
          T_INT, _closed_int_range(1, 10000), 20, False,
          "每使用者每分鐘的自訂動作呼叫上限。允許 1–10000 次。"),
    _spec("limits.action_max_body_chars", "ANILA_ACTION_MAX_BODY_CHARS", SettingClass.C,
          T_INT, _closed_int_range(1, 1_000_000), 20000, False,
          "自訂動作 body 的最大字元數。允許 1–1000000 字元。"),
    _spec("limits.default_context_window", "ANILA_DEFAULT_CONTEXT_WINDOW", SettingClass.C,
          T_INT, _closed_int_range(256, 10_000_000), 128000, False,
          "模型未登記 context window 時的後備值。允許 256–10000000 token。"),
    _spec("limits.attachment_budget_ratio", "ANILA_ATTACHMENT_BUDGET_RATIO", SettingClass.C,
          T_FLOAT, _closed_float_range(0.0, 1.0), 0.7, False,
          "附件可佔用 context window 的比例。允許 0–1（兩端都含）。"),
    _spec("limits.attachment_token_safety", "ANILA_ATTACHMENT_TOKEN_SAFETY", SettingClass.C,
          T_FLOAT, _closed_float_range(1.0, 4.0), 1.15, False,
          "token 估算的安全係數。允許 1–4（低於 1 等於刻意低估，會超出視窗）。"),
    _spec("limits.attachment_max_stored_tokens", "ANILA_ATTACHMENT_MAX_STORED_TOKENS",
          SettingClass.C, T_INT, _closed_int_range(1, 100_000_000), 800_000, False,
          "單份附件可落地的 token 絕對上限。允許 1–100000000 token。"),

    # ── intl.* —— 語系相關 ──────────────────────────────────────────────
    _spec("intl.zh_normalize", "ANILA_ZH_NORMALIZE", SettingClass.C, T_BOOL_NE_0, _is_bool,
          True, False, "把模型輸出的簡體字正規化成繁體。"),
    _spec("intl.query_expansion", "ANILA_QUERY_EXPANSION", SettingClass.C, T_BOOL_NE_0,
          _is_bool, True, False, "檢索前做同義詞查詢擴展。"),
    _spec("intl.zip_filename_encoding", "ANILA_ZIP_FILENAME_ENC", SettingClass.C, T_STR,
          _is_supported_text_encoding, "", False,
          "ZIP 內非 UTF-8 檔名的解碼碼頁。空值＝依序試 cp950、gbk；"
          "填的話必須是 Python 認得的碼頁名（例如 gbk）。"),

    # ── memory.* —— 長期記憶的檢索參數 ──────────────────────────────────
    _spec("memory.retrieve_top_k", "MEMORY_RETRIEVE_TOP_K", SettingClass.C, T_INT,
          _closed_int_range(1, 100), 3, False, "記憶檢索取回的筆數。允許 1–100 筆。"),
    _spec("memory.retrieve_min_cosine", "MEMORY_RETRIEVE_MIN_COSINE", SettingClass.C,
          T_FLOAT, _closed_float_range(0.0, 1.0), 0.4, False,
          "記憶檢索的相似度門檻。允許 0–1（兩端都含）。與院內規章的分數門檻是"
          "同一種東西。"),
    _spec("memory.max_chunk_chars", "MEMORY_MAX_CHUNK_CHARS", SettingClass.C, T_INT,
          _closed_int_range(1, 100_000), 1200, False,
          "記憶切塊的最大字元數。允許 1–100000 字元。"),
    _spec("memory.http_timeout", "MEMORY_HTTP_TIMEOUT", SettingClass.C, T_FLOAT,
          _closed_float_range(1.0, 3600.0), 30.0, False,
          "記憶抽取的 HTTP 逾時秒數。允許 1–3600 秒。"),
    _spec("memory.llm_model", "MEMORY_LLM_MODEL", SettingClass.B_LOCKED, T_STR, _is_non_empty_str,
          "gemma4", True, "記憶抽取用的模型名。",
          "模型名不是數值鈕 —— 換模型牽動 per-model 授權，畫面上補不了（設計 §3.2）。"
          + _BOOT_SYNC_REASON),

    # ── agents.* ─────────────────────────────────────────────────────────
    _spec("agents.template_dir", "ANILA_TEMPLATE_DIR", SettingClass.B_LOCKED, T_STR,
          _is_existing_directory_path, "", True,
          "agent 註冊範本目錄；空值＝用程式推導的 repo 內路徑。"
          "⚠ api/agents/registration.py:106 在 import 期就把它算成模組常數，"
          "而且讀的是 os.environ。",
          _BOOT_ORDER_REASON + _compose_hint("ANILA_TEMPLATE_DIR")),

    # ── ingestion.* —— OCR 與視覺模型（消費者主要在 anila_core） ────────
    _spec("ingestion.pdf_ocr_fallback", "PDF_OCR_FALLBACK", SettingClass.B_LOCKED,
          T_BOOL_LOWER_TRUE, _is_bool, False, False,
          "掃描 PDF 走視覺模型 OCR 後援。⚠ csp 端的 compose 刻意寫死 false，"
          "預覽與實際 ingest 的不一致是刻意設計。", _OCR_REASON),
    _spec("ingestion.vision_url", "VISION_URL", SettingClass.B_LOCKED, T_STR, _is_url_or_empty,
          "", False, "OCR 用視覺模型的 base URL。", _OCR_REASON),
    _spec("ingestion.vision_model", "VISION_MODEL", SettingClass.B_LOCKED, T_STR,
          _is_model_name, "", False, "OCR 用視覺模型的模型名。", _OCR_REASON),
    _spec("ingestion.vision_api_key", "VISION_API_KEY", SettingClass.A, T_STR, _is_str,
          "", False, "OCR 用視覺模型的 Bearer key。", _SECRET_REASON),
    _spec("ingestion.vision_verify_ssl", "VISION_VERIFY_SSL", SettingClass.SEC,
          T_BOOL_LOWER_TRUE, _is_bool, True, False,
          "呼叫視覺模型時是否驗證 TLS 憑證。", _SEC_REASON),
    _spec("ingestion.pdf_ocr_vision_prompt", "PDF_OCR_VISION_PROMPT", SettingClass.B_LOCKED,
          T_STR, _is_non_empty_str, _DEFAULT_VISION_PROMPT, False,
          "OCR 提示詞。", _OCR_REASON),
    _spec("ingestion.pdf_ocr_dpi", "PDF_OCR_DPI", SettingClass.B_LOCKED, T_INT,
          _closed_int_range(1, 1200), 200, False,
          "PDF 轉圖的解析度（DPI）。允許 1–1200。", _OCR_REASON),
    _spec("ingestion.pdf_ocr_concurrency", "PDF_OCR_CONCURRENCY", SettingClass.B_LOCKED,
          T_INT, _closed_int_range(1, 64), 4, False,
          "OCR 併發頁數。允許 1–64。", _OCR_REASON),
    _spec("ingestion.pdf_ocr_max_pages", "PDF_OCR_MAX_PAGES", SettingClass.B_LOCKED, T_INT,
          _closed_int_range(1, 10000), 100, False,
          "單份 PDF 的 OCR 頁數上限。允許 1–10000。", _OCR_REASON),
    _spec("ingestion.doc_parser", "DOC_PARSER", SettingClass.B_LOCKED, T_STR,
          _one_of("native", "docling"),
          "native", False, "文件解析器（只能是 native／docling）。", _OCR_REASON),
    _spec("ingestion.docling_ocr_langs", "DOCLING_OCR_LANGS", SettingClass.B_LOCKED, T_STR,
          _is_ocr_languages, "ch_tra,en", False, "docling 的 OCR 語系清單（逗號分隔）。",
          _OCR_REASON),

    # ── 已經落地的那一顆：別名，不是複製 ────────────────────────────────
    _spec(KB_THRESHOLD_KEY, None, SettingClass.C, T_FLOAT, _is_usable_kb_threshold,
          KB_THRESHOLD_DEFAULT, False,
          "院內規章檢索的分數門檻。允許 0–1（兩端都含）。⚠ 預設值是拿替代嵌入"
          "模型量出來的猜測，換模型後會失去意義，請用校準視圖重量一次。"),
)


#: key → 條目。``set_setting``／``resolve_setting`` 一律走這裡，所以測試換掉
#: 其中一筆時，寫入端與解析端會同時看到同一個替身。
REGISTRY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}


def require_spec(key: str) -> SettingSpec:
    """取一筆宣告，取不到就拋。**不猜、不回預設**。

    設定頁的 key 是從後端回出去的，所以一個查不到的 key 只可能是打錯或是有人
    在編登錄表時漏了一筆 —— 兩種情況都要當場看得見。
    """
    try:
        return REGISTRY[key]
    except KeyError:
        raise UnknownSettingError(f"未知的設定 key：{key!r}") from None
