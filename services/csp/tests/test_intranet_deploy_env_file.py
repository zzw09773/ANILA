"""``intranet-deploy.sh`` 認的「這個 KEY 已經設了」必須等於 compose 認的。

腳本用 `^KEY=` 認鍵時,操作者手寫成 ` ANILA_ALLOW_GRPC_ENDPOINT=1`(行首一個
空格)那一行是**看不見**的:重跑部署腳本會在檔尾再 append 一行 `...=0`,而
docker compose 取最後一筆 —— 剛照 runbook §3.1c 開起來的旗標就這樣被靜默關掉,
症狀只是註冊 400,現場幾乎不可能反推到「是部署腳本改的」。``preserve_flag``
存在的理由正是要擋這件事,所以它的「已設」判準錯了,等於它不存在。

實測 docker compose v2.36.2(2026-08-03,本機):下面每一種寫法 compose 都讀成
同一個值,`^KEY=` 一種都認不出來 ——

    ` KEY=1`  `\tKEY=1`  `export KEY=1`  `KEY =1`  `KEY="1"`  `KEY='1'`
    `KEY=1 `  `KEY=1\r\n`(CRLF)  `KEY=1 # 註解`

2026-08-05 補測「引號 × 行尾註解」的**組合**,compose 一樣讀成 1 ——

    `KEY="1" # 註解`  `KEY='1' # 註解`  `export KEY="1" # 註解`
    `KEY="1" # 註解` + CRLF

而腳本當時一個都不警示(``get_env`` 要求整串頭尾都是引號),所以「腳本眼中的
已設 == compose 眼中的已設」在這裡是破的。同一天把整組寫法對 `docker compose
config` 做了差分,13 種寫法兩邊逐字相同。

重複鍵取最後一行(`FOO=first` / `FOO=second` → second)。

這裡不叫 docker:要釘的不變式是**腳本的行為**,而測試環境不保證有 daemon。
compose 的那一半是上面那段實測,行為的那一半在這裡:把腳本裡真正那段
函式抓出來執行,對 `.env` 的結果斷言。抓不到就會炸(sourced 文字裡沒有
``preserve_flag`` → bash 直接失敗),不會靜靜地放水。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "deployment"
    / "intranet"
    / "intranet-deploy.sh"
)
_KEY = "ANILA_ALLOW_GRPC_ENDPOINT"


def _env_helpers() -> str:
    """腳本裡那段 .env 存取函式(``_trim`` 起,到主流程第一行止)。"""
    text = _SCRIPT.read_text(encoding="utf-8")
    start = text.index("_trim() {")
    end = text.index('\necho "===', start)
    return text[start:end]


def _run(workdir: Path, runs: int = 2) -> subprocess.CompletedProcess:
    """在 workdir 上跑 preserve_flag `runs` 次(重跑部署腳本的樣子)。"""
    body = "\n".join(
        [
            # 真腳本第 23 行就是 `set -euo pipefail`。少一個 `-e`,「只在 errexit
            # 底下才會現形」的行為(例如 preserve_flag 裡那個判偽的 `[ … ] &&`)
            # 在這個 harness 裡就永遠看不見 —— 旗標要跟被測的腳本同一套。
            "set -euo pipefail",
            'c() { printf "%s" "$2"; }',
            'warn() { echo "WARN:$*"; }',
            'die() { echo "DIE:$*" >&2; exit 1; }',
            'cd "$1" || exit 1',
            _env_helpers(),
            *[f'preserve_flag {_KEY} "note"' for _ in range(runs)],
        ]
    )
    return subprocess.run(
        ["bash", "-c", body, "bash", str(workdir)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _read_raw(env_path: Path) -> str:
    """不做換行轉換的讀檔 —— CRLF 的 ``\\r`` 要留著,否則驗不出「原封不動」。"""
    with env_path.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _key_lines(env_path: Path) -> list[str]:
    return [line for line in _read_raw(env_path).split("\n") if _KEY in line]


# compose 讀得到「1」的各種寫法。每一種都是操作者真的會手寫出來的東西。
_ENABLED_WRITINGS = [
    pytest.param(f" {_KEY}=1\n", id="leading-space"),
    pytest.param(f"\t{_KEY}=1\n", id="leading-tab"),
    pytest.param(f"export {_KEY}=1\n", id="export-prefix"),
    pytest.param(f"{_KEY} =1\n", id="space-before-equals"),
    pytest.param(f'{_KEY}="1"\n', id="double-quoted"),
    pytest.param(f"{_KEY}='1'\n", id="single-quoted"),
    pytest.param(f"{_KEY}=1 \n", id="trailing-space"),
    pytest.param(f"{_KEY}=1\r\n", id="crlf"),
    pytest.param(f"{_KEY}=1 # 為了 Triton\n", id="inline-comment"),
    # ↓ 引號 × 行尾註解的**組合**。上面兩種各自單獨都已經對了,合起來卻漏:
    #   `get_env` 舊版要求整串頭尾都是引號,`KEY="1" # 註解` 不符合 → 掉進
    #   「只砍註解」那條 → 剩 `"1"` → 與 "1" 不等 → compose 讀成 1 而腳本不警示。
    #   操作者照 runbook 開了旗標又寫了理由在後面,是很自然的寫法。
    pytest.param(f'{_KEY}="1" # 為了 Triton\n', id="double-quoted-comment"),
    pytest.param(f"{_KEY}='1' # 為了 Triton\n", id="single-quoted-comment"),
    pytest.param(f'export {_KEY}="1" # 為了 Triton\n', id="export-quoted-comment"),
    pytest.param(f'{_KEY}="1" # 為了 Triton\r\n', id="crlf-quoted-comment"),
]


@pytest.mark.parametrize("written", _ENABLED_WRITINGS)
def test_an_operator_set_flag_survives_two_script_runs(tmp_path, written):
    """重跑兩次之後,操作者那一行原封不動,而且沒有第二行同名鍵。

    第二行同名鍵就是這個缺陷本身:compose 取最後一筆,append 的 `=0` 會贏。
    """
    env = tmp_path / ".env"
    env.write_text(written, encoding="utf-8", newline="")

    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr

    lines = _key_lines(env)
    assert len(lines) == 1, (
        f"腳本在檔尾又寫了一行 {_KEY} —— compose 取最後一筆,"
        f"操作者開起來的旗標會被它蓋掉:{lines}"
    )
    assert lines[0] == written.rstrip("\n"), "操作者原本那一行被改寫了"


@pytest.mark.parametrize("written", _ENABLED_WRITINGS)
def test_an_enabled_flag_is_warned_about(tmp_path, written):
    """compose 會讀成 1 的寫法,都要印 warn。

    ``KEY="1"`` 這種以前不會警示 —— 放寬確實生效了,操作者卻不會在部署輸出裡
    看到任何一行提醒。
    """
    env = tmp_path / ".env"
    env.write_text(written, encoding="utf-8", newline="")

    proc = _run(tmp_path, runs=1)
    assert f"WARN:{_KEY}=1" in proc.stdout, proc.stdout


def test_a_fresh_deployment_still_gets_a_zero(tmp_path):
    """缺鍵才補 0 —— 全新部署的預設姿態不能因為這次修改而變寬。"""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")

    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _key_lines(env) == [f"{_KEY}=0"]
    assert "WARN:" not in proc.stdout


def test_an_explicitly_disabled_flag_stays_disabled_and_silent(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{_KEY}=0\n", encoding="utf-8")

    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _key_lines(env) == [f"{_KEY}=0"]
    assert "WARN:" not in proc.stdout


def test_the_last_occurrence_wins_like_compose(tmp_path):
    """重複鍵時腳本要跟 compose 看到同一個值(最後一筆)。

    讀第一筆的版本會讓腳本以為旗標是關的、compose 卻是開的 —— 部署輸出少了
    那行 warn,而放寬其實生效中。
    """
    env = tmp_path / ".env"
    env.write_text(f"{_KEY}=0\n {_KEY}=1\n", encoding="utf-8")

    proc = _run(tmp_path, runs=1)
    assert f"WARN:{_KEY}=1" in proc.stdout, proc.stdout
