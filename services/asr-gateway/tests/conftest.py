"""app.config 的 Settings 與 app.main 的 `app = create_app()` 都在 module
層執行,而 create_app 會 fail-loud 驗設定 → env 必須在任何 app.* import 之前
就位。pytest 保證 conftest 先於 test module 載入,所以放這裡。

這些值只為了讓 import 過得去;測試自己會建帶著明確 Settings 的 app。
"""

import os
import sys
from pathlib import Path

# ── anila_core(SSRF guard 的 SSOT)────────────────────────────────────────
# gateway 不把整包 anila-core 裝進 runtime image(它會拖進 asyncpg / pgvector /
# aiosqlite,而這個服務刻意是「純 CPU、無 DB」);image 是在 build 時把
# packages/anila-core/src/anila_core/security/ **原檔**複製進去(見 Dockerfile)。
# 本機測試對應的做法就是把同一份 src 掛上 sys.path —— 兩邊 import 路徑一致,
# 而且 repo 裡永遠只有一份 url_guard.py,沒有第二份可以漂移。
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_SRC = _REPO_ROOT / "packages" / "anila-core" / "src"
if _CORE_SRC.is_dir() and str(_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(_CORE_SRC))

os.environ.setdefault("ASR_DECODE_URL", "https://decoder.invalid:9000")
os.environ.setdefault("ASR_DECODER_TOKEN", "conftest-token")

import pytest  # noqa: E402 — 必須在上面的 sys.path / env 就位之後


@pytest.fixture
def intranet_guard_env(monkeypatch):
    """把「內網部署當下的出向旗標」在測試裡明說出來。

    對應 `.env` 與 `infra/compose/platform.yml` 的實際值:內網模型 gateway 走
    純 http(P0.2 拍板),而本地 decoder 是 docker 服務名 `asr-decoder`,靠
    ANILA_TRUSTED_HOSTS 點名。**這不是放寬 guard** —— guard 照跑,只是測試
    不再依賴跑測試那台機器剛好設了什麼環境變數。
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "asr-decoder")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    yield
