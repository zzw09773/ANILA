"""Tests for the cross-tenant HttpUserFactReader (route-3 Phase 3).

Pure-client unit tests using respx to mock the CSP endpoint —
exercises serialisation, error mapping, and the DTO conversion
boundary without needing a running CSP backend.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from anila_core.memory.long_term import (
    HttpUserFactReader,
    UserFactDTO,
    UserFactReadError,
)


@pytest.mark.asyncio
@respx.mock
async def test_get_user_facts_happy_path_returns_dtos():
    """200 + well-formed payload → list[UserFactDTO] with values intact."""
    payload = {
        "total": 2,
        "facts": [
            {
                "id": 7,
                "user_id": 42,
                "key": "role",
                "value": "engineer",
                "confidence": 0.9,
                "source_conversation_id": 100,
                "source_message_id": None,
                "created_at": "2026-05-04T10:00:00+00:00",
                "updated_at": "2026-05-04T10:00:00+00:00",
            },
            {
                "id": 8,
                "user_id": 42,
                "key": "name",
                "value": "Sara",
                "confidence": 1.0,
                "source_conversation_id": 100,
                "source_message_id": None,
                "created_at": "2026-05-04T10:00:00+00:00",
                "updated_at": "2026-05-04T10:00:00+00:00",
            },
        ],
    }
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(200, json=payload)
    )

    reader = HttpUserFactReader(base_url="http://csp:8000", service_token="csk-x")
    facts = await reader.get_user_facts(user_id=42)

    assert len(facts) == 2
    assert all(isinstance(f, UserFactDTO) for f in facts)
    assert facts[0].key == "role"
    assert facts[0].value == "engineer"
    assert facts[0].confidence == 0.9
    assert facts[0].source_conversation_id == 100


@pytest.mark.asyncio
@respx.mock
async def test_get_user_facts_401_raises_classified_error():
    """Wrong / missing token → 401 → UserFactReadError(status_code=401).

    The agent runtime catches this specifically to log + degrade
    rather than crash.
    """
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(401, json={"detail": "Missing token"})
    )

    reader = HttpUserFactReader(base_url="http://csp:8000", service_token="bad")
    with pytest.raises(UserFactReadError) as exc_info:
        await reader.get_user_facts(user_id=42)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@respx.mock
async def test_get_user_facts_403_for_non_agent_token():
    """A service-client token (not agent-scoped) gets 403 from CSP.
    Pin the status-code mapping so an upstream auth refactor doesn't
    silently change agent behaviour.
    """
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(403, json={"detail": "agent only"})
    )

    reader = HttpUserFactReader(base_url="http://csp:8000", service_token="csc-x")
    with pytest.raises(UserFactReadError) as exc_info:
        await reader.get_user_facts(user_id=42)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
@respx.mock
async def test_get_user_facts_network_error_wraps_to_readerror():
    """Connection errors (CSP down, DNS failure) become
    UserFactReadError without a status_code. The agent runtime
    treats these the same as 5xx — log + degrade.
    """
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        side_effect=httpx.ConnectError("nope")
    )

    reader = HttpUserFactReader(base_url="http://csp:8000", service_token="csk-x")
    with pytest.raises(UserFactReadError) as exc_info:
        await reader.get_user_facts(user_id=42)
    assert exc_info.value.status_code is None


@pytest.mark.asyncio
@respx.mock
async def test_get_user_facts_empty_list_returns_empty_dto_list():
    """Valid response with zero facts returns ``[]`` rather than raising."""
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(200, json={"total": 0, "facts": []})
    )

    reader = HttpUserFactReader(base_url="http://csp:8000", service_token="csk-x")
    facts = await reader.get_user_facts(user_id=42)
    assert facts == []


@pytest.mark.asyncio
@respx.mock
async def test_get_user_facts_strips_trailing_slash_in_base_url():
    """Base URL with or without trailing slash both work — the agent
    SDK shouldn't have to care.
    """
    respx.get("http://csp:8000/api/memory/users/42/facts").mock(
        return_value=httpx.Response(200, json={"total": 0, "facts": []})
    )

    reader = HttpUserFactReader(
        base_url="http://csp:8000/", service_token="csk-x"
    )
    await reader.get_user_facts(user_id=42)  # would 404 if path had double slash
