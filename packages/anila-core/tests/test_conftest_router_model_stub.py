"""Guards for the shared router-model-resolve stub in tests/conftest.py.

The suite-wide autouse fixture stubs ``_csp_resolve_router_model`` so that
networkless Router tests do not fail closed against a live CSP. Tests that DO
want the real hop opt out with ``@pytest.mark.real_router_model_resolve``.

These two cases pin that contract: without the marker the hop is swallowed,
with the marker it really runs (proved by leaving resolve unmocked and
asserting the un-mocked request surfaces).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from fastapi.testclient import TestClient

from anila_core.api.router_server import create_router_app
from anila_core.config import settings
from anila_core.memory import close_all_connections

CSP_CHAT_URL = f"{settings.csp_base_url}/v1/chat/completions"


@pytest_asyncio.fixture
async def db_path(tmp_path: Path):
    db = tmp_path / "router-model-stub.db"
    yield db
    await close_all_connections()


def _reply(text: str) -> dict:
    return {
        "id": "c",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _post(db_path: Path, session_id: str):
    client = TestClient(create_router_app(session_db_path=str(db_path)))
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-x"},
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "session_id": session_id,
        },
    )


@respx.mock
def test_stub_hides_the_resolve_hop_without_the_marker(db_path: Path) -> None:
    # Only the chat call is mocked; resolve is not. The autouse stub hides it.
    respx.post(CSP_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_reply("answer"))
    )
    assert _post(db_path, "s-stub").status_code == 200


@respx.mock
@pytest.mark.real_router_model_resolve
def test_marker_lets_the_real_resolve_hop_run(db_path: Path) -> None:
    respx.post(CSP_CHAT_URL).mock(
        return_value=httpx.Response(200, json=_reply("answer"))
    )
    with pytest.raises(Exception) as excinfo:
        _post(db_path, "s-real")
    assert "not mocked" in str(excinfo.value)
