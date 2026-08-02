#!/usr/bin/env python3
"""把共同前導的國家用語／紀年段產成 Studio 可 import 的 Python 常數檔。

``services/anila-studio`` 的 pyproject 未宣告依賴 ``anila-core``（刻意隔離），
故以 build-time 產物橋接 SSOT。防漂移由
``packages/anila-core/tests/test_preamble_studio_sync.py`` 把關——
改了 SSOT 沒重跑本腳本，測試會紅。

用法（repo 任何位置皆可）::

    python packages/anila-core/scripts/gen_preamble_studio.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "anila-core" / "src"))

from anila_core.prompts import ERA_RULES, NATIONAL_TERMINOLOGY  # noqa: E402

OUT = ROOT / "services" / "anila-studio" / "app" / "generated_preamble.py"


def emit_str(s: str) -> str:
    """Emit a Python string literal that round-trips ``s`` exactly at import time.

    Uses ``repr()`` so backslashes, quotes, and control characters cannot
    silently alter the value (unlike a raw triple-quoted dump).
    """
    return repr(s)


def main() -> None:
    content = (
        "# 自動產生檔——SSOT 在 packages/anila-core/src/anila_core/prompts/，不要手改。\n"
        "# 重新產生：python packages/anila-core/scripts/gen_preamble_studio.py\n"
        "# Studio 未依賴 anila-core；僅注入【國家與用語規範】＋【紀年規則】兩段\n"
        "# （語言規則 Studio 既有 prompt 已涵蓋，勿重複）。\n"
        "\n"
        f"NATIONAL_TERMINOLOGY = {emit_str(NATIONAL_TERMINOLOGY)}\n"
        "\n"
        f"ERA_RULES = {emit_str(ERA_RULES)}\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(content)} chars)")


if __name__ == "__main__":
    main()
