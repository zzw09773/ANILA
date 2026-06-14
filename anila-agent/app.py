"""一鍵啟動 anila-agent 的 OpenAI 相容服務。

    python app.py
或
    uvicorn app:app --host 0.0.0.0 --port 8200

兩種都會先 load .env。env（或 .env）至少要有：
    ANILA_BASE_URL / ANILA_MODEL（模型端點）
    CSP_BASE_URL / CSP_SERVICE_TOKEN / ANILA_COLLECTION_ID（檢索 + 派工認證）
缺 token → fail-closed 401；collection ≤0 → 503。

起來後對外提供 /health、/v1/models、/v1/chat/completions（含 streaming）。
這個 host:port 就是註冊到 CSP 的 endpoint。
"""

from __future__ import annotations

from dotenv import load_dotenv

# 必須在 import service_wrapper 之前：它在 import 期就把 env 讀成模組常數
# （COLLECTION_ID / CSP_BASE_URL / CSP_SERVICE_TOKEN ...）。晚一步就讀到預設值。
# 既有 os.environ（docker -e）優先於 .env（load_dotenv 預設不覆寫）。
load_dotenv()

import os  # noqa: E402

from anila_agent.serving.service_wrapper import app  # noqa: E402

__all__ = ["app"]


def main() -> None:
    """以 uvicorn 啟動服務。host/port/log 由 env 控（ANILA_HOST / PORT / ANILA_LOG_LEVEL）。"""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("ANILA_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8200")),
        log_level=os.environ.get("ANILA_LOG_LEVEL", "INFO").lower(),
    )


if __name__ == "__main__":
    main()
