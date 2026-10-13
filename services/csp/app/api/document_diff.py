"""比對兩份有條號的 Word。檔案不落地。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_current_user
from app.services.document_version_diff import (
    DocumentDiffQuota,
    compare_documents,
    explain_documents,
)

router = APIRouter(prefix="/api/document-diffs", tags=["文件版本比對"])

_MAX_BYTES = 5_000_000


class ChangeOut(BaseModel):
    id: str
    kind: Literal["added", "removed", "modified"]
    article: str
    importance: Literal["high", "low"]
    before: str
    after: str
    summary: str


class ExplanationOut(BaseModel):
    id: str
    text: str


class DiffOut(BaseModel):
    parse_complete: bool
    readable: bool
    summary: str
    warnings: list[str] = Field(default_factory=list)
    changes: list[ChangeOut] = Field(default_factory=list)
    explanations: list[ExplanationOut] = Field(default_factory=list)
    model_status: Literal[
        "not_requested",
        "skipped",
        "unavailable",
        "failed",
        "ok",
        "filtered",
    ] = "not_requested"
    model_note: str = ""


@router.post("", response_model=DiffOut)
@router.post("/", response_model=DiffOut, include_in_schema=False)
async def create_document_diff(
    before: UploadFile = File(...),
    after: UploadFile = File(...),
    _user: User = Depends(get_current_user),
) -> dict:
    return compare_documents(await _read_docx(before), await _read_docx(after))


@router.post("/explain", response_model=DiffOut)
async def explain_document_diff(
    before: UploadFile = File(...),
    after: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        return await explain_documents(db, user, await _read_docx(before), await _read_docx(after))
    except DocumentDiffQuota as exc:
        raise HTTPException(
            status_code=429,
            detail={"code": "quota_exceeded", "message": exc.message},
        ) from exc


async def _read_docx(upload: UploadFile) -> bytes:
    name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    if not name.lower().endswith(".docx"):
        raise HTTPException(
            status_code=400,
            detail="第一版只比對有條號的 Word 檔（.docx）。",
        )
    data = await upload.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail="檔案超過 5 MB，這次沒有比對。")
    if not data:
        raise HTTPException(
            status_code=400,
            detail="檔案是空的，無法比對，也不能據此說沒有差異。",
        )
    return data
