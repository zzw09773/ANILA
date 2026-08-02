#!/usr/bin/env python3
"""把共同前導 SSOT 產成前端 TS 常數檔。

前端（Vite build）吃不到 python 模組，所以用 build-time 產物橋接：
本腳本從 ``anila_core.prompts`` 讀出前導文本，寫到
``apps/anilalm/src/generated/preamble.ts``。防漂移由
``packages/anila-core/tests/test_common_preamble.py`` 的 sync guard 把關——
改了 SSOT 沒重跑本腳本，測試會紅。

用法（repo 任何位置皆可）::

    python packages/anila-core/scripts/gen_preamble_ts.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "packages" / "anila-core" / "src"))

from anila_core.prompts import (  # noqa: E402
    COMMON_PREAMBLE,
    FACTS_AS_OF,
    LANGUAGE_PREAMBLE,
)

OUT = ROOT / "apps" / "anilalm" / "src" / "generated" / "preamble.ts"


def _esc(s: str) -> str:
    """跳脫 TS template literal 的三個特殊序列。"""
    return s.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def main() -> None:
    content = (
        "// 自動產生檔——SSOT 在 packages/anila-core/src/anila_core/prompts/，不要手改。\n"
        "// 重新產生：python packages/anila-core/scripts/gen_preamble_ts.py\n"
        f"// 要職資訊時點：{FACTS_AS_OF}（異動改 current_facts.py 後重跑）\n"
        "\n"
        f"export const COMMON_PREAMBLE = `{_esc(COMMON_PREAMBLE)}`\n"
        "\n"
        f"export const LANGUAGE_PREAMBLE = `{_esc(LANGUAGE_PREAMBLE)}`\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(content)} chars)")


if __name__ == "__main__":
    main()
