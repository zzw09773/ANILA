"""GeometricQA — HTTP client wrapping renderer /qa-geometric."""
from __future__ import annotations

import httpx
import pytest
import respx

from app.services.geometric_qa import run_geometric_qa, GeometricDefect


@pytest.mark.asyncio
@respx.mock
async def test_returns_parsed_defects():
    respx.post("http://pptx-renderer:7100/qa-geometric").mock(
        return_value=httpx.Response(200, json={
            "defects": [
                {"slide_index": 0, "severity": "warning", "kind": "whitespace", "detail": "ratio=0.62"},
                {"slide_index": 2, "severity": "critical", "kind": "overflow", "detail": "x+w=13.7"},
            ]
        })
    )
    out = await run_geometric_qa(b"fake pptx bytes", renderer_url="http://pptx-renderer:7100")
    assert len(out) == 2
    assert out[0].kind == "whitespace"
    assert out[1].severity == "critical"


@pytest.mark.asyncio
@respx.mock
async def test_returns_empty_on_500():
    respx.post("http://pptx-renderer:7100/qa-geometric").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    out = await run_geometric_qa(b"x", renderer_url="http://pptx-renderer:7100")
    assert out == []


@pytest.mark.asyncio
@respx.mock
async def test_returns_empty_on_malformed_json():
    respx.post("http://pptx-renderer:7100/qa-geometric").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})
    )
    out = await run_geometric_qa(b"x", renderer_url="http://pptx-renderer:7100")
    assert out == []


@pytest.mark.asyncio
@respx.mock
async def test_skips_malformed_defect_entries():
    """Defect entries with missing required fields are skipped — don't crash the whole QA pass."""
    respx.post("http://pptx-renderer:7100/qa-geometric").mock(
        return_value=httpx.Response(200, json={
            "defects": [
                {"slide_index": 0, "severity": "warning", "kind": "ws"},
                {"missing_required": True},
                {"slide_index": "not_int", "severity": "x", "kind": "y"},
            ]
        })
    )
    out = await run_geometric_qa(b"x", renderer_url="http://pptx-renderer:7100")
    assert len(out) == 1
