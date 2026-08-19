"""守衛:DOCLING_SERVICE_TOKEN 的「活憑證」拒收(2026-08-19 稽核 v15)。

第二票實測:.env.example 的佔位字串原樣起容器,POST /parse 拿到 200——repo 是
PUBLIC,那等於公開了一份能用的憑證。主防線是 .env.example 佔位值改成角括號
(原樣複製即不能用);這層 `_require_token` 是第**二**層墊底,不是主防線。

稽核量到的陷阱必須釘在這裡:舊佔位字串
`replace-with-openssl-rand-hex-32` **剛好 32 字元**,而真值(openssl rand -hex 32)
是 64 字元——「長度 < 32 拒收」這種直覺規則**剛好抓不到它**。所以這個字串要
顯式列名,不能只靠長度。
"""
from __future__ import annotations

import itertools
import string

import pytest

from app.config import Settings
from app.main import _require_token

LEGACY_PLACEHOLDER = "replace-with-openssl-rand-hex-32"


def _settings(token: str) -> Settings:
    return Settings(DOCLING_SERVICE_TOKEN=token)


def test_rejects_empty() -> None:
    with pytest.raises(RuntimeError):
        _require_token(_settings(""))


def test_rejects_whitespace_only() -> None:
    with pytest.raises(RuntimeError):
        _require_token(_settings("   "))


def test_rejects_angle_bracket_placeholder() -> None:
    """角括號佔位(根目錄慣例)——就算有人把 cp 壞檔修到能起,值本身也過不了。"""
    with pytest.raises(RuntimeError):
        _require_token(_settings("<openssl rand -hex 32>"))


def test_rejects_legacy_placeholder_by_name() -> None:
    """舊出貨佔位值:32 字元,長度規則抓不到,必須靠列名。

    這條是 2026-08-19 稽核點名的陷阱——刪掉列名、只留 len<32,這條會紅。
    """
    assert len(LEGACY_PLACEHOLDER) == 32  # 陷阱的前題:剛好卡在長度閘上
    with pytest.raises(RuntimeError):
        _require_token(_settings(LEGACY_PLACEHOLDER))


def test_rejects_short_token() -> None:
    with pytest.raises(RuntimeError):
        _require_token(_settings("short"))


def test_accepts_64_hex_token() -> None:
    """真值形狀(openssl rand -hex 32 = 64 字元 hex)要放行——守衛不可擋真值。"""
    token = "ab" * 32
    assert len(token) == 64
    _require_token(_settings(token))  # 不拋 = 過
