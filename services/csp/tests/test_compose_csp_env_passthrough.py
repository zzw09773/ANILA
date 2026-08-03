"""compose 的 csp 區塊有沒有把「文件叫操作者去調的旋鈕」帶進容器。

這個檔案存在的理由,是同一個缺陷已經在這個包裡出現兩次:

- 第一次是 ``ANILA_ALLOW_GRPC_ENDPOINT``。功能寫好了、`.env.example` 寫好了,
  但 ``infra/compose/platform.yml`` 的 csp 區塊沒有那一行,而整棵樹**沒有
  任何 ``env_file:``** —— `.env` 只是 compose 的變數來源,不會整包灌進容器。
  結果是註冊 `grpc://` 端點一律 400,而 `.env` 裡明明寫著 1。
- 第二次是 ``EMBEDDING_TIMEOUT``。runbook §3.1c 的排錯表與 ``triton_grpc``
  的兩則錯誤訊息都叫操作者「調高 `EMBEDDING_TIMEOUT`」,那個變數同樣沒有進到
  csp 容器:改 `.env`、`up -d csp`,`printenv` 是空的,行為一模一樣。

兩次都**沒有任何錯誤訊息**。這正是本樹最貴的那條教訓的形狀:「讓使用者以為
發生了什麼、後端卻收不到那個意圖」的控制項。所以這裡直接讀 compose 檔本身,
把「文件/錯誤訊息點名的環境變數 → csp 環境區塊有對應直通」釘住。

刻意不用 ``docker compose config``:測試環境不保證有 docker daemon,而要釘的
不變式是**這份檔案的內容**,不是本機 docker 的狀態。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLATFORM_YML = _REPO_ROOT / "infra" / "compose" / "platform.yml"

# 每一個都有「文件或程式的錯誤訊息叫操作者去設它」這個理由在後面。
# 加新的一行進來以前先問:少了它,操作者照做會不會什麼都沒發生?
_OPERATOR_KNOBS = [
    # runbook §3.1c 排錯表 + client.py 兩則 DEADLINE_EXCEEDED 訊息。
    "EMBEDDING_TIMEOUT",
    # runbook §3.1c 第 1 步 / .env.example。
    "ANILA_ALLOW_GRPC_ENDPOINT",
    # runbook §3.1c 第 2 步。
    "ANILA_ALLOW_PRIVATE_ENDPOINT",
    # runbook §3.1b。
    "ANILA_ALLOW_HTTP_ENDPOINT",
    "ANILA_ALLOW_HTTP_AGENT_ENDPOINT",
    # SSRF allow-list,新增模型 service 要改的那條。
    "ANILA_TRUSTED_HOSTS",
    # anila-studio ReadTimeout 那次的旋鈕。
    "LLM_TIMEOUT",
]


@pytest.fixture(scope="module")
def csp_environment() -> dict:
    doc = yaml.safe_load(_PLATFORM_YML.read_text(encoding="utf-8"))
    env = doc["services"]["csp"]["environment"]
    assert isinstance(env, dict), "csp 的 environment 改成 list 了,本檔要跟著改"
    return env


def test_there_is_still_no_env_file_shortcut():
    """平台這組 compose 檔沒有 ``env_file:`` —— 所以「列舉」是唯一的路。

    哪天有人加了 ``env_file:``,上面那套推理(以及本檔其餘的測試)就要重寫;
    在那之前,沒被列舉的變數就是沒進容器。
    """
    compose_files = sorted(
        list(_REPO_ROOT.glob("compose*.yaml"))
        + list(_REPO_ROOT.glob("infra/compose/*.yml"))
    )
    assert compose_files, "找不到平台的 compose 檔,路徑推導壞了"
    hits = [
        p.name for p in compose_files if "env_file" in p.read_text(encoding="utf-8")
    ]
    assert hits == [], f"出現 env_file:,本檔的前提要重新檢視 — {hits}"


@pytest.mark.parametrize("key", _OPERATOR_KNOBS)
def test_operator_knob_reaches_the_csp_container(csp_environment, key):
    """文件叫人去設的變數,csp 區塊必須有對應那一行。

    少任何一行,對應的那句指示就是「改了、重啟了、什麼也沒發生」。
    """
    assert key in csp_environment, (
        f"{key} 沒有列在 platform.yml 的 csp environment —— "
        f".env 設了也不會進到容器,而文件叫操作者去設它"
    )


@pytest.mark.parametrize("key", _OPERATOR_KNOBS)
def test_the_knob_is_fed_from_the_same_named_variable(csp_environment, key):
    """``EMBEDDING_TIMEOUT: "${LLM_TIMEOUT:-30}"`` 這種貼錯也要擋。

    鍵在、值卻插值到別的變數,症狀跟根本沒這一行一樣難查。
    """
    value = str(csp_environment[key])
    assert f"${{{key}" in value, (
        f"{key} 的值是 {value!r} —— 沒有從同名的 .env 變數插值進來"
    )


def test_embedding_timeout_default_matches_the_application_default():
    """compose 的預設值與 ``Settings.EMBEDDING_TIMEOUT`` 不可各說各話。

    兩邊漂開的症狀是:沒設這個變數時,容器裡的值跟讀原始碼推出來的不一樣。
    """
    from app.config import settings

    doc = yaml.safe_load(_PLATFORM_YML.read_text(encoding="utf-8"))
    value = str(doc["services"]["csp"]["environment"]["EMBEDDING_TIMEOUT"])
    # "${EMBEDDING_TIMEOUT:-30}" → 30
    fallback = value.split(":-", 1)[1].rstrip("}")
    assert int(fallback) == settings.EMBEDDING_TIMEOUT
