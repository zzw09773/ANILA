"""備份可執行性契約 (W1-6)。

這批測試存在的理由:2026-07-26 稽核發現「相信自己有 DR、實際上沒有」——
照 runbook 部署完之後,每日 cron 因為 `production_backup.py` 的 `_required_env`
一個都沒被設而在第一行就死,週日 cron 因為 `--full` 不存在而 fatal,兩者都只寫進
沒人看的 log 檔。所以這裡機械化地釘住三件事:

1. `production_backup.py` 硬性要求的每一個環境變數,都在 `.env.example` 有對應鍵。
2. runbook 教的指令形式與 `anila-ops.sh` 的實際介面一致(沒有 `--full`、restore
   是雙參數 + 模式)。
3. 失敗看得見:backup 寫 heartbeat、health 檢查 heartbeat 年齡、`age` 進離線工具包。

也可以當獨立檢查腳本跑:
    python3 infra/deployment/tests/test_backup_env_contract.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROFILE = ROOT / "infra/deployment/backup/production-backup-profile.v1.json"
BACKUP_TOOL = ROOT / "infra/deployment/backup/production_backup.py"
OPS = ROOT / "infra/deployment/scripts/anila-ops.sh"
ENV_EXAMPLE = ROOT / ".env.example"
GUIDE = ROOT / "docs/runbooks/intranet-zero-to-prod-guide.md"
BACKUP_RUNBOOK = ROOT / "docs/runbooks/production-backup-restore.md"
TOOLKIT = ROOT / "infra/deployment/intranet/download-intranet-toolkit.sh"
RUNBOOK_DIR = ROOT / "docs/runbooks"

# profile 的 automation 底下唯一「可選」的 env:留空時 production_backup.py 會
# 退回 profile 的 default_keep。其他每一個 *_env 都走 _required_env,缺就爆。
OPTIONAL_PROFILE_ENV_KEYS = frozenset({"keep_env"})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _now() -> float:
    return time.time()


def required_backup_env_names() -> list[str]:
    """從 profile 與備份工具原始碼推導出必要環境變數清單(不寫死名單)。"""

    profile = json.loads(_read(PROFILE))
    automation = profile["automation"]
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    isinstance(value, str)
                    and key.endswith("_env")
                    and key not in OPTIONAL_PROFILE_ENV_KEYS
                ):
                    names.add(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(automation)
    # 工具原始碼裡直接寫死的名稱 (restore/verify 路徑) 也要算進來。
    names.update(
        re.findall(
            r'_required(?:_file)?_env\(\s*"([A-Z0-9_]+)"', _read(BACKUP_TOOL)
        )
    )
    return sorted(names)


def env_example_assigned_keys() -> set[str]:
    """`.env.example` 中「未被註解」的賦值鍵。"""

    keys: set[str] = set()
    for line in _read(ENV_EXAMPLE).splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match:
            keys.add(match.group(1))
    return keys


class BackupEnvContractTests(unittest.TestCase):
    def test_every_required_env_is_present_in_env_example(self) -> None:
        required = required_backup_env_names()
        # 稽核當時的實際數量是 18;數量變動代表 profile 契約改了,要一起改文件。
        self.assertEqual(len(required), 18, required)
        assigned = env_example_assigned_keys()
        missing = [name for name in required if name not in assigned]
        self.assertEqual(
            missing,
            [],
            "以下 production_backup.py 必要環境變數不在 .env.example,"
            f"照 runbook 部署完的 cron 會靜默失敗: {missing}",
        )

    def test_every_required_env_is_documented_in_backup_runbook(self) -> None:
        runbook = _read(BACKUP_RUNBOOK)
        missing = [name for name in required_backup_env_names() if name not in runbook]
        self.assertEqual(missing, [], missing)

    def test_env_example_does_not_leave_retention_knobs_empty_traps(self) -> None:
        # ANILA_BACKUP_KEEP / RESTART_TIMEOUT 走 os.environ.get(name, default);
        # 若被設成空字串會直接 int("") 爆掉。anila-ops.sh 的 loader 把空值當未設,
        # 這條測試釘住那個行為,不然 .env.example 的空白範本本身就是地雷。
        ops = _read(OPS)
        self.assertIn(
            '[ -n "$value" ] || continue', ops, "loader 必須把空值視為未設"
        )


class BackupCommandSurfaceTests(unittest.TestCase):
    def test_runbooks_do_not_teach_a_nonexistent_full_flag(self) -> None:
        offenders = []
        for path in sorted(RUNBOOK_DIR.rglob("*.md")):
            if "backup --full" in _read(path):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            f"anila-ops.sh backup 不吃參數,runbook 不得再教 --full: {offenders}",
        )

    def test_backup_rejects_arguments_and_names_the_dead_full_flag(self) -> None:
        ops = _read(OPS)
        self.assertIn('if [ "$#" -ne 0 ]; then', ops)
        self.assertIn("--full|-full|full)", ops)
        self.assertIn('fatal "usage: anila-ops.sh backup"', ops)

    def test_guide_teaches_the_two_argument_restore_form(self) -> None:
        guide = _read(GUIDE)
        self.assertIn(
            "`anila-ops.sh restore <bundle 目錄> <全新可拋棄目標> prepare`", guide
        )
        # 舊的單參數形式(以及它附帶的「輸入 RESTORE」腳本)已不存在。
        self.assertNotIn("restore backups/<stamp>", guide)
        self.assertNotIn("需輸入 `RESTORE`", guide)

    def test_guide_no_longer_claims_the_bundle_contains_env_or_private_keys(self) -> None:
        guide = _read(GUIDE)
        self.assertNotIn("備份含 `.env` 與 JWT 私鑰", guide)
        self.assertIn("備份不含 `.env`,也不含 JWT / TLS 私鑰", guide)
        self.assertIn("key_management_reference", guide)

    def test_guide_puts_the_backup_directory_outside_the_repo(self) -> None:
        guide = _read(GUIDE)
        self.assertNotIn("備份落在 `<repo>/backups/`", guide)
        self.assertIn("**備份落在 repo 外**", guide)
        self.assertIn("$ANILA_STATE_DIR/backups", guide)

    def test_restore_drill_is_not_scheduled_by_cron(self) -> None:
        # prepare/smoke 拒絕既存 target,而 drill 目錄含機敏明文,不能自動產生。
        self.assertIn("restore 演練不排 cron", _read(GUIDE))


class BackupFailureVisibilityTests(unittest.TestCase):
    def test_backup_writes_a_heartbeat_on_success_and_failure(self) -> None:
        ops = _read(OPS)
        self.assertIn("write_backup_heartbeat success 0", ops)
        self.assertIn('write_backup_heartbeat failure "$status"', ops)
        self.assertIn("anila.backup-heartbeat.v1", ops)
        # exec 會讓 heartbeat 永遠寫不到,所以 backup 不能再 exec。
        self.assertNotIn("exec python3 \"$PRODUCTION_BACKUP_TOOL\" \\\n    --repo-root", ops)

    def test_heartbeat_lives_outside_the_repo(self) -> None:
        ops = _read(OPS)
        self.assertIn(
            'assert_outside_repo "$BACKUP_HEARTBEAT" ANILA_BACKUP_HEARTBEAT_FILE', ops
        )

    def test_health_fails_when_the_heartbeat_is_older_than_25_hours(self) -> None:
        ops = _read(OPS)
        self.assertIn("BACKUP_HEARTBEAT_MAX_AGE_SECONDS=90000", ops)
        self.assertIn("health_backup_heartbeat", ops)
        self.assertIn(
            'if [ "$age_seconds" -gt "$BACKUP_HEARTBEAT_MAX_AGE_SECONDS" ]; then', ops
        )
        heartbeat_block = ops[ops.index("health_backup_heartbeat() {"):]
        heartbeat_block = heartbeat_block[: heartbeat_block.index("\ncmd_health")]
        # 三種靜默失敗都必須是 FAIL (health 以 exit 1 收尾,才接得上告警)。
        self.assertIn("chk fail \"找不到 backup heartbeat", heartbeat_block)
        self.assertIn("chk fail \"backup heartbeat 格式無法解析", heartbeat_block)
        self.assertIn("chk fail \"最近一次備份失敗", heartbeat_block)

    def test_health_and_backup_preflight_both_check_for_age(self) -> None:
        ops = _read(OPS)
        self.assertIn('for tool in python3 age openssl docker; do', ops)
        self.assertIn('command -v age >/dev/null 2>&1', ops)
        self.assertIn("backup_preflight", ops)

    def test_preflight_derives_the_required_env_list_from_the_profile(self) -> None:
        # 不接受在 shell 裡複製一份會漂移的名單。
        ops = _read(OPS)
        self.assertIn("backup_missing_required_env", ops)
        self.assertIn("production-backup-profile.v1.json", ops)

    def test_restore_preflight_only_requires_the_keys_restore_actually_uses(self) -> None:
        # 演練可能在沒掛 off-host 的可拋棄主機上做,不該被 backup 的 18 項擋住。
        ops = _read(OPS)
        block = ops[ops.index("restore_preflight() {"):]
        block = block[: block.index("\n}\n")]
        self.assertIn("ANILA_BACKUP_SIGNING_PUBLIC_KEY_FILE", block)
        self.assertIn("ANILA_BACKUP_AGE_IDENTITY_FILE", block)
        self.assertNotIn("ANILA_BACKUP_OFFHOST_DIR", block)
        self.assertNotIn("backup_missing_required_env", block)
        self.assertIn("restore_preflight || exit 90", ops)

    def test_backup_loads_required_env_from_file_without_eval(self) -> None:
        ops = _read(OPS)
        self.assertIn("load_backup_env_file", ops)
        self.assertIn('load_backup_env_file "$REPO_ROOT/.env"', ops)
        self.assertIn('load_backup_env_file "${ANILA_BACKUP_ENV_FILE:-}"', ops)
        # 嚴格解析,不 source / 不 eval:這些值只是路徑與指標,沒有理由被 root eval。
        loader = ops[ops.index("load_backup_env_file() {"):ops.index("load_backup_env() {")]
        for forbidden in ("eval", "source ", "\n. \""):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, loader)
        self.assertIn("ANILA_BACKUP_TEST_*)", loader)


@unittest.skipUnless(
    os.name == "posix"
    and shutil.which("bash") is not None
    and shutil.which("realpath") is not None,
    "需要 POSIX + bash + realpath 才能實際執行 anila-ops.sh",
)
class BackupCliBehaviorTests(unittest.TestCase):
    """真的執行腳本,別只 grep 字串。"""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="anila-state-") as state:
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("COMPOSE_", "ANILA_"))
            }
            env["ANILA_STATE_DIR"] = state
            return subprocess.run(
                ["bash", str(OPS), *args],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )

    def test_full_flag_fails_with_an_explanation_instead_of_bare_usage(self) -> None:
        result = self._run("backup", "--full")
        self.assertNotEqual(result.returncode, 0)
        combined = result.stdout + result.stderr
        self.assertIn("全量", combined)
        self.assertIn("--full", combined)

    def test_unknown_backup_argument_still_fails_closed(self) -> None:
        result = self._run("backup", "--incremental")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("usage: anila-ops.sh backup", result.stdout + result.stderr)

    def test_restore_without_target_fails_with_the_two_argument_usage(self) -> None:
        result = self._run("restore", "/nonexistent/bundle")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "usage: anila-ops.sh restore <signed-bundle-dir> "
            "<new-disposable-target> [prepare|smoke]",
            result.stdout + result.stderr,
        )

    def test_help_still_renders_the_documented_env_contract(self) -> None:
        result = self._run("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ANILA_BACKUP_ENV_FILE", result.stdout)
        self.assertIn("ANILA_BACKUP_HEARTBEAT_FILE", result.stdout)

    def test_preflight_names_every_missing_env_in_one_pass(self) -> None:
        """一次列完所有缺項,而不是像 production_backup.py 一次只報一個。"""

        required = required_backup_env_names()
        with tempfile.TemporaryDirectory(prefix="anila-preflight-") as work:
            stub_bin = Path(work) / "bin"
            stub_bin.mkdir()
            for tool in ("age", "openssl", "docker"):
                stub = stub_bin / tool
                stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                stub.chmod(0o755)
            empty_env = Path(work) / "backup.env"
            empty_env.write_text("# 全部留空\n", encoding="utf-8")
            state = Path(work) / "state"
            state.mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("COMPOSE_", "ANILA_"))
            }
            env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
            env["ANILA_STATE_DIR"] = str(state)
            env["ANILA_BACKUP_ENV_FILE"] = str(empty_env)
            result = subprocess.run(
                ["bash", str(OPS), "backup"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        for name in required:
            with self.subTest(name=name):
                self.assertIn(name, combined)
        # preflight 擋在前面,不該真的走到備份工具。
        self.assertNotIn("production backup ERROR", combined)


@unittest.skipUnless(
    os.name == "posix" and shutil.which("bash") is not None,
    "需要 POSIX + bash 才能實際執行 health 的 heartbeat 判讀",
)
class HeartbeatEvaluationTests(unittest.TestCase):
    """把 health 的 heartbeat 判讀段抽出來真的跑,別只 grep 字串。"""

    HARNESS_PREAMBLE = "\n".join(
        [
            "set -uo pipefail",
            'ok(){ printf "OK %s\\n" "$*"; }',
            'warn(){ printf "WARN %s\\n" "$*"; }',
            'err(){ printf "FAIL %s\\n" "$*"; }',
            "_FAILS=0",
            'chk(){ case "$1" in',
            '  ok) ok "$2" ;;',
            '  warn) warn "$2" ;;',
            '  fail) err "$2"; _FAILS=$((_FAILS + 1)) ;;',
            "esac; }",
            "BACKUP_HEARTBEAT_MAX_AGE_SECONDS=90000",
        ]
    )

    @classmethod
    def setUpClass(cls) -> None:
        ops = _read(OPS)
        start = ops.index("health_backup_heartbeat() {")
        end = ops.index("\ncmd_health", start)
        cls.function_source = ops[start:end]

    def _evaluate(self, heartbeat: str | None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory(prefix="anila-heartbeat-") as work:
            path = Path(work) / "backup-heartbeat.json"
            if heartbeat is not None:
                path.write_text(heartbeat, encoding="utf-8")
            harness = Path(work) / "harness.sh"
            harness.write_text(
                self.HARNESS_PREAMBLE
                + "\n"
                + self.function_source
                + "\nhealth_backup_heartbeat\nexit $_FAILS\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["bash", str(harness)],
                env={**os.environ, "BACKUP_HEARTBEAT": str(path)},
                capture_output=True,
                text=True,
                timeout=60,
            )
        # age 不一定裝在測試機上,把那一項的 FAIL 扣掉再看 heartbeat 的判定。
        output = result.stdout + result.stderr
        heartbeat_fails = sum(
            1
            for line in output.splitlines()
            if line.startswith("FAIL") and "找不到 age" not in line
        )
        return heartbeat_fails, output

    def test_missing_heartbeat_is_a_failure(self) -> None:
        fails, output = self._evaluate(None)
        self.assertEqual(fails, 1, output)
        self.assertIn("找不到 backup heartbeat", output)

    def test_fresh_success_passes(self) -> None:
        fails, output = self._evaluate(
            '{"schema_version":"anila.backup-heartbeat.v1","status":"success",'
            '"exit_code":0,"epoch":%d,"finished":"now"}\n' % int(_now())
        )
        self.assertEqual(fails, 0, output)
        self.assertIn("最近一次備份成功", output)

    def test_recent_failure_is_reported(self) -> None:
        fails, output = self._evaluate(
            '{"status":"failure","exit_code":90,"epoch":%d}\n' % int(_now())
        )
        self.assertEqual(fails, 1, output)
        self.assertIn("最近一次備份失敗", output)

    def test_heartbeat_older_than_25_hours_is_a_failure(self) -> None:
        fails, output = self._evaluate(
            '{"status":"success","epoch":%d}\n' % int(_now() - 26 * 3600)
        )
        self.assertEqual(fails, 1, output)
        self.assertIn("未更新", output)

    def test_heartbeat_at_24_hours_is_still_accepted(self) -> None:
        fails, output = self._evaluate(
            '{"status":"success","epoch":%d}\n' % int(_now() - 24 * 3600)
        )
        self.assertEqual(fails, 0, output)

    def test_unparsable_heartbeat_is_treated_as_no_backup(self) -> None:
        fails, output = self._evaluate("這不是 JSON\n")
        self.assertEqual(fails, 1, output)
        self.assertIn("格式無法解析", output)


class OfflineToolkitAgeTests(unittest.TestCase):
    def test_toolkit_downloads_age_with_a_pinned_checksum(self) -> None:
        toolkit = _read(TOOLKIT)
        self.assertIn("AGE_VERSION=", toolkit)
        self.assertRegex(toolkit, r'AGE_SHA256="\$\{AGE_SHA256:-[0-9a-f]{64}\}"')
        self.assertIn("github.com/FiloSottile/age/releases/download", toolkit)
        self.assertIn('"$got" = "$AGE_SHA256"', toolkit)

    def test_age_archive_lands_in_the_toolkit_manifest(self) -> None:
        toolkit = _read(TOOLKIT)
        self.assertIn('AGE_ARCHIVE="09-age-${AGE_VERSION}-linux-amd64.tar.gz"', toolkit)
        self.assertIn("sha256sum *.tar.gz > TOOLKIT-CHECKSUMS.sha256", toolkit)
        # 只寫進 checksum 檔不夠:manifest 少了 age 就要讓整包 fail,否則又是
        # 「以為帶了、其實沒帶」。
        self.assertIn(
            'grep -q " $AGE_ARCHIVE\\$" "$DEST/TOOLKIT-CHECKSUMS.sha256"', toolkit
        )
        self.assertIn("exit $(( FAILED_IMG || FAILED_AGE ))", toolkit)

    def test_backup_runbook_points_at_the_offline_age_artifact(self) -> None:
        runbook = _read(BACKUP_RUNBOOK)
        self.assertIn("09-age-", runbook)
        self.assertIn("age-keygen", runbook)


if __name__ == "__main__":  # 可當獨立檢查腳本使用
    unittest.main(verbosity=2)
