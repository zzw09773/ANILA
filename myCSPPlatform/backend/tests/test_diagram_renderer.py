"""diagram_renderer — dot subprocess wrapper.

Studio Fix 2 (2026-05-18). Mocks ``asyncio.create_subprocess_exec`` so
the tests don't require the `dot` binary to be installed in the test
environment — we exercise every failure path explicitly.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.diagram_renderer import render_dot_to_png

_VALID_DOT = "digraph G { A -> B }"
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_returns_png_on_success() -> None:
    """When dot binary returns 0 with PNG output, return the bytes."""
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(
        return_value=(_PNG_HEADER + b"\x00" * 100, b"")
    )

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT)

    assert out is not None
    assert out.startswith(_PNG_HEADER)


@pytest.mark.asyncio
async def test_returns_none_when_dot_missing() -> None:
    """FileNotFoundError → graceful None."""
    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("dot not found"),
    ):
        out = await render_dot_to_png(_VALID_DOT)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_on_nonzero_exit() -> None:
    fake_proc = AsyncMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", b"syntax error"))

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_on_timeout() -> None:
    fake_proc = AsyncMock()
    fake_proc.kill = AsyncMock()
    fake_proc.wait = AsyncMock()
    fake_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT, timeout=0.01)
    assert out is None


@pytest.mark.asyncio
async def test_returns_none_when_output_not_png() -> None:
    fake_proc = AsyncMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"not a png", b""))

    with patch(
        "app.services.diagram_renderer.asyncio.create_subprocess_exec",
        return_value=fake_proc,
    ):
        out = await render_dot_to_png(_VALID_DOT)
    assert out is None
