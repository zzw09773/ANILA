"""Tests for dispatch_to_agent — verifies it calls CSP proxy, not agent directly."""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from anila_core.tools.dispatch_tool import dispatch_to_agent


class TestDispatchToAgent:
    @pytest.mark.asyncio
    async def test_calls_csp_not_agent_directly(self, respx_mock):
        """dispatch_to_agent must POST to CSP /v1/chat/completions with model=agent_id."""
        csp_url = "http://mock-csp:8000"
        expected_model = "my-test-agent"

        route = respx_mock.post(f"{csp_url}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "choices": [{
                        "message": {"role": "assistant", "content": "agent reply"},
                        "finish_reason": "stop",
                        "index": 0,
                    }],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3,
                              "total_tokens": 8},
                },
            )
        )

        result = await dispatch_to_agent(
            agent_id=expected_model,
            query="test query",
            csp_base_url=csp_url,
            csp_api_key="sk-test",
            stream=False,
        )

        assert route.called
        request = route.calls.last.request
        body = json.loads(request.content)
        assert body["model"] == expected_model
        assert body["messages"][0]["content"] == "test query"
        assert "agent reply" in result

    @pytest.mark.asyncio
    async def test_bearer_token_is_csp_api_key(self, respx_mock):
        """The Authorization header must carry the CSP API key, not a user JWT."""
        csp_url = "http://mock-csp:8000"
        csp_api_key = "sk-my-csp-key-123"

        route = respx_mock.post(f"{csp_url}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion",
                    "choices": [{"message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop", "index": 0}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                              "total_tokens": 2},
                },
            )
        )

        await dispatch_to_agent(
            agent_id="some-agent",
            query="hello",
            csp_base_url=csp_url,
            csp_api_key=csp_api_key,
            stream=False,
        )

        request = route.calls.last.request
        assert request.headers["authorization"] == f"Bearer {csp_api_key}"

    @pytest.mark.asyncio
    async def test_raises_on_http_error(self, respx_mock):
        csp_url = "http://mock-csp:8000"
        respx_mock.post(f"{csp_url}/v1/chat/completions").mock(
            return_value=httpx.Response(403, json={"detail": "forbidden"})
        )

        with pytest.raises(Exception):
            await dispatch_to_agent(
                agent_id="forbidden-agent",
                query="hello",
                csp_base_url=csp_url,
                csp_api_key="sk-bad",
                stream=False,
            )
