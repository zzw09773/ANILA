# -*- coding: utf-8 -*-
"""靜態守衛:csp-db 的 TZ 必須維持 UTC(W2-10 / 子計畫 C1 §c 的連帶鐵則)。

為什麼需要一支測試而不是只寫註解
--------------------------------
「顯示層一律 UTC+8」是已採納的決定(W1-4④ 的前端 formatter),而「順手把
``TZ: Asia/Taipei`` 加到 db 容器讓 log 也是台灣時間」看起來是同一個決定的自然
延伸 —— 那個建議確實被提出過,而且**是錯的**。治理帳的 ``created_at`` 帶
``server_default CURRENT_TIMESTAMP``,``CURRENT_TIMESTAMP`` 是 timestamptz,寫進
``timestamp without time zone`` 欄時按 session TZ 轉換 → session TZ 一改成台北,
那條路徑就開始存本地時間,而同一欄的舊值是 UTC。**同一個欄位混兩種語意,而且
資料裡沒有任何標記能區分某一列屬於哪一種** —— 事後無法修,因為無從得知該回推
哪些列。

一個註解阻止不了「看起來很自然」的改動,所以這裡用測試把它釘住。C1 全部批次
(naive timestamp 欄 → timestamptz)做完之後這支才可以刪 —— 屆時
``CURRENT_TIMESTAMP`` 寫進 timestamptz 欄存的是絕對時點,與 session TZ 無關。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_FILES = (
    REPO_ROOT / "infra" / "compose" / "platform.yml",
    REPO_ROOT / "infra" / "compose" / "dev.yml",
)
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _csp_db_block(compose_text: str) -> str:
    """取出 ``csp-db:`` 這個 service 的 YAML 區塊(到下一個同縮排 service 為止)。"""
    match = re.search(r"(?m)^  csp-db:\n", compose_text)
    assert match, "compose 裡找不到 csp-db service"
    start = match.end()
    following = re.search(r"(?m)^  [A-Za-z0-9_.-]+:\n", compose_text[start:])
    end = start + (following.start() if following else len(compose_text) - start)
    return compose_text[start:end]


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_csp_db_pins_tz_to_utc(compose_path: Path) -> None:
    block = _csp_db_block(compose_path.read_text(encoding="utf-8"))

    tz_lines = [
        line.strip()
        for line in block.splitlines()
        if re.match(r"\s*TZ\s*:", line)
    ]
    assert tz_lines, f"{compose_path.name} 的 csp-db 沒有明示 TZ —— W2-10 要求寫死 UTC"
    assert len(tz_lines) == 1, f"{compose_path.name} 的 csp-db 有多個 TZ 設定:{tz_lines}"

    value = tz_lines[0].split(":", 1)[1].strip().strip("'\"")
    assert value in {"Etc/UTC", "UTC"}, (
        f"{compose_path.name} 的 csp-db TZ = {value!r}。C1 全部批次做完前必須是 UTC:"
        "治理帳的 server_default CURRENT_TIMESTAMP 會按 session TZ 轉換,改成台北會讓"
        "同一欄混兩種語意且無標記可分(C1 §c)。"
    )

    # 不得可被 .env 覆寫 —— 那是最容易無聲破壞這條鐵則的路徑。
    assert "${" not in tz_lines[0], (
        f"{compose_path.name} 的 csp-db TZ 吃了 interpolation:{tz_lines[0]!r}。"
        "刻意要寫死,讓編 .env 不可能改到它。"
    )


@pytest.mark.parametrize("compose_path", COMPOSE_FILES, ids=lambda p: p.name)
def test_csp_db_tz_rule_carries_its_reason(compose_path: Path) -> None:
    """光有 ``TZ: Etc/UTC`` 不夠 —— 下一個人要看得到為什麼不能改。"""
    block = _csp_db_block(compose_path.read_text(encoding="utf-8"))
    assert "W2-10" in block, f"{compose_path.name} 的 csp-db 區塊沒指向 W2-10"
    assert "CURRENT_TIMESTAMP" in block, (
        f"{compose_path.name} 的 csp-db 區塊沒說明 server_default 那條寫入路徑 ——"
        "沒有它,讀者無法自行判斷這條鐵則什麼時候可以解除"
    )


def test_env_example_documents_the_rule() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "W2-10" in text and "csp-db 的時區必須維持 UTC" in text, (
        ".env.example 必須載明 csp-db TZ 鐵則(W2-10 追加要求)"
    )
    assert "CURRENT_TIMESTAMP" in text, ".env.example 的鐵則段要附成因,不只結論"
