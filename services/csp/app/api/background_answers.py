# -*- coding: utf-8 -*-
"""送出「完整回答，做好再通知我」。回答在背景完成。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.bulk_identity import ExpectedUserId, reject_if_expected_user_mismatch
from app.database import get_db
from app.models.user import User
from app.services.background_answer import enqueue_background_answer

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


class BackgroundAnswerCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=500_000)
    agent_name: Optional[str] = Field(None, max_length=100)
    attachment_reference_ids: list[str] = Field(default_factory=list, max_length=20)
    metadata: Optional[dict] = None


def _without_writer(metadata):
    if not isinstance(metadata, dict):
        return metadata
    stream = metadata.get("anila_stream")
    if not isinstance(stream, dict) or "writer" not in stream:
        return metadata
    cleaned = {key: value for key, value in stream.items() if key != "writer"}
    return {**metadata, "anila_stream": cleaned}


@router.post("/{conv_id}/background-answers", status_code=202)
def create_background_answer(
    conv_id: int,
    body: BackgroundAnswerCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    expected_user_id: ExpectedUserId = None,
):
    """收下問題後立刻回覆。完成時寫一筆既有的 /api/notifications。"""
    reject_if_expected_user_mismatch(current_user, expected_user_id)
    from app.api.conversations import TurnHeadOut, _message_out, _stored_content
    from app.services import message_tree as mtree
    from app.services.background_answer import run_detached

    user_msg, assistant_msg, unanswered_msg = enqueue_background_answer(
        db,
        conv_id,
        current_user,
        content=_stored_content(body.content, current_user) or "",
        agent_name=body.agent_name,
        attachment_reference_ids=body.attachment_reference_ids,
        user_metadata=body.metadata,
    )
    background_tasks.add_task(run_detached, assistant_msg.id)
    edges = mtree.load_edges(db, conv_id)
    groups = mtree.sibling_groups(edges)
    out = TurnHeadOut(
        user=_message_out(
            user_msg, groups.get(user_msg.parent_id, [user_msg.id]), user=current_user,
        ),
        assistant=_message_out(
            assistant_msg,
            groups.get(assistant_msg.parent_id, [assistant_msg.id]),
            user=current_user,
        ),
        unanswered=(
            None
            if unanswered_msg is None
            else _message_out(
                unanswered_msg,
                groups.get(unanswered_msg.parent_id, [unanswered_msg.id]),
                user=current_user,
            )
        ),
    )
    if out.assistant.metadata is not None:
        out.assistant.metadata = _without_writer(out.assistant.metadata)
    return out
