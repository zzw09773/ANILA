"""Regression: LLM fallback deck must surface a soft warning on JobStatus.

Silent "done" after schema exhaustion is the defect this locks down —
the SPA reads ``warning`` and shows amber copy instead of celebrating.
"""
from __future__ import annotations

import pytest

from app.api.studio import FALLBACK_DECK_WARNING
from app.schemas.studio import JobStatus, SlidesSpec
from app.services import studio_job_service as jobs


@pytest.fixture(autouse=True)
def _clean_jobs():
    jobs._reset_for_tests()
    yield
    jobs._reset_for_tests()


@pytest.mark.asyncio
async def test_mark_done_carries_fallback_warning():
    async def runner(updater: jobs.JobUpdater) -> None:
        spec = SlidesSpec.model_validate(
            {
                "title": "生成失敗說明",
                "theme": "corporate_navy",
                "slides": [
                    {
                        "title": "無法產出合法結構",
                        "bullets": ["請重試"],
                    }
                ],
            }
        )
        await updater.mark_done(
            spec=spec,
            pptx_bytes=b"PK-fake-pptx",
            defects=[],
            qa_passes=0,
            warning=FALLBACK_DECK_WARNING,
        )

    rec = await jobs.create_job(
        user_id=1, collection_id=1, runner=runner, report_ctx=None,
    )
    # Wait for the spawned task to finish.
    task = jobs.get_job(rec.job_id).task
    assert task is not None
    await task

    done = jobs.get_job(rec.job_id)
    assert done is not None
    assert done.state == "done"
    assert done.warning is not None
    assert done.warning == FALLBACK_DECK_WARNING

    status = done.to_status()
    assert isinstance(status, JobStatus)
    assert status.warning == FALLBACK_DECK_WARNING
    # Done + warning must coexist — not flipped to failed.
    assert status.state == "done"
    assert status.error is None


@pytest.mark.asyncio
async def test_mark_done_without_warning_stays_clean():
    async def runner(updater: jobs.JobUpdater) -> None:
        spec = SlidesSpec.model_validate(
            {
                "title": "正常簡報",
                "theme": "corporate_navy",
                "slides": [{"title": "頁一", "bullets": ["a", "b"]}],
            }
        )
        await updater.mark_done(
            spec=spec,
            pptx_bytes=b"PK-ok",
            defects=[],
            qa_passes=1,
        )

    rec = await jobs.create_job(
        user_id=1, collection_id=1, runner=runner, report_ctx=None,
    )
    await jobs.get_job(rec.job_id).task
    status = jobs.get_job(rec.job_id).to_status()
    assert status.state == "done"
    assert status.warning is None
