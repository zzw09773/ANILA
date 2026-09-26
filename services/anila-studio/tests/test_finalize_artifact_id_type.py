"""CSP returns the registered artifact id as a number; Studio's job status
schemas carry it as a string. A numeric id written back onto the job record
made every status poll fail validation (HTTP 500), so the deck looked lost."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import job_lifecycle, job_reporting


class _Updater:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_numeric_artifact_id_from_csp_is_stored_as_string(monkeypatch):
    async def fake_register(**_kwargs: Any) -> dict[str, Any]:
        return {"artifact_id": 1, "classification_level": "internal"}

    patched: dict[str, Any] = {}

    async def fake_patch(**kwargs: Any) -> None:
        patched.update(kwargs)

    monkeypatch.setattr(job_reporting, "register_artifact", fake_register)
    monkeypatch.setattr(job_reporting, "report_job_patched", fake_patch)

    ctx = job_lifecycle.make_context(
        artifact_type="slides",
        owner_user_id=1,
        requester=None,
        bearer="token",
        collection_id=2,
        describe=lambda _record: job_lifecycle.ArtifactInfo(
            artifact_type="slides", title="deck", storage_ref="decks/j.pptx",
            primary_bytes=b"pptx",
        ),
    )
    record = SimpleNamespace(job_id="j_1", state="done")
    updater = _Updater()

    await job_lifecycle._finalize(record, ctx, updater)

    assert ctx.artifact_id == "1"
    assert patched["artifact_id"] == "1"
    assert updater.calls == [
        {"artifact_id": "1", "classification_level": "internal"}
    ]
