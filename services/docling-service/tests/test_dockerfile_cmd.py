"""守衛測試:出貨啟動字串(Dockerfile CMD 的 `uvicorn app.main:app`)要載得起來。

驗證入口必須是使用者的入口(CLAUDE.md 🚪 2026-08-17):出貨用「字串路徑」
啟動,不必、也不該只驗「建構物件」的 create_app() 叫得起來。兩者在測試碼
裡長得幾乎一樣,差別在——這條測試**從 Dockerfile 讀出**那個字串再解析,所以:

- 把 CMD 改成不存在的模組路徑(如 `app.nope:app`)→ importlib 載入失敗 → 紅
- 把 main.py 的 `app = create_app()` 刪掉 → import 成功但取不到 attr → 紅

手寫一個等價字串守的是「某個字串載得進去」,不是「出貨用的那個載得進去」;
這條測試守的是後者。
"""
from __future__ import annotations

import importlib
from pathlib import Path

from fastapi import FastAPI

DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


def _asgi_app_ref(dockerfile_text: str) -> str:
    """從 Dockerfile 的 uvicorn CMD 抽出 `module:attr` 字串。抽不到 → 紅。"""
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("CMD") or "uvicorn" not in stripped:
            continue
        for token in (
            stripped.replace('"', " ")
            .replace("'", " ")
            .replace(",", " ")
            .replace("[", " ")
            .replace("]", " ")
            .split()
        ):
            if ":" in token and "." in token:
                return token
    raise AssertionError("Dockerfile 沒有可解析的 uvicorn CMD(module:attr)字串")


def test_dockerfile_cmd_loads_shipped_app() -> None:
    module_path, attr_name = _asgi_app_ref(
        DOCKERFILE.read_text(encoding="utf-8")
    ).rsplit(":", 1)

    # CMD 的 module 路徑改成不存在的值,這行就炸。
    module = importlib.import_module(module_path)
    # main.py 少了模組層級 `app = create_app()`,這行就炸。
    app = getattr(module, attr_name)

    assert isinstance(app, FastAPI)
