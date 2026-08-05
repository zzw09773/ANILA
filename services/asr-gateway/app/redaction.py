"""外流字串的祕密清洗 —— log、`/asr/health`、送到瀏覽器的錯誤訊息共用一份。

⚠ 這個 repo 是 PUBLIC,而 gateway 的錯誤訊息會**原字串**經 WebSocket
`{"type":"error"}` 送進使用者的瀏覽器(`app/session.py`),log 則會被貼進工單。
祕密有兩種進得來的方式,兩種都要擋:

1. **憑證字面值** —— 上游可能把金鑰回音在 body 裡(實際見過
   `{"error":"invalid api key: sk-…"}`)。
2. **URL 裡的 userinfo** —— operator 把憑證貼進端點位址
   (`https://user:sk-…@host/…`)。httpx 的 `HTTPStatusError` 訊息會把**完整
   URL** 貼上去,而 httpx 自己的 INFO request log 是**每一次請求**都印一行
   (不只失敗那次)。`httpx.URL.__repr__` 會遮蔽密碼,但 log 走的是 `%s`
   → `__str__` → **不遮蔽**。

所以清洗一律走 `scrub()`:先剝 userinfo,再抹憑證字面值。單獨用其中一半都
會漏掉另一種。
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse, urlunparse

# `scheme://userinfo@` —— userinfo 不含 `/ ? #` 與空白(RFC 3986 §3.2.1)。
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/?#\s@]*@")


def strip_url_userinfo(url: str) -> str:
    """Keep host/path; drop userinfo so a pasted secret is not echoed.

    針對「整個字串就是一個 URL」的情境(`/asr/health` 的 `decode_url`、
    啟動時那行 log)。自由文字請用 `strip_userinfo_in_text`。
    """
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.username is None and parsed.password is None:
        return url
    hostname = parsed.hostname or ""
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        host = hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=host))


def strip_userinfo_in_text(text: str) -> str:
    """把**自由文字裡**每一個 URL 的 userinfo 剝掉。

    例外訊息與 log 行不是純 URL(`Server error '500 …' for url '<url>'`),
    urlparse 那條路吃不下,所以這裡走正規表示式。
    """
    if not text or "@" not in text:
        return text
    return _URL_USERINFO_RE.sub(lambda m: m.group("scheme"), text)


def redact_secrets(text: str, *secrets: str) -> str:
    """把憑證字面值換成 `<redacted>`。

    ⚠ **任何非空長度都要抹**。原本這裡有一道 `len >= 4` 的門檻(避免短字串
    到處誤中),但那讓 1~3 個字元的憑證變成完全不設防 —— 「訊息被短憑證打得
    支離破碎」只是難讀,「短憑證原樣送進瀏覽器」是外洩。難讀勝過外洩。
    """
    result = text
    for secret in secrets:
        token = (secret or "").strip()
        if token:
            result = result.replace(token, "<redacted>")
    return result


def scrub(text: str, *secrets: str) -> str:
    """要外流的字串一律過這裡:先剝 URL userinfo,再抹憑證字面值。"""
    return redact_secrets(strip_userinfo_in_text(text), *secrets)


class UserinfoRedactingFilter(logging.Filter):
    """把 log record 裡每一個 URL 的 userinfo 剝掉。

    存在的理由是 httpx 的 `HTTP Request: %s %s …` INFO 行:它印的是**每一次**
    請求(不只失敗),而位址是我們自己傳進去的 `ASR_DECODE_URL` /
    治理中心指派的 endpoint —— 只要 operator 把憑證貼進位址,gateway 就會
    每 0.5 秒把它寫進 log 一次。這不是 httpx 的 bug,是我們得自己收的口。

    ⚠ 這道濾網只管 **userinfo**。憑證字面值走 header,不會出現在 log 的格式
    參數裡;真的要進錯誤訊息的那些由呼叫端的 `scrub()` 負責。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = strip_userinfo_in_text(record.msg)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(_clean_arg(a) for a in args)
        elif isinstance(args, dict):
            record.args = {k: _clean_arg(v) for k, v in args.items()}
        return True


def _clean_arg(value):
    """只在 `str(value)` 真的含 userinfo 時才動它。

    保持原型別很重要 —— `%d` 收到字串會 TypeError,而一個為了防洩漏而把服務
    log 打爛的濾網,第一次上線就會被拔掉。
    """
    if isinstance(value, str):
        return strip_userinfo_in_text(value)
    if isinstance(value, (int, float, bool, bytes)) or value is None:
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — 濾網不准把 log 弄炸
        return value
    cleaned = strip_userinfo_in_text(text)
    return cleaned if cleaned != text else value


def install_userinfo_redaction(*logger_names: str) -> None:
    """把濾網掛到 root 的 handler 上,再逐一掛到點名的 logger。

    兩層都要:
      * **root 的 handler** —— 所有往上冒泡的紀錄都會經過,涵蓋面最廣。
      * **點名的 logger 自己** —— logger 層的 filter 只作用在「直接用這個
        logger 記」的紀錄,但它不受 root 有沒有 handler 影響(uvicorn 底下
        root 可能早就被別人設定過,`basicConfig` 就不會再加 handler)。
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, UserinfoRedactingFilter) for f in handler.filters):
            handler.addFilter(UserinfoRedactingFilter())
    for name in logger_names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, UserinfoRedactingFilter) for f in logger.filters):
            logger.addFilter(UserinfoRedactingFilter())
