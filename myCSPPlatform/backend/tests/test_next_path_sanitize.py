"""Sprint 6 X / B3: tests for OIDC next_path sanitisation.

next_path 是 OIDC 流程的 query param + JWT state payload。攻擊者若能
讓 callback HTML 把使用者 redirect 到外部站，就能做 phishing。我們的
allow-list 規則：必須 / 開頭 + 第二字元不能是 / 或 \\ + 不含控制字元 +
長度 <= 200。任何不符 fallback 為 ``/``。
"""
from __future__ import annotations

import pytest

from app.services.external_auth_service import sanitize_next_path


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 合法路徑 — 原樣通過
        ("/", "/"),
        ("/dashboard", "/dashboard"),
        ("/api/users/1?tab=models", "/api/users/1?tab=models"),
        # 空 / None / 非 str → fallback
        ("", "/"),
        (None, "/"),
        # absolute URL → fallback
        ("https://evil.com/", "/"),
        ("http://evil.com", "/"),
        # protocol-relative bypass
        ("//evil.com/phish", "/"),
        ("//evil.com", "/"),
        # backslash bypass（部分瀏覽器解析為 //evil.com）
        ("/\\evil.com", "/"),
        ("/\\\\evil.com", "/"),
        # control chars / header injection
        ("/foo\r\nLocation: https://evil.com", "/"),
        ("/foo\nbar", "/"),
        ("/foo\x00bar", "/"),
        # 超長
        ("/" + "a" * 300, "/"),
        # 邊界長度（200 字元剛好通過）
        ("/" + "a" * 199, "/" + "a" * 199),
    ],
)
def test_sanitize_next_path(raw, expected):
    assert sanitize_next_path(raw) == expected
