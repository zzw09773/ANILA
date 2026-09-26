"""簡報生圖提示只留短的抽象視覺描述。

studio 映像沒有 anila_core，不能直接 import。這裡的過濾規則必須與
``anila_core.api.router_prompts.redact_internal_details`` 相同，避免把
路徑、網址、主機或環境變數名稱送進生圖模型。知識庫原文（來源、條文、
引用、chunk）整段拒絕，不截成前綴再送。
"""
from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_ABS_PATH_RE = re.compile(r"(?<![\w.+-])/[\w.+-]+(?:/[\w.+-]+)+")
_FILENAME_RE = re.compile(r"\b[\w.-]+\.(?:js|py)\b", re.IGNORECASE)
_IPV4_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
_HOST_RE = re.compile(
    r"\b(?:localhost|(?:[a-z0-9-]+\.)+(?:com|org|net|tw|io|dev|local|internal|lan))\b",
    re.IGNORECASE,
)
_CDN_RE = re.compile(r"cdnjs|jsDelivr|jsdelivr|unpkg|threejs\.org", re.IGNORECASE)
_ENV_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_KB_LEAK_RE = re.compile(
    r"(來源\s*[:：]|第\s*\d+\s*條|\(參\s*\[|（參\s*\[|chunk\b|leaf-\d+)",
    re.IGNORECASE,
)

IMAGE_PROMPT_MAX_CHARS = 240


def redact_image_prompt_details(text: str) -> str:
    """拿掉路徑、檔名、位址、主機與環境變數名稱。"""
    if not isinstance(text, str):
        text = str(text)
    text = _URL_RE.sub("", text)
    text = _IPV4_RE.sub("", text)
    text = _ABS_PATH_RE.sub("", text)
    text = _FILENAME_RE.sub("", text)
    text = _HOST_RE.sub("", text)
    text = _CDN_RE.sub("", text)
    text = _ENV_RE.sub("", text)
    return text


def outbound_image_prompt(prompt: str) -> str | None:
    """回傳可送出的抽象視覺描述；過長、像知識庫原文，或濾完是空的就放棄。"""
    text = " ".join(str(prompt).split())
    if not text or len(text) > IMAGE_PROMPT_MAX_CHARS or _KB_LEAK_RE.search(text):
        return None
    cleaned = " ".join(redact_image_prompt_details(text).split())
    if not cleaned or len(cleaned) > IMAGE_PROMPT_MAX_CHARS or _KB_LEAK_RE.search(cleaned):
        return None
    return cleaned
