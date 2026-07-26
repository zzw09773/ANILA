#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legacy 清單 ratchet 檢查 —— 補救計畫 W1-7 / 子計畫 C5 首發。

## 為什麼需要這支

稽核抓到的 **S3 反模式**:平台一再出現「新 read model 落地了,但舊那層留著,
沒有退場條件、沒有清單」。結果是**每一個 boundary 都要重查一次舊層還在不在**
—— 而漏查的那個 boundary 就是下一個安全缺口。已知八對:

    conv.classified          vs  classification_level(五級 read model)
    platform_links           vs  registered_service
    CSP_SERVICE_TOKEN        vs  per-agent credential
    is_legacy service client vs  ——(該清零)
    legacy_runtime_call      vs  ——(該清零)
    proxy_service facade     vs  proxy/ package
    PBKDF2 100k fallback     vs  600k(OWASP 2024)
    SQLite conftest          vs  真 PG

## 為什麼是 grep-ledger 而不是 lint rule

八對橫跨 py / jsx / yaml / shell,**沒有單一 lint 生態蓋得住**。而對「禁止新增」
這個目標,計數 ratchet 與 lint rule 等價。個別對(例如 `classified`)之後可以
再加 ESLint `no-restricted-syntax` 或 ruff 自訂規則強化,記在 `notes`。

## ratchet 的方向性(這支的全部意義)

- 實測 **>** `max_count` → **fail**(exit 3)。新增引用被擋下。
- 實測 **<** `max_count` → 預設只提示「該下修了」。因為清掉引用的 PR 不該被
  自己的成果擋住,但**上限不會自己降** —— 靜默鬆動就是這個機制要防的事。
  Wave 收尾用 `--strict-ratchet` 跑一次,沒下修的上限會 fail。

`max_count` 一律 = **當下實測值**,只准降不准升。要升必須在 PR 描述寫理由,
而 reviewer 看得到 diff 裡的數字變大。

exit code 沿用 `check_orm_pg_drift.py` 的約定,讓 CI 能分清「腳本自己壞了」
與「真的有 findings」—— 這個區分是踩過坑才加的(`|| echo "::warning::"`
曾經把 `ModuleNotFoundError` 偽裝成綠燈)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_BROKEN = 1  # 腳本自己壞了 / ledger 檔不合法 —— 絕對不能被當成「沒 findings」
EXIT_USAGE = 2
EXIT_FINDINGS = 3

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER = REPO_ROOT / "infra" / "ci" / "legacy_ledger.json"

REQUIRED_FIELDS = (
    "id",
    "pattern",
    "scope",
    "max_count",
    "owner",
    "exit_condition",
)


def _iter_files(scope: list[str], exclude: list[str]) -> list[Path]:
    """展開 scope glob,扣掉 exclude glob。相對 repo root。"""
    seen: dict[Path, None] = {}
    for pat in scope:
        for path in REPO_ROOT.glob(pat):
            if path.is_file():
                seen[path] = None
    if exclude:
        dropped = set()
        for pat in exclude:
            for path in REPO_ROOT.glob(pat):
                dropped.add(path)
        for path in dropped:
            seen.pop(path, None)
    return sorted(seen)


def _count(entry: dict) -> tuple[int, list[str]]:
    """回傳(命中行數, 前幾筆命中位置)。逐行計數,一行多次命中只算一次。"""
    try:
        rx = re.compile(entry["pattern"])
    except re.error as exc:  # ledger 寫壞了是 BROKEN,不是 findings
        raise ValueError(f"{entry['id']}: pattern 不是合法 regex — {exc}") from exc

    hits: list[str] = []
    total = 0
    for path in _iter_files(entry["scope"], entry.get("exclude", [])):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                total += 1
                if len(hits) < 5:
                    rel = path.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{lineno}")
    return total, hits


def _validate(ledger: dict) -> list[str]:
    problems: list[str] = []
    entries = ledger.get("entries")
    if not isinstance(entries, list) or not entries:
        return ["ledger 的 entries 必須是非空 list"]
    seen_ids: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(f"entries[{i}] 不是 object")
            continue
        for field in REQUIRED_FIELDS:
            if field not in entry:
                problems.append(f"entries[{i}] 缺 {field}")
        eid = entry.get("id")
        if eid in seen_ids:
            problems.append(f"id 重複:{eid}")
        seen_ids.add(eid)
        if not isinstance(entry.get("scope"), list):
            problems.append(f"{eid}: scope 必須是 glob list")
        if not isinstance(entry.get("max_count"), int):
            problems.append(f"{eid}: max_count 必須是整數")
        # owner 空字串等於沒 owner —— 這個機制的失敗模式就是「大家都以為別人在顧」
        if not str(entry.get("owner", "")).strip():
            problems.append(f"{eid}: owner 不得為空")
        if not str(entry.get("exit_condition", "")).strip():
            problems.append(f"{eid}: exit_condition 不得為空(沒有退場條件的清單就是永久豁免)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="legacy 清單 ratchet 檢查(C5)")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument(
        "--strict-ratchet",
        action="store_true",
        help="實測低於上限也 fail(Wave 收尾用,強迫下修)",
    )
    ap.add_argument(
        "--report",
        action="store_true",
        help="只印實測值,永遠 exit 0(量測基線用)",
    )
    args = ap.parse_args(argv)

    ledger_path = Path(args.ledger)
    if not ledger_path.is_file():
        print(f"BROKEN: 找不到 ledger 檔 {ledger_path}", file=sys.stderr)
        return EXIT_BROKEN
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"BROKEN: ledger 不是合法 JSON — {exc}", file=sys.stderr)
        return EXIT_BROKEN

    problems = _validate(ledger)
    if problems:
        print("BROKEN: ledger 格式問題", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return EXIT_BROKEN

    over: list[str] = []
    under: list[str] = []
    print(f"{'id':<28} {'實測':>6} {'上限':>6}  狀態")
    print("-" * 64)
    for entry in ledger["entries"]:
        try:
            actual, hits = _count(entry)
        except ValueError as exc:
            print(f"BROKEN: {exc}", file=sys.stderr)
            return EXIT_BROKEN
        cap = entry["max_count"]
        if actual > cap:
            state = "✗ 超過上限"
            over.append(
                f"{entry['id']}: 實測 {actual} > 上限 {cap}(+{actual - cap})\n"
                f"    前幾筆:{', '.join(hits)}\n"
                f"    退場條件:{entry['exit_condition']}\n"
                f"    owner:{entry['owner']}"
            )
        elif actual < cap:
            state = "△ 可下修"
            under.append(f"{entry['id']}: 實測 {actual} < 上限 {cap} → 把 max_count 改成 {actual}")
        else:
            state = "✓"
        print(f"{entry['id']:<28} {actual:>6} {cap:>6}  {state}")

    if args.report:
        return EXIT_OK

    if over:
        print("\n新增了 legacy 引用(C5 ratchet 只准降不准升):", file=sys.stderr)
        for line in over:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\n若這是刻意的(例如為了先修更嚴重的問題),請在 PR 描述寫明理由並"
            "調高 infra/ci/legacy_ledger.json 的 max_count —— reviewer 看得到數字變大。",
            file=sys.stderr,
        )
        return EXIT_FINDINGS

    if under:
        header = "上限該下修了" if args.strict_ratchet else "提示:上限可下修(非 fail)"
        stream = sys.stderr if args.strict_ratchet else sys.stdout
        print(f"\n{header}:", file=stream)
        for line in under:
            print(f"  - {line}", file=stream)
        if args.strict_ratchet:
            return EXIT_FINDINGS
        print("\n未新增 legacy 引用(有可下修的上限,見上方)。")
        return EXIT_OK

    print("\n全部剛好在上限上,未新增 legacy 引用。")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 任何未預期例外都是 BROKEN,不是「沒問題」
        print(f"BROKEN: 未預期例外 {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(EXIT_BROKEN)
