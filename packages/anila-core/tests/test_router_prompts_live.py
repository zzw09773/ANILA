"""Router system prompts come from the platform settings, with the shipped text
as the only fallback (owner ruling 2026-08-22: the three prompts are editable in
the governance center without a rebuild).

Invariants exercised here (queued-fix-router-prompt-ui-knob):
  ① a change on csp reaches the router within the TTL, no rebuild;
  ② the shipped default is the verbatim text that used to be hard-coded;
  ④ csp unreachable → router keeps answering with the shipped default and logs it.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from anila_core.api import router_prompts as rp
from anila_core.api import router_server as rs


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    rs.reset_router_prompt_cache()
    monkeypatch.setattr(rs.settings, "csp_service_token", "svc-token", raising=False)
    monkeypatch.setattr(rs.settings, "csp_base_url", "http://csp.test", raising=False)
    yield
    rs.reset_router_prompt_cache()


def _client_returning(resp):
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_shipped_defaults_are_the_verbatim_templates():
    """② — before any refresh the router uses exactly the shipped text."""
    prompts = rs.current_router_prompts()
    assert prompts[rp.KEY_SYSTEM] == rp.DEFAULT_ROUTER_SYSTEM
    assert prompts[rp.KEY_PLAIN] == rp.DEFAULT_PLAIN_ASSISTANT
    assert prompts[rp.KEY_FORCED] == rp.DEFAULT_FORCED_ANSWER
    assert "{agent_list}" in rp.DEFAULT_ROUTER_SYSTEM
    assert rs.router_prompts_source() == "default"


@pytest.mark.asyncio
async def test_refresh_adopts_csp_values():
    """① — what the governance center stored is what the next prompt uses."""
    payload = {
        "prompts": {
            rp.KEY_SYSTEM: "custom system {agent_list}",
            rp.KEY_PLAIN: "custom plain",
            rp.KEY_FORCED: "custom forced",
        }
    }
    with patch.object(rs, "get_http_client", return_value=_client_returning(_Resp(200, payload))):
        await rs.refresh_router_prompts()
    prompts = rs.current_router_prompts()
    assert prompts[rp.KEY_SYSTEM] == "custom system {agent_list}"
    assert prompts[rp.KEY_PLAIN] == "custom plain"
    assert prompts[rp.KEY_FORCED] == "custom forced"
    assert rs.router_prompts_source() == "csp"
    # and the prompt builders read the live values, not module constants.
    # 今天的日期是組裝時附上的，不在治理中心存的那三段裡。
    plain = rs._build_system_prompt([])
    forced = rs._forced_answer_prompt()
    assert plain.startswith("custom plain")
    assert rp.HTML_PREVIEW_HINT_EN in plain
    assert rp.DISCLOSURE_RULE_EN in plain
    assert forced.startswith("custom forced")
    assert rp.DISCLOSURE_RULE_EN in forced
    assert "Today is " in plain
    assert "Today is " in forced
    assert "Today is" not in prompts[rp.KEY_PLAIN]
    assert "Today is" not in prompts[rp.KEY_FORCED]


@pytest.mark.asyncio
async def test_csp_unreachable_keeps_shipped_defaults_and_logs(caplog):
    """④ — kill: drop the try/except fallback → this raises instead of logging."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=ConnectionError("csp down"))
    with patch.object(rs, "get_http_client", return_value=client):
        with caplog.at_level(logging.WARNING, logger=rs.logger.name):
            await rs.refresh_router_prompts()
    prompts = rs.current_router_prompts()
    assert prompts[rp.KEY_SYSTEM] == rp.DEFAULT_ROUTER_SYSTEM
    assert rs.router_prompts_source() == "default"
    assert any("router prompts" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_csp_error_status_keeps_previous_values():
    """A 5xx after a good read must not blank the prompts."""
    good = {"prompts": {rp.KEY_SYSTEM: "good {agent_list}", rp.KEY_PLAIN: "p", rp.KEY_FORCED: "f"}}
    with patch.object(rs, "get_http_client", return_value=_client_returning(_Resp(200, good))):
        await rs.refresh_router_prompts()
    rs._router_prompt_state["at"] = 0.0  # expire the TTL
    with patch.object(rs, "get_http_client", return_value=_client_returning(_Resp(500))):
        await rs.refresh_router_prompts()
    assert rs.current_router_prompts()[rp.KEY_SYSTEM] == "good {agent_list}"


@pytest.mark.asyncio
async def test_system_prompt_without_agent_list_placeholder_is_refused():
    """A stored system prompt that lost ``{agent_list}`` cannot be formatted;
    the router must fall back to the shipped default rather than crash per
    request."""
    payload = {"prompts": {rp.KEY_SYSTEM: "no placeholder here", rp.KEY_PLAIN: "p", rp.KEY_FORCED: "f"}}
    with patch.object(rs, "get_http_client", return_value=_client_returning(_Resp(200, payload))):
        await rs.refresh_router_prompts()
    assert rs.current_router_prompts()[rp.KEY_SYSTEM] == rp.DEFAULT_ROUTER_SYSTEM
    assert rs.current_router_prompts()[rp.KEY_PLAIN] == "p"


@pytest.mark.asyncio
async def test_no_service_token_means_no_call_and_defaults():
    rs.settings.csp_service_token = ""
    client = _client_returning(_Resp(200, {"prompts": {rp.KEY_PLAIN: "x"}}))
    with patch.object(rs, "get_http_client", return_value=client):
        await rs.refresh_router_prompts()
    client.get.assert_not_called()
    assert rs.router_prompts_source() == "default"
