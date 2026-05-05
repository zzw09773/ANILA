# 個人化記憶（User Personalization）

AgenticRAG 提供 `UserContextProvider` Protocol，讓你把任何後端的「使用者長期事實」注入 agent 的 system prompt — agent 不必知道這些事實怎麼來的。

## 何時需要

如果你想讓 agent：
- 記住使用者的姓名、職稱、語言偏好等長期屬性
- 跨對話 / 跨 session 維持個人化
- 根據使用者背景做不同回應風格

就需要一個 `UserContextProvider`。

如果不需要，**什麼都不做** — 預設 `NoopUserContextProvider` 會讓 agent 跟舊版完全一樣（無個人化）。

## Protocol 介面

```python
from agentic_rag.runtime.personalization import UserContextProvider, UserFact
from fastapi import Request

class MyProvider:
    """Implement this — sync 或 async 都行。"""
    async def get_user_facts(self, request: Request) -> list[UserFact]:
        # 從 request.headers / request.cookies / request.state 拿 user 識別
        # 查你的後端
        # 回傳事實清單
        ...
```

`UserFact` 是 frozen dataclass：
- `key: str` — 事實名（建議用 dot-notation：`profile.name`、`preference.language`）
- `value: str` — 事實值
- `confidence: float = 1.0` — 信心分數（0.0–1.0），預設 1.0

## Wiring：注入到 `create_app`

```python
from agentic_rag.api.server import create_app

provider = MyProvider()  # 你的實作
app = create_app(
    provider=llm_provider,
    tool_registry=tools,
    user_context_provider=provider,  # ← 這行
    ...
)
```

如果不傳，自動使用 `NoopUserContextProvider`（無事實）。

---

## 範例 1：靜態事實（測試 / dev / demo）

對 demo 或測試很有用 — 寫死一組假事實，agent 行為可重現。

```python
# my_app/static_facts.py
from agentic_rag.runtime.personalization import UserFact
from fastapi import Request


class StaticFactsProvider:
    """Returns the same facts for every request — dev / demo only."""

    def __init__(self, facts: list[UserFact]) -> None:
        self._facts = facts

    async def get_user_facts(self, request: Request) -> list[UserFact]:
        return list(self._facts)


# 使用：
provider = StaticFactsProvider([
    UserFact(key="name", value="Demo User"),
    UserFact(key="role", value="engineer"),
    UserFact(key="preferred_language", value="zh-TW"),
])
app = create_app(..., user_context_provider=provider)
```

每次對話 agent 的 system prompt 都會帶有這 3 條事實。

---

## 範例 2：HTTP backend（生產：跟 identity / memory service 對接）

從外部 service 拿事實。失敗時退化（empty list）才不會讓對話整個失敗。

```python
# my_app/http_provider.py
import logging
import os
from dataclasses import dataclass

import httpx
from fastapi import Request

from agentic_rag.runtime.personalization import UserFact

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpProviderConfig:
    base_url: str           # e.g. "https://identity.example.com"
    bearer_token: str       # service-to-service auth
    timeout_seconds: float = 5.0


class HttpFactsProvider:
    """Fetches user facts from an external HTTP service.

    Conventions assumed:
    - Caller user_id arrives via 'X-User-Id' request header.
    - Backend exposes GET {base_url}/users/{user_id}/facts
    - Response shape: {"facts": [{"key": ..., "value": ..., "confidence": ...}, ...]}
    """

    def __init__(self, config: HttpProviderConfig) -> None:
        self._config = config

    async def get_user_facts(self, request: Request) -> list[UserFact]:
        user_id = request.headers.get("X-User-Id")
        if not user_id:
            return []  # 沒識別 → 沒個人化（不報錯）

        url = f"{self._config.base_url.rstrip('/')}/users/{user_id}/facts"
        headers = {"Authorization": f"Bearer {self._config.bearer_token}"}

        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                resp = await client.get(url, headers=headers)
        except httpx.RequestError as exc:
            logger.warning("facts fetch failed user_id=%s: %s", user_id, exc)
            return []  # 後端掛了不要拖垮 chat

        if resp.status_code != 200:
            logger.warning(
                "facts fetch non-200 user_id=%s status=%s",
                user_id, resp.status_code,
            )
            return []

        try:
            payload = resp.json()
        except ValueError:
            return []

        facts: list[UserFact] = []
        for item in payload.get("facts", []):
            if not isinstance(item, dict):
                continue
            key, value = item.get("key"), item.get("value")
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            try:
                confidence = float(item.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 1.0
            facts.append(UserFact(key=key, value=value, confidence=confidence))
        return facts


# 使用：
provider = HttpFactsProvider(HttpProviderConfig(
    base_url=os.environ["IDENTITY_API_URL"],
    bearer_token=os.environ["IDENTITY_TOKEN"],
))
app = create_app(..., user_context_provider=provider)
```

部署在 ANILA 平台時，header 名稱跟 endpoint shape 由 ANILA 定，請參考 [`docs/deploy/anila.md`](../deploy/anila.md)。

---

## 範例 3：DB-backed（local SQLite / Postgres）

當你已經有自己的使用者 DB 時：

```python
# my_app/db_provider.py
import asyncpg
from fastapi import Request

from agentic_rag.runtime.personalization import UserFact


class PostgresFactsProvider:
    """Reads facts directly from a Postgres table.

    Schema assumed:
        CREATE TABLE user_facts (
            user_id INT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            confidence FLOAT DEFAULT 1.0,
            PRIMARY KEY (user_id, key)
        );
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def get_user_facts(self, request: Request) -> list[UserFact]:
        user_id = request.headers.get("X-User-Id")
        if not user_id:
            return []
        try:
            user_id_int = int(user_id)
        except ValueError:
            return []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT key, value, confidence FROM user_facts "
                "WHERE user_id = $1 ORDER BY key",
                user_id_int,
            )
        return [
            UserFact(key=r["key"], value=r["value"], confidence=float(r["confidence"]))
            for r in rows
        ]
```

---

## 渲染格式自訂

預設 `format_user_facts_block` 會輸出 Markdown：

```markdown
## 使用者背景（已記住的事實）
- **name**: Sara
- **role**: engineer

以上是平台對使用者的長期記憶...
```

如果你要不同的格式（JSON、YAML、結構化 tag），就**不要用** AgenticRAG 內建的 `_enrich_system_prompt_with_user_facts` — 自己改寫 chat handler 或在你的 provider 裡直接組好 prompt 字串放進 `UserFact.value`。

---

## 測試你的 Provider

```python
# tests/test_my_provider.py
import pytest
from unittest.mock import MagicMock
from agentic_rag.runtime.personalization import UserContextProvider, UserFact

from my_app.static_facts import StaticFactsProvider


@pytest.mark.asyncio
async def test_my_provider_satisfies_protocol():
    provider = StaticFactsProvider([UserFact(key="x", value="y")])
    assert isinstance(provider, UserContextProvider)


@pytest.mark.asyncio
async def test_my_provider_returns_facts():
    provider = StaticFactsProvider([
        UserFact(key="name", value="Sara"),
    ])
    facts = await provider.get_user_facts(MagicMock())
    assert len(facts) == 1
    assert facts[0].key == "name"
```

`UserContextProvider` 是 `runtime_checkable` Protocol，所以 `isinstance` 會 duck-type 檢查 — 你的類別不必繼承任何東西，方法簽章對就過。
