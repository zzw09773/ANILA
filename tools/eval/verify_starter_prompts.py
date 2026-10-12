#!/usr/bin/env python3
"""首頁入門卡：問句要先從合成示範文件查得到，才算可以顯示。

與 ``apps/anila-shell/src/starterPrompts.js`` 同一套規則（關鍵字單元、
唯一最高分、預期文件與原文）。不打模型、不打網路。

執行（repo 根目錄）::

    python3 tools/eval/verify_starter_prompts.py

任一題不該顯示卻顯示、或該顯示卻查不到 → exit 1。
也會用 node 跑前端同一支函式，兩邊的可見 id 不一致就失敗。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "apps/anila-shell/src/starterPrompts.data.json"
KEYWORD_PATH = (
    ROOT / "packages/anila-core/src/anila_core/embeddings/keyword.py"
)

# 與 keyword.cjk_search_units、starterPrompts.js 相同。
_MIN_SCORE = 2
EXPECTED_VISIBLE = ("meeting-hours", "retention-years", "visitor-photo")
EXPECTED_HIDDEN = ("parking-apply",)

_NODE = r"""
import { visibleStarterPrompts } from "./apps/anila-shell/src/starterPrompts.js";
process.stdout.write(visibleStarterPrompts().map((item) => item.id).join("\n"));
"""


def _load_keyword_units():
    spec = importlib.util.spec_from_file_location("anila_keyword_units", KEYWORD_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"無法載入 {KEYWORD_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.cjk_search_units


def cjk_search_units(text: str, limit: int = 12) -> list[str]:
    """複本，給檢索用。載入後會與 keyword.cjk_search_units 對過。"""
    import re

    cjk = re.compile(r"[\u3400-\u9fff]+")
    word = re.compile(r"[A-Za-z0-9]{2,}")
    raw = (text or "").strip()
    if not raw:
        return []
    units: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token and token not in seen and len(units) < limit:
            seen.add(token)
            units.append(token)

    for run in cjk.findall(raw):
        if len(run) == 1:
            add(run)
            continue
        for size in (2, 3):
            if len(run) < size:
                continue
            for i in range(len(run) - size + 1):
                add(run[i : i + size])
                if len(units) >= limit:
                    return units
    for token in word.findall(raw):
        add(token.lower())
        if len(units) >= limit:
            break
    return units


def retrieve(question: str, documents: list[dict]) -> dict | None:
    units = cjk_search_units(question)
    if not units:
        return None
    ranked = []
    for doc in documents:
        hay = str(doc.get("text") or "").lower()
        score = sum(1 for unit in units if unit.lower() in hay)
        ranked.append((score, str(doc.get("id") or ""), doc))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    best_score, _best_id, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0
    if best_score < _MIN_SCORE or best_score <= second:
        return None
    return best


def visible_ids(catalog: dict) -> list[str]:
    documents = catalog["documents"]
    by_id = {doc["id"]: doc for doc in documents}
    found: list[str] = []
    for prompt in catalog["prompts"]:
        hit = retrieve(prompt["q"], documents)
        if hit is None or hit["id"] != prompt["expect_doc"]:
            continue
        text = str(hit.get("text") or "")
        quote = str(prompt.get("quote") or "")
        span = str(prompt.get("expect_span") or "")
        if span not in text or quote not in text or span not in quote:
            continue
        if prompt["expect_doc"] not in by_id:
            continue
        found.append(prompt["id"])
    return found


def _assert_units_match_product(catalog: dict) -> None:
    product = _load_keyword_units()
    samples = [prompt["q"] for prompt in catalog["prompts"]]
    samples.append("幫我寄信，會議室")
    for sample in samples:
        local = cjk_search_units(sample)
        upstream = product(sample)
        if local != upstream:
            raise SystemExit(
                "檢索單元和 keyword.cjk_search_units 不一致：\n"
                f"  問句：{sample}\n  本腳本：{local}\n  產品：{upstream}"
            )


def _js_visible_ids() -> list[str]:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            "前端入門卡函式跑不起來：\n"
            + (proc.stderr or proc.stdout or f"exit {proc.returncode}")
        )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    try:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"讀不到題庫 {CATALOG_PATH}：{exc}") from exc
    prompts = catalog.get("prompts") or []
    ids = [prompt.get("id") for prompt in prompts]
    if len(ids) != len(set(ids)):
        raise SystemExit("starter prompt id 重複")
    for hidden in EXPECTED_HIDDEN:
        if hidden not in ids:
            raise SystemExit(f"題庫少了不該上架的對照題 {hidden}")
    _assert_units_match_product(catalog)
    found = visible_ids(catalog)
    if tuple(found) != EXPECTED_VISIBLE:
        raise SystemExit(
            "可見入門卡和預期不符。\n"
            f"  預期：{list(EXPECTED_VISIBLE)}\n  實際：{found}"
        )
    hidden_shown = [item for item in EXPECTED_HIDDEN if item in found]
    if hidden_shown:
        raise SystemExit(f"查不到的問句仍被顯示：{hidden_shown}")
    js_ids = _js_visible_ids()
    if js_ids != found:
        raise SystemExit(
            "前端與這支腳本的可見入門卡不一致。\n"
            f"  腳本：{found}\n  前端：{js_ids}"
        )
    print(
        f"starter prompts OK：{len(found)} visible, "
        f"{len(EXPECTED_HIDDEN)} hidden（前端一致）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
