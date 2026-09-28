"""Studio 向 CSP 換一張工作委託權杖。

只收使用者的存取權杖。分類從知識庫列上讀，不信請求本文。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.services.auth_service import require_interactive_user
from app.services.studio_job_token import (
    STUDIO_JOB_TOKEN_TTL_MINUTES,
    issue_studio_job_token,
)

router = APIRouter(prefix="/api/studio", tags=["Studio 工作委託"])


class StudioJobTokenRequest(BaseModel):
    job_id: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    collection_id: int = Field(ge=1)


class StudioJobTokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/job-tokens", response_model=StudioJobTokenResponse)
def create_studio_job_token(
    body: StudioJobTokenRequest,
    current_user: User = Depends(require_interactive_user),
    db: Session = Depends(get_db),
) -> StudioJobTokenResponse:
    """以呼叫者的身分簽一張綁定 job_id 的權杖。

    知識庫必須是這個人（或管理員）看得到的那一筆。工作權杖再拿來換新的
    一張會被拒，避免把長壽命權杖變成可續期的工作階段。
    """
    from app.api.ingestion.collections import _require_collection_access

    coll = _require_collection_access(db, current_user, body.collection_id)
    stored = coll.classification_level or "無機密"
    try:
        classification = ClassificationLevel.from_storage(stored).value
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="知識庫分類等級無法用於工作委託"
        ) from exc
    token = issue_studio_job_token(
        user_id=current_user.id,
        job_id=body.job_id,
        classification=classification,
        token_version=int(current_user.token_version or 0),
        db=db,
    )
    return StudioJobTokenResponse(
        token=token,
        token_type="bearer",
        expires_in=STUDIO_JOB_TOKEN_TTL_MINUTES * 60,
    )
