"""Quick builder may select fixed report layouts, never upload executable templates."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.report import ReportPreset


class RenderQuickDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    preset: ReportPreset
    format: Literal["html", "pdf", "docx"]
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)
