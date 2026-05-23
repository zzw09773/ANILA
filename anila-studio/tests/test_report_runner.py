"""Tests for app.services.report_runner — pipeline behaviours in isolation.

We focus on:
  - retrieval failure propagates as a clear RuntimeError,
  - empty chunk pool refuses to start the LLM stage,
  - outline JSON parse failure surfaces as RuntimeError,
  - per-section draft failure degrades gracefully (other sections still ship),
  - normalization runs OpenCC + LaTeX + citation strip on visible text,
  - the runner emits the expected step labels in order.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.clients.csp_client import ChunkHit, CspServerError
from app.schemas.report import (
    GenerateReportRequest,
    JOB_STEP_DRAFTING,
    JOB_STEP_NORMALIZING,
    JOB_STEP_OUTLINING,
    JOB_STEP_RENDERING,
    JOB_STEP_RETRIEVING,
    ReportPreset,
)
from app.services import report_runner
from app.services.report_job_service import ReportJobUpdater


# ── Helpers ──────────────────────────────────────────────────────────────


def _chunk(i: int) -> ChunkHit:
    return ChunkHit(
        chunk_id=i,
        document_id=100 + i,
        filename=f"doc_{i}.pdf",
        chunk_key=f"k_{i}",
        content=f"chunk content {i} 视频测试",  # simplified Chinese to verify OpenCC
        score=0.9 - i * 0.05,
        metadata={},
        parent_chunk_id=None,
        parent_content=None,
        chunk_type="leaf",
        chunk_level=0,
    )


def _outline_response(sections: list[dict]) -> dict:
    import json

    payload = {
        "title": "測試標題",
        "tldr": "這是一份至少滿足二十字下限的測試摘要文字。",
        "sections": sections,
    }
    return {
        "choices": [
            {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
        ]
    }


def _draft_response(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


class _RecordingUpdater(ReportJobUpdater):
    """Updater that records every set() call for assertion."""

    def __init__(self) -> None:
        super().__init__(job_id="r_test_runner")
        self.steps: list[str] = []
        self.final_state: str | None = None
        self.final_download_urls: dict[str, str] | None = None
        self.final_title: str | None = None
        self.final_sections_count: int | None = None
        self.final_references_count: int | None = None

    async def set(self, **kwargs):
        if "step" in kwargs and kwargs["step"]:
            self.steps.append(kwargs["step"])
        if "state" in kwargs and kwargs["state"]:
            self.final_state = kwargs["state"]
        if "download_urls" in kwargs and kwargs["download_urls"]:
            self.final_download_urls = kwargs["download_urls"]
        if "title" in kwargs and kwargs["title"]:
            self.final_title = kwargs["title"]
        if "sections_count" in kwargs and kwargs["sections_count"] is not None:
            self.final_sections_count = kwargs["sections_count"]
        if "references_count" in kwargs and kwargs["references_count"] is not None:
            self.final_references_count = kwargs["references_count"]

    async def mark_done(self, *, spec, download_urls):
        await self.set(
            state="done",
            step="done",
            title=spec.title,
            sections_count=len(spec.sections),
            references_count=len(spec.references),
            download_urls=download_urls,
        )


# ── Fixture ──────────────────────────────────────────────────────────────


@pytest.fixture
def runner_env(monkeypatch, tmp_path: Path):
    """Patch csp + renderer; redirect ARTIFACTS_DIR to tmp."""
    async def fake_pdf(html, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"%PDF-fake")

    async def fake_docx(html, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"PK\x03\x04fake")

    from app.services import report_renderer as renderer_mod

    monkeypatch.setattr(renderer_mod, "write_pdf_file", fake_pdf)
    monkeypatch.setattr(renderer_mod, "write_docx_file", fake_docx)
    return tmp_path


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retrieval_failure_surfaces_as_runtime_error(runner_env, monkeypatch):
    async def boom(*a, **kw):
        raise CspServerError("retrieval down")

    monkeypatch.setattr(report_runner, "search_chunks", boom)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.KEY_SUMMARY
    )
    updater = _RecordingUpdater()
    with pytest.raises(RuntimeError, match="檢索失敗"):
        await report_runner.run_report_pipeline(
            request=request,
            bearer="t",
            updater=updater,
            artifacts_dir=runner_env,
        )
    assert updater.steps == [JOB_STEP_RETRIEVING]


@pytest.mark.asyncio
async def test_empty_chunk_pool_refuses_to_proceed(runner_env, monkeypatch):
    async def empty(*a, **kw):
        return []

    monkeypatch.setattr(report_runner, "search_chunks", empty)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.KEY_SUMMARY
    )
    updater = _RecordingUpdater()
    with pytest.raises(RuntimeError, match="找不到相關內容"):
        await report_runner.run_report_pipeline(
            request=request,
            bearer="t",
            updater=updater,
            artifacts_dir=runner_env,
        )


@pytest.mark.asyncio
async def test_outline_invalid_json_surfaces_as_runtime_error(
    runner_env, monkeypatch
):
    async def chunks(*a, **kw):
        return [_chunk(1), _chunk(2)]

    async def bad_outline(*, model, messages, bearer, **kwargs):
        return {"choices": [{"message": {"content": "not JSON at all"}}]}

    monkeypatch.setattr(report_runner, "search_chunks", chunks)
    monkeypatch.setattr(report_runner, "proxy_chat_completions", bad_outline)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.KEY_SUMMARY
    )
    updater = _RecordingUpdater()
    with pytest.raises(RuntimeError, match="大綱生成失敗"):
        await report_runner.run_report_pipeline(
            request=request,
            bearer="t",
            updater=updater,
            artifacts_dir=runner_env,
        )


@pytest.mark.asyncio
async def test_per_section_draft_failure_degrades_gracefully(
    runner_env, monkeypatch
):
    """If ONE draft call fails the section gets a placeholder; the whole
    job still finishes."""
    chunks = [_chunk(i) for i in range(1, 6)]

    async def fake_chunks(*a, **kw):
        return chunks

    call_idx = {"i": 0}

    async def selective(*, model, messages, bearer, **kwargs):
        call_idx["i"] += 1
        # First call → outline; second call (first draft) → fail; third+ → ok
        last = messages[-1]["content"]
        if "JSON object" in last:
            return _outline_response(
                [
                    {"heading": "A", "key_points": ["a1"]},
                    {"heading": "B", "key_points": ["b1"]},
                ]
            )
        # Draft call
        if call_idx["i"] == 2:
            raise ValueError("upstream draft failure")
        return _draft_response(f"正常內容 {call_idx['i']}")

    monkeypatch.setattr(report_runner, "search_chunks", fake_chunks)
    monkeypatch.setattr(report_runner, "proxy_chat_completions", selective)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.KEY_SUMMARY
    )
    updater = _RecordingUpdater()
    await report_runner.run_report_pipeline(
        request=request,
        bearer="t",
        updater=updater,
        artifacts_dir=runner_env,
    )
    assert updater.final_state == "done"
    assert updater.final_sections_count == 2  # both sections present
    # File on disk should contain the placeholder text for the failed section
    html_path = runner_env / f"{updater.job_id}.html"
    body = html_path.read_text(encoding="utf-8")
    assert "本章節撰寫失敗" in body


@pytest.mark.asyncio
async def test_normalize_converts_simplified_to_taiwan_phrasing(
    runner_env, monkeypatch
):
    """The spec's title is "测试标题" — OpenCC s2twp should output
    Taiwan-style traditional characters in the rendered HTML."""

    async def chunks(*a, **kw):
        return [_chunk(1), _chunk(2)]

    async def llm(*, model, messages, bearer, **kwargs):
        last = messages[-1]["content"]
        if "JSON object" in last:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"title": "视频测试标题", '
                                '"tldr": "这是一份用于检验繁简转换功能的测试摘要文字内容长度。", '
                                '"sections": [{"heading":"信息处理","key_points":["数据采集"]}]}'
                            )
                        }
                    }
                ]
            }
        return _draft_response("使用视频与数据进行测试。")

    monkeypatch.setattr(report_runner, "search_chunks", chunks)
    monkeypatch.setattr(report_runner, "proxy_chat_completions", llm)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.KEY_SUMMARY
    )
    updater = _RecordingUpdater()
    await report_runner.run_report_pipeline(
        request=request,
        bearer="t",
        updater=updater,
        artifacts_dir=runner_env,
    )
    assert updater.final_state == "done"

    html_path = runner_env / f"{updater.job_id}.html"
    body = html_path.read_text(encoding="utf-8")
    # OpenCC s2twp turns 视频 → 影片, 信息 → 資訊, 数据 → 數據, etc.
    # We assert at least *one* canonical Taiwan transformation happened
    # in any of those words.
    assert ("影片" in body) or ("資訊" in body) or ("數據" in body)


@pytest.mark.asyncio
async def test_runner_emits_expected_step_sequence(runner_env, monkeypatch):
    async def chunks(*a, **kw):
        return [_chunk(1), _chunk(2)]

    async def llm(*, model, messages, bearer, **kwargs):
        last = messages[-1]["content"]
        if "JSON object" in last:
            return _outline_response(
                [{"heading": "A", "key_points": ["a1"]}]
            )
        return _draft_response("section body [1]")

    monkeypatch.setattr(report_runner, "search_chunks", chunks)
    monkeypatch.setattr(report_runner, "proxy_chat_completions", llm)

    request = GenerateReportRequest(
        collection_id=1, preset=ReportPreset.DEEP_TECH_REVIEW
    )
    updater = _RecordingUpdater()
    await report_runner.run_report_pipeline(
        request=request,
        bearer="t",
        updater=updater,
        artifacts_dir=runner_env,
    )
    # The pipeline emits steps in this order; "done" comes via mark_done
    assert updater.steps == [
        JOB_STEP_RETRIEVING,
        JOB_STEP_OUTLINING,
        JOB_STEP_DRAFTING,
        JOB_STEP_NORMALIZING,
        JOB_STEP_RENDERING,
        "done",
    ]
    assert updater.final_download_urls == {
        "html": f"/api/reports/jobs/{updater.job_id}/download/html",
        "pdf": f"/api/reports/jobs/{updater.job_id}/download/pdf",
        "docx": f"/api/reports/jobs/{updater.job_id}/download/docx",
    }
