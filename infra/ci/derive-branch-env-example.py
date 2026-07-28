#!/usr/bin/env python3
"""從 main 的 `.env.example` 推導出部署分支的版本 —— 讓「語意重推導」變成可執行的。

## 為什麼需要這支

`AGENTS.md` §3 與 `CLAUDE.md` §6 都寫著同一條鐵則:跨分支同步時,各部署分支的
`.env.example` 姿態**不可被 main 覆蓋**,而且更新姿態必須「語意重推導」——
以 main 現行範本為基底、只覆寫該分支蓄意的值,**禁止直接 apply 舊 diff**。

那條規則是用血換來的:2026-07-22 的廢棄 sync 分支正是因為套了舊 diff,把
`prod-intranet-card` 的 `ENABLE_PUBLIC_SHARE=false` 與 `ENABLE_MEMORY=false`
弄丟了 —— 也就是在一個卡登內網部署上,悄悄打開了匿名分享連結與長期記憶。

但那條規則到目前為止只是**寫在文件裡的一段話**,靠每次同步的人記得。這支把它
變成程式:姿態宣告在下方的表裡,推導是機械的,而且可以 `--check` 進 CI。
新增變數時,main 的範本會自動帶過去(因為推導是「以 main 為基底」),不會像
diff replay 那樣把新東西漏掉、或把舊值蓋回去。

## 用法

    # 產生 prod-intranet-card 的版本(印到 stdout)
    python3 infra/ci/derive-branch-env-example.py prod-intranet-card

    # 寫回檔案(在該分支上執行)
    python3 infra/ci/derive-branch-env-example.py prod-intranet-card --write

    # 驗證目前檔案與推導結果一致(CI 用;不一致 exit 3)
    python3 infra/ci/derive-branch-env-example.py prod-intranet-card --check

`--base` 預設讀 `origin/main:.env.example`;離線或想比對工作區時可指向本地檔。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"


# ── 各部署分支蓄意的姿態 ──────────────────────────────────────────────────
#
# 只列「與 main 不同」的項目。沒列到的一律沿用 main —— 這正是重推導與 diff
# replay 的差別:main 新增的變數會自動流過來,而這裡列的值永遠贏。
#
# 改這張表 = 改變一個正式部署的安全姿態。每一項都附理由,因為半年後看到
# `false` 而不知道為什麼,就會有人「順手」把它改成 `true`。
POSTURES: dict[str, dict[str, object]] = {
    "prod-intranet-card": {
        "_title": "prod-intranet-card — 中科院內網卡登正式部署",
        "_purpose": "卡片唯一登入、正式 secret 與 HTTPS endpoint 的 fail-closed 基線。",
        "values": {
            # 正式姿態:dev 預設值一律拒絕啟動,不是 warn。
            "ANILA_ALLOW_DEV_SECRET": "0",
            "ANILA_DEPLOYMENT_PROFILE": "prod-intranet-card",
            "ANILA_ENV": "production",
            # 出向 https 強制。⚠ 注意 agent 專用旗標**不在此列** ——
            # 內網 MLSteam agent 是純 http NodePort,那條必須維持 1。
            "ANILA_ALLOW_HTTP_ENDPOINT": "0",
            # 卡登唯一。REQUIRE_CARD_LOGIN_ONLY=true 會讓帳密註冊/登入回 404。
            "ENABLE_CARD_LOGIN": "true",
            "REQUIRE_CARD_LOGIN_ONLY": "true",
            # ⚠ 這兩個就是 2026-07-22 被舊 diff 弄丟的那兩個。
            # 匿名分享連結:內網部署不允許未登入可讀的連結。
            "ENABLE_PUBLIC_SHARE": "false",
            # 長期記憶:跨對話的記憶會把高密等內容帶進低密等脈絡。
            "ENABLE_MEMORY": "false",
            # 撤銷清單強制:沒有它,被撤銷的卡還能登入。
            "CARD_CRL_REQUIRED": "true",
            # 匯出稽核:正式部署必須留匯出紀錄(Wave 1 要求)。
            "ANILA_EXPORT_RECORD_REQUIRED": "true",
            # agent trace-test 證據的有效期。預設 86400 = 24 小時,
            # 過期後 agent 會**靜默**從可派工清單消失(無錯誤、無通知)。
            # 正式部署拉到 7 天上限,改用排程重跑 trace-test 而不是靠運氣。
            "AGENT_TRACE_TEST_FRESHNESS_SECONDS": "604800",
            # pilot 模式必須關:開啟時 GET /v1/agents 直接回空陣列。
            "ANILA_PILOT_MODE": "false",
        },
    },
    "prod-military-passwd": {
        "_title": "prod-military-passwd — 帳密登入正式部署",
        "_purpose": "帳密登入、正式 secret 與 HTTPS endpoint 的 fail-closed 基線。",
        "values": {
            "ANILA_ALLOW_DEV_SECRET": "0",
            "ANILA_DEPLOYMENT_PROFILE": "prod-military-passwd",
            "ANILA_ENV": "production",
            "ANILA_ALLOW_HTTP_ENDPOINT": "0",
            "ENABLE_CARD_LOGIN": "false",
            "REQUIRE_CARD_LOGIN_ONLY": "false",
            "ENABLE_PUBLIC_SHARE": "false",
            "ENABLE_MEMORY": "false",
            "ANILA_EXPORT_RECORD_REQUIRED": "true",
            "ANILA_PILOT_MODE": "false",
        },
    },
}


def _read_base(base: str) -> str:
    """讀基底範本。`ref:path` 形式走 git,否則當本地路徑。"""
    if ":" in base and not Path(base).exists():
        proc = subprocess.run(
            ["git", "show", base], cwd=ROOT, capture_output=True, encoding="utf-8"
        )
        if proc.returncode != 0:
            raise SystemExit(f"讀不到 {base}: {proc.stderr.strip()}")
        return proc.stdout
    return Path(base).read_text(encoding="utf-8")


def derive(base_text: str, branch: str) -> str:
    posture = POSTURES.get(branch)
    if posture is None:
        raise SystemExit(
            f"未知分支 {branch!r};已宣告的有: {', '.join(sorted(POSTURES))}"
        )
    values: dict[str, str] = posture["values"]  # type: ignore[assignment]

    out: list[str] = []
    seen: set[str] = set()
    for line in base_text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            key = m.group(1)
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)

    # main 範本裡沒有的姿態變數要補在檔尾,而不是靜默消失。
    missing = [k for k in values if k not in seen]
    if missing:
        out.append("")
        out.append("# ── 本分支專屬:main 範本尚未收錄的姿態變數 ──────────────────")
        out.append("# 這些出現在這裡,代表 main 的範本還沒有它們。若 main 之後收錄了,")
        out.append("# 重新推導時會自動移回原位,這一段會空掉。")
        for key in missing:
            out.append(f"{key}={values[key]}")

    text = "\n".join(out) + "\n"

    # 檔頭身分:讓人打開檔案第一眼就知道自己在哪條分支。
    text = text.replace(
        "# === [分支設定檔] main — 通用開發基線 (SSOT) ===",
        f"# === [分支設定檔] {posture['_title']} ===",
        1,
    )
    text = text.replace(
        "# 用途:帳密／OIDC、隔離 dev stack 與通用功能開發；不可作正式部署設定。",
        f"# 用途:{posture['_purpose']}",
        1,
    )
    text = text.replace(
        "# ANILA Platform — main 開發用 root .env 範本",
        f"# ANILA Platform — {branch} root .env 範本",
        1,
    )
    text += (
        "\n# 本檔由 infra/ci/derive-branch-env-example.py 從 main 的範本推導產生。\n"
        "# 要改姿態請改那支腳本裡的 POSTURES 表再重新推導,不要手改本檔 ——\n"
        "# 手改會在下次同步時被靜默蓋掉,而那正是 2026-07-22 弄丟\n"
        "# ENABLE_PUBLIC_SHARE=false / ENABLE_MEMORY=false 的原因。\n"
    )
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("branch", choices=sorted(POSTURES))
    ap.add_argument("--base", default="origin/main:.env.example")
    ap.add_argument("--write", action="store_true", help="寫回 .env.example")
    ap.add_argument("--check", action="store_true", help="比對現況,不一致 exit 3")
    args = ap.parse_args()

    derived = derive(_read_base(args.base), args.branch)

    if args.check:
        current = ENV_EXAMPLE.read_text(encoding="utf-8")
        if current == derived:
            print(f"[PASS] .env.example 與 {args.branch} 的姿態推導一致")
            return 0
        print(f"[FAIL] .env.example 與 {args.branch} 的姿態推導不一致。", file=sys.stderr)
        print("       重新推導:", file=sys.stderr)
        print(
            f"       python3 infra/ci/derive-branch-env-example.py {args.branch} --write",
            file=sys.stderr,
        )
        import difflib

        for line in difflib.unified_diff(
            current.splitlines(), derived.splitlines(),
            fromfile="現況", tofile="推導結果", lineterm="", n=1,
        ):
            print("       " + line, file=sys.stderr)
        return 3

    if args.write:
        ENV_EXAMPLE.write_text(derived, encoding="utf-8")
        print(f"已寫入 {ENV_EXAMPLE.relative_to(ROOT)}({args.branch} 姿態)")
        return 0

    sys.stdout.write(derived)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
