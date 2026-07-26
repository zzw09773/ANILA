# -*- coding: utf-8 -*-
"""`check_legacy_ledger.py` 這個 gate 本身的測試 —— W1-7 / C5。

為什麼 gate 需要自己的測試
--------------------------
gate 的失敗模式不是「誤報」,是**靜默不報**。已經踩過一次:drift 檢查用
`|| echo "::warning::"` 包起來,結果 `ModuleNotFoundError` 被降級成 warning,
CI 全綠而檢查從來沒跑成過。所以這裡把三件事釘住:

1. 現況(乾淨 tree)→ exit 0;
2. seed 一處新的 legacy 引用 → exit 3 **而且訊息指出是哪一條、超了多少**;
3. ledger 檔本身壞掉 → exit 1(BROKEN),**不得**被誤判成「沒有 findings」。

第 3 條是重點:如果 ledger 壞掉時腳本回 0,這個 gate 就從「擋新增」退化成
「永遠說沒問題」,而沒有人會發現。

另外檢查 ledger 的資料完整性:八對俱全、每對都有 owner 與退場條件 —— 沒有
退場條件的清單就是永久豁免,那是 C5 要防的事情本身。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "infra" / "ci" / "check_legacy_ledger.py"
LEDGER = REPO_ROOT / "infra" / "ci" / "legacy_ledger.json"

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_FINDINGS = 3


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


class LegacyLedgerGateTest(unittest.TestCase):
    def test_clean_tree_passes(self):
        proc = _run()
        self.assertEqual(
            proc.returncode,
            EXIT_OK,
            f"乾淨 tree 應該過。stdout={proc.stdout}\nstderr={proc.stderr}",
        )

    def test_new_legacy_reference_fails_with_actionable_message(self):
        """seed 一處 `conv.classified` → 必須 exit 3 且訊息可行動。"""
        target = REPO_ROOT / "services" / "csp" / "app" / "api" / "attachments.py"
        original = target.read_text(encoding="utf-8")
        try:
            target.write_text(
                original
                + "\n\n# seed for W1-7 gate test\ndef _seed(conv):\n    return conv.classified\n",
                encoding="utf-8",
            )
            proc = _run()
            self.assertEqual(
                proc.returncode,
                EXIT_FINDINGS,
                f"新增 legacy 引用時 gate 必須擋下。stdout={proc.stdout}\nstderr={proc.stderr}",
            )
            combined = proc.stdout + proc.stderr
            # 只回一個 exit code 沒用 —— 修的人要知道是哪一條、退場條件是什麼
            self.assertIn("classified-boolean-backend", combined)
            self.assertIn("退場條件", combined)
            self.assertIn("owner", combined)
        finally:
            target.write_text(original, encoding="utf-8")

        # 還原後必須回到綠燈,否則這個測試會把 tree 弄髒而不自知
        self.assertEqual(_run().returncode, EXIT_OK, "還原後應回到 exit 0")

    def test_missing_ledger_is_broken_not_ok(self):
        proc = _run("--ledger", str(REPO_ROOT / "infra" / "ci" / "no-such-ledger.json"))
        self.assertEqual(proc.returncode, EXIT_BROKEN)
        self.assertIn("BROKEN", proc.stderr)

    def test_malformed_ledger_is_broken_not_ok(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{ this is not json")
            bad = fh.name
        try:
            proc = _run("--ledger", bad)
            self.assertEqual(proc.returncode, EXIT_BROKEN)
            self.assertIn("BROKEN", proc.stderr)
        finally:
            Path(bad).unlink(missing_ok=True)

    def test_entry_without_exit_condition_is_broken(self):
        """沒有退場條件的清單 = 永久豁免,那正是 C5 要防的事。"""
        led = json.loads(LEDGER.read_text(encoding="utf-8"))
        led["entries"][0]["exit_condition"] = ""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(led, fh, ensure_ascii=False)
            path = fh.name
        try:
            proc = _run("--ledger", path)
            self.assertEqual(proc.returncode, EXIT_BROKEN)
            self.assertIn("exit_condition", proc.stderr)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_ledger_covers_the_eight_known_pairs(self):
        led = json.loads(LEDGER.read_text(encoding="utf-8"))
        ids = {e["id"] for e in led["entries"]}
        expected = {
            "classified-boolean-backend",
            "classified-boolean-shell",
            "platform-links-vs-registered-service",
            "csp-service-token",
            "is-legacy-service-client",
            "legacy-runtime-call",
            "proxy-service-facade",
            "pbkdf2-100k-legacy-fallback",
            "sqlite-conftest-vs-pg",
        }
        self.assertEqual(expected - ids, set(), "C5 §3 的八對必須全部登記")
        for entry in led["entries"]:
            self.assertTrue(str(entry.get("owner", "")).strip(), f"{entry['id']} 缺 owner")
            self.assertTrue(
                str(entry.get("exit_condition", "")).strip(),
                f"{entry['id']} 缺退場條件",
            )
            self.assertIsInstance(entry.get("max_count"), int)


if __name__ == "__main__":
    unittest.main()
