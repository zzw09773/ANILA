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

2026-09-26 擁有者決定不部署本機生圖模型。``FLUX_BACKEND_URL`` 在
anila-studio 也不再是開關：簡報配圖改看治理中心的 ``image_generation``
角色。studio 區塊與 ``.env.example`` 都不該再留這顆。

``MODEL`` 是半棵樹的子字串,只比對 environment 的**鍵**,不比對值。
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


def test_studio_no_longer_ships_the_local_flux_knob():
    """本機 FLUX 已移除。studio 不再讀 FLUX_BACKEND_URL。"""
    studio_env = _environment_of("anila-studio")
    for key in ("FLUX_BACKEND_URL", "FLUX_CACHE_DIR", "FLUX_AGENT_BASE_URL"):
        assert key not in studio_env, key


def test_env_example_no_longer_offers_flux_toggles():
    """``.env.example`` 不再把本機生圖位址留給操作者填。"""
    lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    for key in ("FLUX_BACKEND_URL", "FLUX_AGENT_BASE_URL", "FLUX_CACHE_DIR"):
        assert not any(re.match(rf"^{key}\s*=", line) for line in lines), key
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "csp / anila-studio 的 get_flux_provider" not in text


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
