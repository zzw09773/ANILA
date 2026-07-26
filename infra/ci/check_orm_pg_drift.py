#!/usr/bin/env python3
"""ORM ↔ 真 Postgres 漂移檢查 + naive DateTime 政策斷言 —— W0-1(補救計畫 Wave 0)。

為什麼需要這支
--------------
`services/csp/app/services/startup_migrations.py` 是 alembic **之外的第二套
schema 機制**(覆蓋 users / model_registry / token_usage / departments /
api_keys / alerts / audit_logs / platform_links 八張表,coverage 21%),docstring
自承「The 0001 alembic baseline does not match the current SQLAlchemy models」。
四個後果:

1. `alembic upgrade head` 到乾淨 DB **得不到可用 schema** → DR 還原是壞的
2. `alerts.fingerprint` 宣告了 UNIQUE 但生產沒有 → 告警去重在併發下失效,
   且 `health_checker` 每 60 秒對每個 model/agent 全表掃
3. 93 個 naive timestamp 欄(含整套五級分類治理帳)
4. `ingestion_collections.created_by` = NOT NULL + ON DELETE SET NULL →
   硬刪使用者必定 IntegrityError

而測試用 SQLite + `Base.metadata.create_all`(不跑 alembic)→ 驗證迴路**結構上
看不見以上任何一條**。

兩種檢查刻意分開
----------------
**--mode drift**:ORM metadata vs 真 PG。抓 缺表 / 缺欄 / 缺 index / 缺 UNIQUE /
缺 FK / **timestamp 的時區屬性不一致**(`TZ_MISMATCH`)。

⚠ **刻意不做通用型別比對**。第一版 docstring 宣稱抓「型別不符」而實作只比對欄位
名稱存在(由 PR #52 的 Codex review 抓到)。通用比對在這個 codebase 會大量誤報
—— `user_memory.embedding` 是 ORM `Text` 對應 SQL `halfvec`、
`JSON().with_variant(JSONB)` 兩邊名稱也不同 —— 需要一長串 allow-list,而
allow-list 一長就等於什麼都放行。所以只比對 tz 屬性:那是本次補救真正在追的
一項(W2-10 的 93 欄轉換若只改一邊就會被這條抓到)。

**--mode policy**:純靜態,不需要 DB。目前只有一條規則:**所有 `DateTime` 欄
必須宣告 `timezone=True`**。

⚠ 為什麼政策斷言不能靠 drift 抓:`classification_events.created_at` 的 ORM 是
`Column(DateTime, ...)`(naive)、PG 是 `timestamp without time zone`(naive)
——**兩邊相符**,diff 永遠不報。全 models 是 naive 104 vs aware 53,病根是
「ORM 與 DB 一起錯」,不是漂移。這個區別是 rev.2 缺漏審查抓到的:原本 W0-1 的
驗收條件寫「baseline 含 93 筆 timestamp 型別不符」,那在結構上做不到。

⚠ 使用注意:CI 版必須跑在**乾淨 DB 且已 `alembic upgrade head` + 跑過
`run_startup_migrations()`** 之上,否則抓到的是「這個庫剛好缺什麼」而不是
「migration chain 產不出什麼」。對既有庫唯讀跑也有價值(可看現況),但不能當
migration chain 的驗收。

用法
----
    # 政策斷言(不需要 DB)
    python infra/ci/check_orm_pg_drift.py --mode policy

    # 漂移檢查(需要 DSN)
    python infra/ci/check_orm_pg_drift.py --mode drift \\
        --dsn postgresql://user:pw@host:5432/csp

    # 產生/更新 baseline(只准縮不准增,ratchet)
    python infra/ci/check_orm_pg_drift.py --mode policy --write-baseline

Exit code(刻意讓「有發現」與「檢查本身壞了」不同碼)
---------------------------------------------------
    0  乾淨
    3  有發現(drift 條目 / 新增 naive 欄)—— 呼叫端可自行決定 warn 或 fail
    2  用法錯誤(缺 --dsn、baseline 不存在)
    1  **檢查本身失敗**(連不上 DB、缺 driver、import 失敗…)

⚠ 為什麼要把 3 和 1 分開:第一版 CI 步驟寫成
`python check_orm_pg_drift.py … || echo "::warning::drift 存在"`,結果腳本因為
`ModuleNotFoundError: No module named 'psycopg'` 直接 crash,而那個 `||` 把
traceback 一起吞掉 —— job 顯示通過,看起來像「有 drift 但已知」,實際上**檢查
從來沒跑成**。Python 對未捕捉例外也是 exit 1,所以光靠 exit code 0/1 分不出
「找到問題」和「壞掉」。這就是稽核指的「吞掉錯誤讓紅燈變綠燈」,而我自己踩了。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CSP_ROOT = REPO_ROOT / "services" / "csp"
BASELINE_PATH = Path(__file__).resolve().parent / "orm_drift_baseline.json"

# 「找到問題」與「檢查本身壞了」必須是不同的 exit code —— 見模組 docstring。
EXIT_OK = 0
EXIT_BROKEN = 1   # 未捕捉例外也是 1,所以 1 一律代表「這支檢查不可信」
EXIT_USAGE = 2
EXIT_FINDINGS = 3


def _load_metadata():
    """Import CSP 的 SQLAlchemy metadata。"""
    sys.path.insert(0, str(CSP_ROOT))
    from app.database import Base  # noqa: E402  (path 必須先設好)
    import app.models  # noqa: F401,E402  (讓所有 model 註冊進 metadata)

    return Base.metadata


# ── 政策斷言 ───────────────────────────────────────────────────────────────
def collect_naive_datetime_columns(metadata) -> list[str]:
    """列出所有未宣告 timezone=True 的 DateTime 欄(`table.column` 形式)。"""
    from sqlalchemy import DateTime

    out: list[str] = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        for col in table.columns:
            type_ = col.type
            if isinstance(type_, DateTime) and not getattr(type_, "timezone", False):
                out.append(f"{table.name}.{col.name}")
    return out


def run_policy(write_baseline: bool) -> int:
    metadata = _load_metadata()
    naive = collect_naive_datetime_columns(metadata)

    payload = {
        "_note": (
            "W0-1 政策段:未宣告 timezone=True 的 DateTime 欄。只准縮不准增"
            "(ratchet)。逐批清除見補救計畫 W2-10 / 子計畫 C1。"
            "⚠ W2-10 必須在同一個 PR 內同時改 PG 型別與 ORM 宣告,否則只改一邊"
            "會讓剛做完的正確工作反而觸發 drift 告警。"
        ),
        "work_package": "W2-10",
        "naive_datetime_columns": naive,
    }

    if write_baseline:
        BASELINE_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Wrote baseline: {len(naive)} naive DateTime columns → {BASELINE_PATH.name}")
        return 0

    if not BASELINE_PATH.exists():
        print(f"Missing baseline {BASELINE_PATH}; run with --write-baseline first", file=sys.stderr)
        return EXIT_USAGE

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    allowed = set(baseline.get("naive_datetime_columns", []))
    current = set(naive)

    added = sorted(current - allowed)
    removed = sorted(allowed - current)

    print(
        f"Policy[naive DateTime]: {len(current)} current / {len(allowed)} baseline"
        f" (+{len(added)} / -{len(removed)})"
    )
    for col in removed:
        print(f"  ✔ {col} 已改為 timezone=True → 請從 baseline 移除")
    for col in added:
        print(f"  ✖ NEW naive DateTime: {col}", file=sys.stderr)

    if added:
        print(
            "\n新增 naive DateTime 欄會延續「治理帳時間無時區標記」這個缺陷。"
            "新欄位請一律宣告 DateTime(timezone=True)。",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    return 0


# ── 漂移檢查 ───────────────────────────────────────────────────────────────
def run_drift(dsn: str) -> int:
    from sqlalchemy import create_engine, inspect

    metadata = _load_metadata()
    engine = create_engine(dsn)
    insp = inspect(engine)

    db_tables = set(insp.get_table_names())
    findings: list[dict] = []

    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        if table.name not in db_tables:
            findings.append({"kind": "MISSING_TABLE", "target": table.name})
            continue

        db_cols = {c["name"]: c for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name not in db_cols:
                findings.append({"kind": "MISSING_COLUMN", "target": f"{table.name}.{col.name}"})
                continue
            # ── timestamp 的時區屬性比對 ────────────────────────────────────
            #
            # 刻意**只**比對這一項,不做通用型別比對。理由:
            #   - 通用比對在這個 codebase 會大量誤報 —— `user_memory.embedding`
            #     是 ORM `Text` 對應 SQL `halfvec`、`JSON().with_variant(JSONB)`
            #     兩邊名稱也不同,要維護一長串 allow-list 才能用,而 allow-list
            #     一長就會變成「什麼都放行」。
            #   - 而 tz 屬性是**本次補救真正在追的那一項**:W2-10 要把 93 個
            #     naive 欄轉 timestamptz,若只改 PG 沒改 ORM(或反之),就會出現
            #     「一邊 aware 一邊 naive」的中間狀態 —— 那正是這條要抓的。
            #
            # 第一版的 docstring 宣稱會抓「型別不符」而實作只比對欄位名稱,
            # 由 PR #52 的 Codex review 抓到。現在敘述與實作一致:只比 tz。
            from sqlalchemy import DateTime as _DateTime

            orm_type = col.type
            if isinstance(orm_type, _DateTime):
                orm_tz = bool(getattr(orm_type, "timezone", False))
                db_type_name = str(db_cols[col.name]["type"]).upper()
                db_tz = "WITH TIME ZONE" in db_type_name or "TIMESTAMPTZ" in db_type_name
                if orm_tz != db_tz:
                    findings.append({
                        "kind": "TZ_MISMATCH",
                        "target": f"{table.name}.{col.name}",
                        "detail": (
                            f"ORM timezone={orm_tz}, DB={db_type_name} "
                            "→ 只改了一邊;W2-10 必須在同一個 PR 內同時改"
                        ),
                    })

        # index / unique:比對「宣告要有」與「DB 實際有」的欄位集合
        db_index_cols = {
            tuple(ix["column_names"]) for ix in insp.get_indexes(table.name)
        }
        db_unique_cols = {
            tuple(uc["column_names"]) for uc in insp.get_unique_constraints(table.name)
        }
        pk_cols = tuple(insp.get_pk_constraint(table.name).get("constrained_columns") or ())
        for col in table.columns:
            single = (col.name,)
            if col.index and single not in db_index_cols and single != pk_cols:
                findings.append({"kind": "MISSING_INDEX", "target": f"{table.name}.{col.name}"})
            if col.unique and single not in db_unique_cols and single != pk_cols:
                findings.append({"kind": "MISSING_UNIQUE", "target": f"{table.name}.{col.name}"})

        # FK:比對**約束形狀**,不是數量。
        #
        # 這裡原本是 `sum(len(c.foreign_keys) for c in table.columns)` 對
        # `len(insp.get_foreign_keys(...))`,兩個都是計數 —— 而且兩邊數的不是
        # 同一種東西:前者數「參與 FK 的欄位」,後者數「FK 約束」。所以**任何
        # 含複合 FK 的表都必然 declared > actual**,永遠誤報:
        #
        #   document_relations              declared 5 / db 3  (fk_docrel_src、dst 各 2 欄)
        #   ingestion_documents             declared 6 / db 4  (active_generation 複合 3 欄)
        #   ingestion_document_generations  declared 3 / db 2
        #
        # 這三條是 W2-6 收編完之後唯一剩下的非 TZ findings,而**沒有任何
        # migration 能讓它們歸零**(除了加冗餘單欄 FK 去湊數字)—— 也就是說
        # 這個誤報直接擋住 drift gate 轉為硬性 fail。
        #
        # 更糟的是計數比對的另一面:它抓不到「FK 存在但指錯目標」。DB 有 3 條
        # FK、ORM 宣告 3 條,即使其中一條指到完全不同的表,計數也相等 → 綠燈。
        # 改比形狀之後這種情況會被抓出來(見 tests 的 seed 反證)。
        orm_fk_shapes = {}
        for fkc in table.foreign_key_constraints:
            elements = list(fkc.elements)
            if not elements:
                continue
            shape = (
                elements[0].column.table.name,
                frozenset((fk.parent.name, fk.column.name) for fk in elements),
            )
            orm_fk_shapes[shape] = fkc.name or "(未命名)"

        db_fk_shapes = set()
        for fk in insp.get_foreign_keys(table.name):
            constrained = fk.get("constrained_columns") or []
            referred_cols = fk.get("referred_columns") or []
            if not constrained or not referred_cols:
                continue
            db_fk_shapes.add(
                (fk["referred_table"], frozenset(zip(constrained, referred_cols)))
            )

        for shape, name in sorted(orm_fk_shapes.items(), key=lambda kv: str(kv[0])):
            if shape in db_fk_shapes:
                continue
            referred, pairs = shape
            cols = ", ".join(f"{a}→{referred}.{b}" for a, b in sorted(pairs))
            findings.append(
                {
                    "kind": "MISSING_FK",
                    "target": f"{table.name} [{name}]",
                    "detail": cols,
                }
            )

        # DB 有、ORM 沒宣告 —— 同樣是漂移,方向相反。列出來(不列的話「ORM 與 DB
        # 一致」這個宣稱只成立一半)。
        for referred, pairs in sorted(db_fk_shapes - set(orm_fk_shapes), key=str):
            cols = ", ".join(f"{a}→{referred}.{b}" for a, b in sorted(pairs))
            findings.append(
                {
                    "kind": "UNDECLARED_FK",
                    "target": table.name,
                    "detail": cols,
                }
            )

    print(f"Drift: inspected {len(metadata.tables)} ORM tables against DB — {len(findings)} findings")
    for f in findings:
        detail = f" ({f['detail']})" if "detail" in f else ""
        print(f"  {f['kind']:16s} {f['target']}{detail}")

    return _classify(findings)


# ── findings 分級 ──────────────────────────────────────────────────────────────
#
# 為什麼需要分級:這支原本是「有任何 finding 就 exit 3」,而 TZ_MISMATCH 有 53 條
# (那是 W2-10 的欄位型別工程,不是這裡能修的)→ CI 只能把整個 drift 步驟降級成
# warning。結果是**結構性漂移(缺欄/缺索引/缺 FK)也沒有人擋**,而那才是會讓
# `alembic upgrade head` 不自足、DR 還原壞掉的那類。
#
# 所以拆兩級:
#   - STRUCTURAL:缺欄 / 缺索引 / 缺 UNIQUE / 缺 FK → **硬 fail**。W2-6 收編完之後
#     這一級是 0,所以現在就能硬起來,而不是等 W2-10。
#   - DEFERRED:已知、已歸戶、有退場條件的類別 → 只警告,**但有計數上限**,超過
#     一樣 fail。上限只准降(與 C5 legacy ledger 同一套紀律)。
#
# 沒有第三種「就是不管」的類別 —— 那等於白名單無限大。
STRUCTURAL_KINDS = (
    "MISSING_TABLE",
    "MISSING_COLUMN",
    "MISSING_INDEX",
    "MISSING_UNIQUE",
    "MISSING_FK",
)
# ⚠ 不是 `orm_drift_baseline.json` —— 那個檔名已經是 --mode policy 的 naive
# DateTime ratchet 清單(118 欄)。這裡是 drift 模式的 DEFERRED 級上限,另立一檔。
DRIFT_BASELINE = Path(__file__).with_name("orm_drift_deferred_baseline.json")


def _classify(findings: list[dict]) -> int:
    structural = [f for f in findings if f["kind"] in STRUCTURAL_KINDS]
    deferred = [f for f in findings if f["kind"] not in STRUCTURAL_KINDS]

    counts: dict[str, int] = {}
    for f in deferred:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1

    caps: dict[str, int] = {}
    if DRIFT_BASELINE.is_file():
        try:
            caps = {
                k: v["max_count"]
                for k, v in json.loads(
                    DRIFT_BASELINE.read_text(encoding="utf-8")
                )["deferred"].items()
            }
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            print(f"BROKEN: drift baseline 不可用 — {exc}", file=sys.stderr)
            return EXIT_BROKEN

    print("\n分級:")
    print(f"  STRUCTURAL(硬 fail)  {len(structural)}")
    for kind, n in sorted(counts.items()):
        cap = caps.get(kind)
        flag = "" if cap is None else (f" / 上限 {cap}" + (" ✗ 超過" if n > cap else ""))
        print(f"  DEFERRED {kind:14s} {n}{flag}")

    failed = False
    if structural:
        print(
            "\n結構性漂移 —— ORM 宣告了 DB 沒有的東西。這類會讓 "
            "`alembic upgrade head` 不自足(DR 還原時 schema 不完整):",
            file=sys.stderr,
        )
        for f in structural:
            detail = f" ({f['detail']})" if "detail" in f else ""
            print(f"  - {f['kind']} {f['target']}{detail}", file=sys.stderr)
        failed = True

    for kind, n in sorted(counts.items()):
        cap = caps.get(kind)
        if cap is None:
            print(
                f"\n{kind} 有 {n} 條但 baseline 沒登記這一類 —— 未登記的類別不得存在"
                f"(否則白名單無限大)。請在 {DRIFT_BASELINE.name} 加一筆,附退場條件。",
                file=sys.stderr,
            )
            failed = True
        elif n > cap:
            print(
                f"\n{kind} 從 {cap} 增加到 {n} —— 上限只准降不准升。",
                file=sys.stderr,
            )
            failed = True

    return EXIT_FINDINGS if failed else EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=("policy", "drift"), required=True)
    ap.add_argument("--dsn", help="PostgreSQL DSN(--mode drift 必填)")
    ap.add_argument("--write-baseline", action="store_true", help="重寫政策 baseline")
    args = ap.parse_args()

    if args.mode == "policy":
        return run_policy(args.write_baseline)
    if not args.dsn:
        ap.error("--mode drift 需要 --dsn")
    return run_drift(args.dsn)


if __name__ == "__main__":
    sys.exit(main())
