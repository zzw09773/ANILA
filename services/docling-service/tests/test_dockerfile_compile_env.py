"""守衛:docling 的 torch.compile 必須維持關閉——從 Dockerfile 讀出那行再斷言。

2026-08-19 稽核 v15:拿掉 build-essential + python3-dev 之後,**整張映像壓在
`DOCLING_INFERENCE_COMPILE_TORCH_MODELS=false` 這一行上**,而全 repo 只有
Dockerfile 一處提到它。翻成 true → `/health` 回 ready 而每一次 `/parse` 都
503(InvalidCxxCompiler,因為CPU上要編譯器) = 假 ready。

與 test_dockerfile_cmd.py 同形:守衛要**讀出貨用的那個 ENV**,不是手寫等價字串。
手寫的守的是「某個字串是 false」;這個守的是「出貨 Dockerfile 那行是 false」。
"""
from __future__ import annotations

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

# 這顆 env 名必須在 Dockerfile 的 ENV 區塊,且值為 false(字面,不分大小寫)。
COMPILE_ENV_NAME = "DOCLING_INFERENCE_COMPILE_TORCH_MODELS"


def _compile_env_value(dockerfile_text: str) -> str | None:
    """從 Dockerfile 原文抽出 `DOCLING_INFERENCE_COMPILE_TORCH_MODELS=<值>` 的
    值(單 token,不含內嵌空白的值)。抽不到 → None。

    用 regex 而不是手工 parse 整個 ENV 區塊:這顆值就一個 `false`、沒有 backslash
    延續,手工 parser 反而在「ENV 區塊內夾註解行」時失準——守的是值,不是 parser。
    """
    m = re.search(
        rf"^\s*{COMPILE_ENV_NAME}\s*=\s*(\S+)",
        dockerfile_text,
        flags=re.MULTILINE,
    )
    return m.group(1) if m else None


def test_dockerfile_bakes_compile_off() -> None:
    """出貨 Dockerfile 的 COMPILE_TORCH_MODELS 必須是 false。

    綠→紅→綠:
      - 把該行 ENV 改成 true → 這條紅(值是 true)。
      - 把整行刪掉 → 這條紅(抽不到,不是靜默)。
    """
    value = _compile_env_value(DOCKERFILE.read_text(encoding="utf-8"))
    assert value is not None, (
        f"Dockerfile 沒有 {COMPILE_ENV_NAME}=… 的 ENV——torch.compile 會回到 docling"
        " 預設 True,CPU 上第一次 parse 就 InvalidCxxCompiler(假 ready)。"
    )
    assert value.strip().lower() == "false", (
        f"Dockerfile 的 {COMPILE_ENV_NAME} 是 {value!r},必須是 false:"
        " 翻成 true = /health 假 ready + 全 parse 503(映像已不帶編譯器)。"
    )
