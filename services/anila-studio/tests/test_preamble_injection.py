"""Studio 投影片／報告 prompt 注入共同前導國家用語＋紀年段。"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from app.generated_preamble import ERA_RULES, NATIONAL_TERMINOLOGY
from app.services.studio_llm import build_generation_prompt


def test_build_generation_prompt_includes_national_and_era():
    system, _user = build_generation_prompt(
        collection_name="測試庫",
        preset="教學投影片",
        extra_instructions=None,
        chunks=[],
    )
    assert "中華民國" in system
    assert "民國114年" in system
    assert NATIONAL_TERMINOLOGY in system
    assert ERA_RULES in system
    assert "── 台灣用語對映" in system
    # 國家／紀年段應在台灣用語對映之前（整體內容規則區塊附近）。
    assert system.index(NATIONAL_TERMINOLOGY) < system.index("── 台灣用語對映")
    assert system.index(ERA_RULES) < system.index("── 台灣用語對映")
    assert system.index("── 整體內容規則") < system.index(NATIONAL_TERMINOLOGY)


def _ensure_report_renderer_stub() -> None:
    # report_runner → report_renderer 需要 jinja2 等（本機借用 venv 可能沒裝）。
    # 測試只驗 system prompt 組裝，stub renderer 後再 import。
    if "app.services.report_renderer" not in sys.modules:
        stub = ModuleType("app.services.report_renderer")
        stub.render_all_artifacts = lambda *a, **k: None  # type: ignore[attr-defined]
        sys.modules["app.services.report_renderer"] = stub


def _assert_composed_system(system: str, *, persona_fragment: str) -> None:
    """Call-boundary invariant: preamble once each + persona still present."""
    assert system.count(NATIONAL_TERMINOLOGY.strip()) == 1
    assert system.count(ERA_RULES.strip()) == 1
    assert "中華民國" in system
    assert "民國114年" in system
    assert persona_fragment in system


def test_report_compose_system_prompt_includes_sections_exactly_once():
    _ensure_report_renderer_stub()

    from app.schemas.report import ReportPreset
    from app.services.report_runner import _PRESET_VOICE, _compose_system_prompt

    composed = _compose_system_prompt(_PRESET_VOICE[ReportPreset.KEY_SUMMARY])
    _assert_composed_system(composed, persona_fragment="高階主管的幕僚")


def _sample_chunk(i: int = 1) -> Any:
    from app.clients.csp_client import ChunkHit

    return ChunkHit(
        chunk_id=i,
        document_id=100 + i,
        filename=f"doc_{i}.pdf",
        chunk_key=f"k_{i}",
        content=(
            f"第 {i} 段是模擬實際從 collection 取出的 chunk 內容,"
            f"需要足夠長度才能通過 chunking artifact 過濾門檻。"
        ),
        score=0.9,
        metadata={},
        parent_chunk_id=None,
        parent_content=None,
        chunk_type="leaf",
        chunk_level=0,
    )


@pytest.mark.asyncio
async def test_llm_outline_system_prompt_at_proxy_boundary(monkeypatch):
    """Outline flow: system prompt reaching proxy_chat_completions must be composed."""
    _ensure_report_renderer_stub()
    from app.schemas.report import GenerateReportRequest, ReportPreset
    from app.services import report_runner

    captured: dict[str, Any] = {}

    async def fake_proxy(*, model, messages, bearer, **kwargs):
        captured["messages"] = messages
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"title":"測試標題",'
                            '"tldr":"這是一份至少滿足二十字下限的測試摘要文字。",'
                            '"sections":[{"heading":"A","key_points":["a1"]}]}'
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(report_runner, "proxy_chat_completions", fake_proxy)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.KEY_SUMMARY
    )
    await report_runner._llm_outline(
        request=request, chunks=[_sample_chunk()], bearer="t"
    )

    assert "messages" in captured
    assert captured["messages"][0]["role"] == "system"
    _assert_composed_system(
        captured["messages"][0]["content"],
        persona_fragment="高階主管的幕僚",
    )


@pytest.mark.asyncio
async def test_llm_draft_system_prompt_at_proxy_boundary(monkeypatch):
    """Draft flow: system prompt reaching proxy_chat_completions must be composed."""
    _ensure_report_renderer_stub()
    from app.schemas.report import ReportPreset, ReportReference
    from app.services import report_runner

    captured: dict[str, Any] = {}

    async def fake_proxy(*, model, messages, bearer, **kwargs):
        captured["messages"] = messages
        return {"choices": [{"message": {"content": "section body [1]"}}]}

    monkeypatch.setattr(report_runner, "proxy_chat_completions", fake_proxy)

    chunk = _sample_chunk(1)
    await report_runner._llm_draft_section(
        preset=ReportPreset.KEY_SUMMARY,
        heading="章節一",
        key_points=["重點一"],
        chunks_for_section=[chunk],
        references=[
            ReportReference(
                n=1,
                filename=chunk.filename,
                chunk_id=chunk.chunk_id,
                excerpt="excerpt",
            )
        ],
        bearer="t",
    )

    assert "messages" in captured
    assert captured["messages"][0]["role"] == "system"
    _assert_composed_system(
        captured["messages"][0]["content"],
        persona_fragment="高階主管的幕僚",
    )
