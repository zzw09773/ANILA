# 自訂 API endpoint

AgenticRAG 內建以下 endpoints：
- `POST /chat` — 經典 query loop
- `POST /agentic-chat` — framework runtime 路徑
- `POST /sessions/{id}/compact` — 手動觸發 memory compact
- `GET /sessions/{id}/away_summary` — 離線摘要
- `POST /documents/...` — 文件 ingestion
- `POST /search/...` — 直接 RAG 查詢

如果你要加自己的 endpoint（例：`/health`、`/metrics`、業務專屬 API），有幾種方式。

## 方式 A：拿到 app 實例後直接加（最簡單）

```python
from agentic_rag.api.server import create_app

app = create_app(provider=..., tool_registry=..., ...)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/my-business-flow")
async def my_business_flow(payload: dict):
    # 你的邏輯
    return {"result": "..."}
```

`create_app` 回傳標準 FastAPI `app` 實例，所有 FastAPI 功能都能用。

## 方式 B：用 APIRouter 包裝（中型應用）

```python
# my_app/routers/business.py
from fastapi import APIRouter

router = APIRouter(prefix="/business", tags=["business"])


@router.get("/users")
async def list_users():
    ...


@router.post("/orders")
async def create_order(payload: dict):
    ...


# my_app/server.py
from agentic_rag.api.server import create_app
from .routers.business import router as business_router

app = create_app(...)
app.include_router(business_router)
```

好處：路由分檔，加新功能不用碰 main file。

## 方式 C：FastAPI dependency injection（複雜應用）

如果新 endpoint 要用 AgenticRAG 內部資源（LLM provider、tool registry、ingestion service），把它們設成 dependency：

```python
# my_app/deps.py
from agentic_rag.providers.base import Provider

_provider_instance: Provider | None = None


def set_provider(p: Provider) -> None:
    global _provider_instance
    _provider_instance = p


def get_provider() -> Provider:
    if _provider_instance is None:
        raise RuntimeError("provider not configured")
    return _provider_instance


# my_app/server.py
from fastapi import Depends
from agentic_rag.api.server import create_app
from .deps import set_provider, get_provider

provider = MyProvider()
set_provider(provider)
app = create_app(provider=provider, ...)


@app.post("/summarize")
async def summarize(text: str, p: Provider = Depends(get_provider)):
    response = await p.complete([{"role": "user", "content": f"Summarize: {text}"}])
    return {"summary": response.content}
```

## 加 middleware

每個請求要做的事（auth、logging、tracing）用 FastAPI middleware：

```python
import time
import logging
from fastapi import Request

logger = logging.getLogger(__name__)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = (time.time() - start) * 1000
    logger.info(
        "%s %s -> %d (%.1fms)",
        request.method, request.url.path, response.status_code, elapsed,
    )
    return response
```

注意：AgenticRAG 已經有兩個 middleware（`ApiKeyMiddleware` + CSP service-token），加 middleware 時請理解執行順序（FastAPI middleware stack 是 LIFO）。

## OpenAPI 文件

FastAPI 自動產生 `/docs` (Swagger UI) 跟 `/redoc`。如果你要禁用（生產），在 `create_app` 之後改：

```python
app = create_app(...)
app.openapi_url = None   # 關 OpenAPI JSON
# 或
app.docs_url = None      # 關 Swagger UI
app.redoc_url = None     # 關 Redoc
```

## CORS

如果有跨網域 SPA 要打 AgenticRAG：

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-spa.example.com"],
    allow_methods=["POST", "GET"],
    allow_headers=["Authorization", "Content-Type"],
)
```

**不要** `allow_origins=["*"]` — 配合 cookie / Authorization 會有 CSRF 風險。

## 測試自訂 endpoint

```python
# tests/test_my_endpoints.py
from fastapi.testclient import TestClient

from my_app.server import app


def test_health_endpoint():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

`TestClient` 會自動處理 lifespan / startup events，跟真實 server 行為一致。
