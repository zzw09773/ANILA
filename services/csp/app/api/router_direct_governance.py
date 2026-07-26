"""Internal direct-answer model governance projection for Router services.

R7.1: The Router's DIRECT_ANSWER classification ceiling is derived from the CSP
model registry ``classification_ceiling`` rather than a manual env knob.  This
service-only endpoint exposes exactly that governance fact for one active
llm-type model so the Router can run its ``enforce_model_ceiling``-equivalent
front gate; the real outbound call is still re-enforced at the CSP model
gateway.  Unknown / inactive / non-llm rows fail closed as 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.model_registry import ModelRegistry
from app.services.agent_credential_service import CallerIdentity
from app.services.auth_service import verify_service_token


router = APIRouter()


@router.get("/direct-model-governance")
def get_direct_model_governance(
    *,
    model: str = Query(..., min_length=1),
    caller: CallerIdentity | None = Depends(verify_service_token),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Return the direct-answer classification ceiling for one active llm model.

    Auth mirrors the internal registry snapshot: a named, non-legacy service
    client only.  Agent credentials and the legacy fleet token are rejected;
    this view is for Router-like ``service_clients`` and never a user API.  The
    projection deliberately exposes only the governance / capacity fields the
    Router needs — no endpoint URL, credentials, or other registry columns.

    ``context_window`` 是**容量事實**,不是授權欄位。Router 需要它才能依部署模型
    的真實容量算 token 預算 —— 否則長輸入會撞上上游 slot 上限,而使用者看到的錯誤
    完全無法診斷。它與分類上限共用這條已受信任的 service-token 投影,避免另開一條
    會漂移的通道;未登記時回 ``null``,Router 端降級成不宣告 ``max_tokens``(絕不
    猜一個值)。
    """

    if caller is None or caller.kind != "service_client" or caller.is_legacy:
        raise HTTPException(
            status_code=403, detail="direct-model governance 僅限具名 service client"
        )
    model_name = model.strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="model 參數不得為空")
    row = db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()
    if row is None or not row.is_active or row.model_type != "llm":
        # Fail closed: the Router treats a 404 as governance-unavailable and
        # denies DIRECT_ANSWER rather than guessing a ceiling.
        raise HTTPException(status_code=404, detail="model 未註冊或非可用 llm 模型")
    # 只有正整數才算「已登記容量」。0 / 負值 / 非整數是登記錯誤,一律當成未登記
    # 送出 None,絕不讓一個壞值變成 Router 的硬上限。
    raw_window = row.context_window
    context_window = (
        int(raw_window)
        if isinstance(raw_window, int)
        and not isinstance(raw_window, bool)
        and raw_window > 0
        else None
    )
    return {
        "model_id": row.name,
        "gateway": "csp",
        "classification_ceiling": row.classification_ceiling,
        "context_window": context_window,
    }


__all__ = ["get_direct_model_governance", "router"]
