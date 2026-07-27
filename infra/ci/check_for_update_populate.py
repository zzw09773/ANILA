#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`with_for_update()` 必須鏈上 `populate_existing()` —— 地雷解除器。

## 為什麼需要這支

2026-07-25 的獨立審查用 SQLAlchemy 2.0.43 實測腳本抓到一條 CRITICAL:

    expire_on_commit=True   legal_hold_seen=True   → BLOCKS erase(正確)
    expire_on_commit=False  legal_hold_seen=False  → ERASES despite hold(BUG)

核心 ORM 語意:**Query 若命中 identity map 且物件未過期,SQLAlchemy 會丟棄剛
SELECT 回來的列值,回傳記憶體舊值。** `populate_existing()` 是唯一的強制覆寫;
在它之前,「commit 使物件過期」是讓重讀生效的**隱式**機制。

也就是說 `SELECT ... FOR UPDATE` 這個動作的**全部意義**——鎖住列、讀到最新
值再決定——會在 `expire_on_commit=False` 之下靜默消失。鎖還在,讀到的是舊值。

## 現在的處境(2026-07-26 實測修正:地雷**已經上膛**,不是等 PR #51)

- `expire_on_commit=True`(SQLAlchemy 預設)只在**自己 commit** 時讓物件過期,
  所以「A 先讀 → B(另一 session)改並 commit → A 未 commit 就加鎖重讀」在預設
  之下**同樣讀到舊值**(真 PG 雙 session 實測,
  見 `services/csp/tests/test_for_update_staleness_pg.py`)。
- `retention_reaper` 正是這個形狀(先掃候選、再逐筆加鎖重讀)→ 這是**現行**的
  legal hold 繞過(**不可逆刪檔 + 法務合規**),`task_link` 終態冪等、Gate 5
  receipt drift 偵測同族。
- 工單 G(PR #51,`fix/stream-session-pool`)把 `SessionLocal` 改成
  `expire_on_commit=False` —— 那是**放大器**(連自己 commit 後都不過期),
  不是引信。baseline 降到 0 之前 #51 不得 merge。

## 為什麼是 AST 而不是 grep

要判斷的是「**同一條 query chain** 上有沒有 `populate_existing()`」,而寫法有
好幾種:多行鏈式、`query.with_for_update().one_or_none()` 單行、先存 query 變數
再加鎖。grep 只能看行,會同時產生誤報與漏報 —— 而漏報在這裡等於「說沒問題」。

本檔走 `ast`,對每個 `Call` 節點往上追它所屬的完整屬性鏈,再問這條鏈上有沒有
`populate_existing`。變數形式(`query = db.query(...)` 之後 `query.with_for_update()`)
會**保守地算成缺少** —— 誤報的代價是有人多看一眼,漏報的代價是法務合規。

## 為什麼還有 ratchet

117 個站點不可能一次改完並逐處複審完。所以與 C5 ledger 同一套紀律:
上限 = 當下實測值,**只准降不准升**。新程式碼不准再長出一個。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2
EXIT_FINDINGS = 3

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = Path(__file__).with_name("for_update_populate_baseline.json")

SCAN_ROOTS = (
    "services/csp/app",
    "packages/anila-core/src",
    "services/ingestion-worker/src",
)


def _chain_names(node: ast.AST) -> list[str]:
    """把一條 `a.b().c().d()` 的鏈攤成 ['a','b','c','d'](順序不重要)。

    從任一節點往**內**走(func → value),收集所有 attribute 名稱。
    """
    names: list[str] = []
    cur = node
    while True:
        if isinstance(cur, ast.Call):
            cur = cur.func
        elif isinstance(cur, ast.Attribute):
            names.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Name):
            names.append(cur.id)
            break
        else:
            break
    return names


def _outermost_chain(tree: ast.AST) -> dict[int, ast.AST]:
    """對每個節點記錄它的 parent,方便從 `with_for_update` 往外走到整條鏈頭。"""
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def scan_file(path: Path) -> list[dict]:
    try:
        src = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        raise ValueError(f"{path}: 無法解析 — {exc}") from exc

    parents = _outermost_chain(tree)
    findings: list[dict] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "with_for_update"):
            continue

        # 從這個 Call 往外走到整條 attribute/call 鏈的最外層,這樣
        # `.with_for_update().one_or_none()` 之後才接的 `populate_existing`
        # 也算得到。
        #
        # ⚠ 走的規則必須是「**同一條鏈**」而不是「parent 是 Attribute 或 Call」。
        # 第一版寫成後者,於是 `cast(X | None, db.query(...)....one_or_none())`
        # 這種把鏈當**參數**傳進另一個呼叫的寫法,會一路走到 `cast` 那個 Call,
        # 而 `cast` 的 func 是 `Name('cast')` → 整條鏈的名字全丟掉 → 誤報。
        # 實例:`services/proxy/session_event_store.py:201-206` 明明已經有
        # `.populate_existing()`,卻被算成缺少。
        #
        # 正確規則:只有當 node 是 parent 的 **func / value**(也就是鏈的延續)
        # 才往外走;當 node 是 parent 的**引數**就停。
        outer: ast.AST = node
        while True:
            parent = parents.get(id(outer))
            if isinstance(parent, ast.Attribute) and parent.value is outer:
                outer = parent
                continue
            if isinstance(parent, ast.Call) and parent.func is outer:
                outer = parent
                continue
            break

        names = set(_chain_names(outer))
        if "populate_existing" in names:
            continue

        findings.append(
            {
                "file": str(path.relative_to(REPO_ROOT)),
                "line": node.lineno,
            }
        )
    return findings


def collect() -> list[dict]:
    findings: list[dict] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            rel = str(path.relative_to(REPO_ROOT))
            if "/tests/" in rel or Path(rel).name.startswith("test_"):
                continue
            findings.extend(scan_file(path))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true",
                    help="只印實測值,永遠 exit 0(量測基線用)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="把實測值寫成新 baseline(只准在數字**下降**時用)")
    args = ap.parse_args(argv)

    try:
        findings = collect()
    except ValueError as exc:
        print(f"BROKEN: {exc}", file=sys.stderr)
        return EXIT_BROKEN

    per_file: dict[str, int] = {}
    for f in findings:
        per_file[f["file"]] = per_file.get(f["file"], 0) + 1

    print(f"with_for_update 缺 populate_existing:{len(findings)} 處 / {len(per_file)} 檔")
    for name, n in sorted(per_file.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:3d}  {name}")

    if args.report:
        return EXIT_OK

    if args.write_baseline:
        BASELINE.write_text(
            json.dumps(
                {
                    "_readme": [
                        "with_for_update() 缺 populate_existing() 的站點數上限。",
                        "由 infra/ci/check_for_update_populate.py 在 CI 執行。",
                        "背景:SQLAlchemy 的 Query 命中 identity map 且物件未過期時會**丟棄**",
                        "剛 SELECT 回來的列值。所以 SELECT ... FOR UPDATE 的全部意義",
                        "(鎖住列、讀到最新值再決定)會靜默消失 —— 鎖還在,讀到的是舊值。",
                        "⚠ 這在預設 expire_on_commit=True 之下就成立(跨 session 未 commit",
                        "重讀,真 PG 實測見 test_for_update_staleness_pg.py);PR #51 的",
                        "expire_on_commit=False 只是放大器,不是引信。已確認被擊穿的包含",
                        "retention_reaper 的 legal hold(不可逆刪檔 + 法務合規)。",
                        "上限只准降不准升。降到 0 之前 PR #51 不得 merge 進 main。",
                    ],
                    "max_count": len(findings),
                    "per_file": per_file,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"\nbaseline 已寫入 {BASELINE.name}(max_count={len(findings)})")
        return EXIT_OK

    if not BASELINE.is_file():
        print(f"BROKEN: 找不到 baseline {BASELINE}", file=sys.stderr)
        return EXIT_BROKEN
    try:
        cap = json.loads(BASELINE.read_text(encoding="utf-8"))["max_count"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"BROKEN: baseline 不可用 — {exc}", file=sys.stderr)
        return EXIT_BROKEN

    if len(findings) > cap:
        print(
            f"\n新增了缺 populate_existing() 的 with_for_update 站點"
            f"({cap} → {len(findings)})。\n"
            f"SELECT ... FOR UPDATE 若不接 populate_existing(),Query 命中 identity "
            f"map 時會丟棄剛 SELECT 回來的列值、回傳記憶體舊值 —— 鎖還在,但讀到的\n"
            f"是舊值,於是「加鎖重讀再決定」這個動作的全部意義消失。\n"
            f"⚠ 這不只在 expire_on_commit=False 才發生。那個旗標只在**自己** commit "
            f"時讓物件過期,\n"
            f"   所以「A 先讀 → B 改並 commit → A 未 commit 就重讀」在預設 True 之下"
            f"同樣讀到舊值(已實測,\n"
            f"   見 services/csp/tests/test_for_update_staleness_pg.py)。\n"
            f"請在該 query chain 上加 `.populate_existing()`。",
            file=sys.stderr,
        )
        return EXIT_FINDINGS

    if len(findings) < cap:
        print(f"\n上限可下修:{cap} → {len(findings)}"
              f"(跑 --write-baseline)。降到 0 之前 PR #51 不得 merge。")
    else:
        print("\n未新增。")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"BROKEN: 未預期例外 {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(EXIT_BROKEN)
