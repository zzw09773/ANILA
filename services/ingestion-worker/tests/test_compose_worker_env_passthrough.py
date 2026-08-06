"""compose 的 ingestion-worker 區塊有沒有把操作者旋鈕帶進容器。

``services/csp/tests/test_compose_csp_env_passthrough.py`` 釘的是 **csp** 那個
環境區塊。它是為兩次同形狀的缺陷寫的:``ANILA_ALLOW_GRPC_ENDPOINT`` 與
``EMBEDDING_TIMEOUT`` 功能都寫好了、``.env.example`` 也寫好了,但 compose 的
服務區塊沒有那一行 —— 而整棵樹**沒有任何 ``env_file:``**,``.env`` 只是 compose
的變數來源,不會整包灌進容器。兩次都沒有任何錯誤訊息:改了、重啟了、什麼也沒發生。

worker 有自己的環境區塊,而 csp 那個檔案的 fixture 只讀 ``services.csp``,
所以那邊的清單保護不到這裡。這個檔案是同一條不變式在 worker 側的一份。

刻意不用 ``docker compose config``:測試環境不保證有 docker daemon,而要釘的
不變式是**這份檔案的內容**,不是本機 docker 的狀態。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ingestion_worker.settings import WorkerSettings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PLATFORM_YML = _REPO_ROOT / "infra" / "compose" / "platform.yml"

# 每一個都有「文件或錯誤訊息叫操作者去設它」這個理由在後面。
# 加新的一行進來以前先問:少了它,操作者照做會不會什麼都沒發生?
_WORKER_OPERATOR_KNOBS = [
    # runbook §3.1c 排錯表的「縮小批次」就是縮這一個;`.env.example` 也列著。
    # 少了它,那句指示又變回一個沒有旋鈕的動作。
    "EMBEDDING_BATCH_SIZE",
    # 同一條不變式的另一項:runbook §3.1c 印著
    # 「EMBEDDING_BATCH_SIZE × 每段延遲 < min(這個, csp 的 EMBEDDING_TIMEOUT)」。
    # 2026-08-07 以前它到不了容器 —— 一條印出來的不變式,只有一項的旋鈕接得到
    # 容器,跟「有按鈕但後端收不到」是同一個形狀。
    "EMBEDDING_TIMEOUT_SECONDS",
]

#: compose 的 fallback 必須跟程式預設同值,否則「沒設這個變數時容器裡是什麼」
#: 跟讀原始碼推出來的不一樣。
_DEFAULT_MUST_MATCH_FIELD = {
    "EMBEDDING_BATCH_SIZE": "embedding_batch_size",
    "EMBEDDING_TIMEOUT_SECONDS": "embedding_timeout_seconds",
}

_DEFAULTED = re.compile(r"^\$\{(?P<key>[A-Z0-9_]+):-(?P<default>.*)\}$")


@pytest.fixture(scope="module")
def worker_environment() -> dict:
    doc = yaml.safe_load(_PLATFORM_YML.read_text(encoding="utf-8"))
    env = doc["services"]["ingestion-worker"]["environment"]
    assert isinstance(env, dict), "ingestion-worker 的 environment 改成 list 了,本檔要跟著改"
    return env


def test_there_is_still_no_env_file_shortcut():
    """平台這組 compose 檔沒有 ``env_file:`` —— 所以「列舉」是唯一的路。"""
    compose_files = sorted(
        list(_REPO_ROOT.glob("compose*.yaml"))
        + list(_REPO_ROOT.glob("infra/compose/*.yml"))
    )
    assert compose_files, "找不到平台的 compose 檔,路徑推導壞了"
    hits = [
        p.name for p in compose_files if "env_file" in p.read_text(encoding="utf-8")
    ]
    assert hits == [], f"出現 env_file:,本檔的前提要重新檢視 — {hits}"


@pytest.mark.parametrize("key", _WORKER_OPERATOR_KNOBS)
def test_operator_knob_reaches_the_worker_container(worker_environment, key):
    assert key in worker_environment, (
        f"{key} 沒有列在 platform.yml 的 ingestion-worker environment —— "
        f".env 設了也不會進到容器,而文件叫操作者去設它"
    )


@pytest.mark.parametrize("key", _WORKER_OPERATOR_KNOBS)
def test_the_knob_is_fed_from_the_same_named_variable(worker_environment, key):
    """鍵在、值卻插值到別的變數,症狀跟根本沒這一行一樣難查。"""
    value = str(worker_environment[key])
    assert f"${{{key}" in value, (
        f"{key} 的值是 {value!r} —— 沒有從同名的 .env 變數插值進來"
    )


@pytest.mark.parametrize("key", sorted(_DEFAULT_MUST_MATCH_FIELD))
def test_the_compose_default_matches_the_application_default(worker_environment, key):
    """`.env` 缺這個鍵時,容器拿到的值由 compose 這一行的 fallback 決定。

    比對方式刻意是「把 compose 的 fallback 字串餵給 pydantic」,不是比字串:
    容器拿到的本來就是字串,而 `settings.py` 的 module-level
    ``settings = WorkerSettings()`` 會在 import 時就驗它。所以這條同時擋掉
    兩件事 —— 值漂開(`:-8` vs 32),以及值根本解析不了(`:-32.0` 對 int
    欄位是 ValidationError,容器會在啟動時就死)。
    """
    value = str(worker_environment[key])
    match = _DEFAULTED.match(value)
    assert match, (
        f"ingestion-worker.{key} 的值是 {value!r} —— 不是 `${{{key}:-<預設>}}` "
        f"這個形狀,缺鍵時容器會拿到什麼變得要另外推敲"
    )
    assert match.group("key") == key, f"{key} 插值到 {match.group('key')} —— 貼錯變數了"
    field = _DEFAULT_MUST_MATCH_FIELD[key]
    app_default = WorkerSettings.model_fields[field].default
    try:
        parsed = getattr(
            WorkerSettings(_env_file=None, **{field: match.group("default")}), field
        )
    except ValidationError as exc:
        raise AssertionError(
            f"compose 的 {key} 預設是 {match.group('default')!r},"
            f"WorkerSettings.{field} 收不下這個字串 —— `.env` 沒有這個鍵的"
            f"全新部署會在 import settings 時就炸掉:{exc}"
        ) from exc
    assert parsed == app_default, (
        f"compose 的 {key} 預設解析成 {parsed!r},"
        f"WorkerSettings.{field} 是 {app_default!r} —— 兩邊各說各話"
    )
