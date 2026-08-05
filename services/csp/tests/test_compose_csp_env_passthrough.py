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

import re
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


# ── 放寬旗標的「預設值」 ─────────────────────────────────────────────────────
#
# 上面那幾條只問「鍵在不在、插值有沒有貼錯」。少的那一半是**預設值**:
# `${ANILA_ALLOW_GRPC_ENDPOINT:-0}` 改成 `:-1` 是一個字元,而 `.env` 沒有這個
# 鍵時 compose 就會把 1 送進容器 —— 全新部署的預設姿態直接變寬。
# 這一半原本零覆蓋。2026-08-05 實測:把 `ANILA_ALLOW_GRPC_ENDPOINT` 改成 `:-1`
# 跑全套,紅的**只有**下面這條新測試 —— 換句話說,在它之前這個改動是全綠的。
#
# `intranet-deploy.sh` 的 `preserve_flag` 釘的是「腳本不會推翻現場的決定」,
# 那是另一半;`.env` 缺鍵時真正生效的是 compose 這一行的 fallback,而部署
# 腳本只在缺鍵時補一行 `KEY=0`,它保護不到「有人把 compose 的預設改掉」。
#
# 用前綴掃描而不是寫死清單:以後新增 `ANILA_ALLOW_*` 旗標會自動被納入,
# 不必記得回來加一行。下面另有一條測試釘住「掃到的東西不能無聲變少」。
_RELAXATION_PREFIX = "ANILA_ALLOW_"

# 掃描應該至少涵蓋這些。任何一個從 compose 消失(改名、刪掉、搬走),上面那條
# 前綴掃描會安靜地變成掃 0 個然後照樣綠 —— 這條就是那個空集合的煞車。
_KNOWN_RELAXATION_FLAGS = {
    "ANILA_ALLOW_DEV_SECRET",
    "ANILA_ALLOW_HTTP_ENDPOINT",
    "ANILA_ALLOW_PRIVATE_ENDPOINT",
    "ANILA_ALLOW_HTTP_AGENT_ENDPOINT",
    "ANILA_ALLOW_GRPC_ENDPOINT",
}

# `"${KEY:-0}"` —— 必須是 `:-`(unset **或空字串**都取 fallback)。
# `${KEY-0}` 只在 unset 時才 fallback,`.env` 裡寫 `KEY=` 就會送空字串進去;
# 這裡一併擋掉那種寫法,不是為了 fallback 值,是為了「值從哪裡來」講得死。
_DEFAULTED = re.compile(r"^\$\{(?P<key>[A-Z0-9_]+):-(?P<default>.*)\}$")


def _relaxation_flags_in_platform_yml() -> list[tuple[str, str, str]]:
    """(service, key, value) —— platform.yml 裡所有服務的放寬旗標。

    不只 csp:ingestion-worker 也自己讀同一批旗標(它同樣會出向連模型),
    那邊的預設值變寬一樣沒有任何錯誤訊息。
    """
    doc = yaml.safe_load(_PLATFORM_YML.read_text(encoding="utf-8"))
    found = []
    for service, spec in (doc.get("services") or {}).items():
        env = (spec or {}).get("environment") or {}
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if key.startswith(_RELAXATION_PREFIX):
                found.append((service, key, str(value)))
    return found


def test_the_relaxation_flag_scan_still_finds_the_known_flags():
    """前綴掃描不能無聲地掃到空集合 —— 否則下面那條就永遠是真的。"""
    found = {key for _, key, _ in _relaxation_flags_in_platform_yml()}
    missing = _KNOWN_RELAXATION_FLAGS - found
    assert not missing, (
        f"platform.yml 裡找不到這些放寬旗標:{sorted(missing)} —— "
        f"要嘛它們被刪了(那 csp 的對應功能就關不掉/開不了),"
        f"要嘛改名了(那預設值的覆蓋就跟著失效,要更新本檔)"
    )


@pytest.mark.parametrize(
    "service,key,value",
    [
        pytest.param(s, k, v, id=f"{s}:{k}")
        for s, k, v in _relaxation_flags_in_platform_yml()
    ],
)
def test_a_relaxation_flag_defaults_to_off(service, key, value):
    """每一個放寬旗標,`.env` 缺鍵時 compose 都要渲染成 0。

    這是「全新部署的預設姿態」本身。實測 compose v2.36.2(2026-08-05):
    `.env` 沒有這個鍵時 `${K:-0}` 渲染成 `0`、`${K:-1}` 渲染成 `1`,
    容器拿到什麼完全由這一行決定。
    """
    match = _DEFAULTED.match(value)
    assert match, (
        f"{service}.{key} 的值是 {value!r} —— 不是 `${{{key}:-0}}` 這個形狀,"
        f"缺鍵時容器會拿到什麼變得要另外推敲"
    )
    assert match.group("key") == key, (
        f"{service}.{key} 插值到 {match.group('key')} —— 貼錯變數了"
    )
    assert match.group("default") == "0", (
        f"{service}.{key} 的預設值是 {match.group('default')!r},不是 '0' —— "
        f"`.env` 沒有這個鍵的全新部署會直接帶著放寬的姿態起來,沒有任何提示"
    )
