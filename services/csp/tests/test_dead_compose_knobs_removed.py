# -*- coding: utf-8 -*-
"""compose 裡「設了也不會有事發生」的旋鈕必須不存在。

這是 ``config.py:10-12`` 移掉 ``ENABLE_API_DOCS`` 時寫下的同一條規則
（原話:*A former ENABLE_API_DOCS flag was never read — removed so operators
cannot believe they toggled docs off.*）延伸到 compose 面:

- ``csp`` 區塊的 ``FLUX_BACKEND_URL`` / ``FLUX_MAX_CONCURRENT`` /
  ``FLUX_TIMEOUT_SECONDS``:註解寫著「empty BACKEND_URL would make
  get_flux_provider() return None (toggle OFF)」,但 ``get_flux_provider()``
  住在 ``services/anila-studio``,而 ``infra/docker/csp.Dockerfile`` 根本沒有
  把 anila-studio COPY 進 csp 映像。操作者在 ``.env`` 把它設成空字串期待
  「關掉繪圖」,對 csp **完全沒有作用**。
- ``router`` 區塊的 ``MODEL``:``services/anila-core-router/README.md:107``
  自己寫著「``main.py`` **不讀 ``MODEL``**……純屬殘留 env,不影響行為」。

⚠ **同名不等於同一顆。** ``FLUX_BACKEND_URL`` 在 ``anila-studio`` 區塊
（``platform.yml`` 的 studio block）是**活的**控制項:``studio_render.py:84,164``
真的讀它,``.env.example`` 那一行與 runbook §「FLUX_BACKEND_URL= 空 = 停用」
講的就是它。所以本檔的掃描一律**綁服務區塊**,不是整檔字串搜尋 ——
整檔搜尋會把活的那顆一起判死。

同理 ``MODEL`` 是半棵樹的子字串,只比對 environment 的**鍵**,不比對值。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLATFORM_YML = _REPO_ROOT / "infra" / "compose" / "platform.yml"
_ENV_EXAMPLE = _REPO_ROOT / ".env.example"

# csp 容器收得到、但 services/csp 全樹沒有任何讀取點的三顆。
_DEAD_IN_CSP = ("FLUX_BACKEND_URL", "FLUX_MAX_CONCURRENT", "FLUX_TIMEOUT_SECONDS")

# router 容器收得到、但 services/anila-core-router 沒有任何讀取點的一顆。
_DEAD_IN_ROUTER = ("MODEL",)


def _environment_of(service: str) -> dict:
    doc = yaml.safe_load(_PLATFORM_YML.read_text(encoding="utf-8"))
    spec = (doc.get("services") or {}).get(service)
    assert spec is not None, f"platform.yml 沒有 {service} 服務,本檔的路徑推導壞了"
    env = spec.get("environment") or {}
    assert isinstance(env, dict), f"{service} 的 environment 改成 list 了,本檔要跟著改"
    return env


# ── 死旋鈕:不可以再出現在那個服務的 environment ────────────────────────────


@pytest.mark.parametrize("key", _DEAD_IN_CSP)
def test_csp_no_longer_ships_the_dead_flux_knob(key):
    assert key not in _environment_of("csp"), (
        f"{key} 又回到 platform.yml 的 csp environment —— "
        f"services/csp 沒有任何讀取點,加回去等於再造一個假控制項"
    )


@pytest.mark.parametrize("key", _DEAD_IN_ROUTER)
def test_router_no_longer_ships_the_vestigial_model_knob(key):
    assert key not in _environment_of("router"), (
        f"{key} 又回到 platform.yml 的 router environment —— "
        f"main.py 不讀它(README.md:107),主路由模型由 CSP "
        f"/api/models/router-primary runtime 決定"
    )


# ── 反向釘:掃描不可以把「活的同名旋鈕」一起殺掉 ────────────────────────────


def test_the_live_studio_knob_is_untouched():
    """``FLUX_BACKEND_URL`` 對 anila-studio 是真的開關,必須還在。

    這一條是上面那三條的煞車:哪天有人用整檔字串搜尋去「清乾淨」,
    studio 的繪圖開關會跟著消失,而症狀是「內網把它設成空字串卻關不掉」
    ——或更糟,關得掉但沒有人知道那一行去哪了。
    """
    studio_env = _environment_of("anila-studio")
    assert "FLUX_BACKEND_URL" in studio_env, (
        "anila-studio 的 FLUX_BACKEND_URL 不見了 —— studio_render.py:84,164 "
        "真的讀它,.env.example 與 runbook 都叫操作者用它停用繪圖"
    )
    assert "${FLUX_BACKEND_URL" in str(studio_env["FLUX_BACKEND_URL"]), (
        "anila-studio 的 FLUX_BACKEND_URL 不再從同名 .env 變數插值"
    )


def test_env_example_still_offers_the_studio_toggle():
    """``.env.example`` 的 ``FLUX_BACKEND_URL=`` 是 studio 的鑰匙,不是 csp 的。

    刪掉它會讓內網「繪圖預設停用」那個姿態沒有地方可以宣告。
    """
    lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert any(re.match(r"^FLUX_BACKEND_URL\s*=", line) for line in lines), (
        ".env.example 少了 FLUX_BACKEND_URL= —— 那是 anila-studio 的停用開關"
    )


def test_env_example_no_longer_tells_operators_that_csp_reads_it():
    """``.env.example`` 不可以再說 csp 的 ``get_flux_provider()`` 會看這顆。

    那句話正是這次清理要消滅的形狀:文件把一個不存在的效果講得像真的。

    ⚠ 這裡刻意**不**用「含 get_flux_provider 的行不准出現 csp」那種掃法 ——
    寫得誠實的更正句(「csp 全樹沒有讀取點」)必然要提到 csp,那種掃法會對著
    正確的說明轉紅。改成一正一反兩條具體斷言:舊的那句錯話要不在,
    「只有 anila-studio 讀」要在。
    """
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "csp / anila-studio 的 get_flux_provider" not in text, (
        ".env.example 仍宣稱 csp 會呼叫 get_flux_provider() —— "
        "anila-studio 才有那支函式,csp 映像裡沒有它"
    )
    assert "只有 anila-studio 讀" in text, (
        ".env.example 少了「這顆只有 anila-studio 讀」那句更正 —— "
        "少了它,下一個人會照著舊理解再把 csp 那一行加回去"
    )


# ── 前提重驗:這四顆真的沒有讀取點（掃原始碼,不掃測試） ──────────────────


def _source_files(root: Path) -> list[Path]:
    return [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.suffix in {".py", ".js", ".mjs", ".ts", ".vue", ".sh"}
        and "__pycache__" not in path.parts
        and ".venv" not in path.parts
        and "node_modules" not in path.parts
    ]


@pytest.mark.parametrize("key", _DEAD_IN_CSP)
def test_csp_source_still_has_no_read_point(key):
    """移除的前提是「零讀取點」。哪天有人真的接上了,這條會先紅。

    紅了不代表要改測試 —— 代表要把那一行 compose 加回去。
    """
    roots = [_REPO_ROOT / "services" / "csp" / "app", _REPO_ROOT / "services" / "csp" / "migrations"]
    hits = [
        f"{path.relative_to(_REPO_ROOT)}:{lineno}"
        for root in roots
        for path in _source_files(root)
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if key in line
    ]
    assert hits == [], (
        f"{key} 現在在 csp 有讀取點了:{hits} —— "
        f"那 platform.yml 的 csp 區塊要把它加回去,而不是留著讀不到值的程式"
    )


def test_router_source_still_has_no_read_point():
    """``MODEL`` 是子字串地雷,所以只找「把它當 env 名字讀」的形狀。"""
    root = _REPO_ROOT / "services" / "anila-core-router"
    reader = re.compile(r"""(environ|getenv)[^\n]*["']MODEL["']""")
    hits = [
        f"{path.relative_to(_REPO_ROOT)}:{lineno}"
        for path in _source_files(root)
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if reader.search(line)
    ]
    assert hits == [], (
        f"router 現在讀 MODEL 了:{hits} —— platform.yml 的 router 區塊要把它加回去"
    )
