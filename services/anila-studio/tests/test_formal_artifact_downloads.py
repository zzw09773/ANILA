"""Formal Gate 3 downloads are authoritative only through CSP Artifact."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import CurrentUserIdentity
from app.config import settings


@pytest.mark.asyncio
async def test_all_five_studio_local_downloads_are_always_gone(
    monkeypatch,
) -> None:
    from app.api.datatables import download_datatable_artifact
    from app.api.infographics import download_infographic_artifact
    from app.api.mindmaps import download_mindmap
    from app.api.reports import download_report
    from app.api.studio import get_slides_job_pptx

    monkeypatch.setattr(settings, "ANILA_DEPLOYMENT_PROFILE", "unknown-typo-profile")
    identity = CurrentUserIdentity(id=7, username="EMP0007", role="user", token_version=0)
    calls = [
        lambda: get_slides_job_pptx("job", identity),
        lambda: download_report("job", "pdf", identity),
        lambda: download_mindmap("job", "svg", identity),
        lambda: download_infographic_artifact("job", "pdf", identity),
        lambda: download_datatable_artifact("job", "xlsx", identity),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 410
        assert "CSP Artifact" in str(exc.value.detail)
