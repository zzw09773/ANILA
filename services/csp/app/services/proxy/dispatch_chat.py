# -*- coding: utf-8 -*-
"""/v1/chat/completions 接受派工 JWT，轉給 agent 核准的底層模型。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt as jose_jwt
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.caller import Caller, get_caller
from app.models.agent import Agent
from app.models.user import User
from app.services.agent_availability import AGENT_TEMPORARILY_UNAVAILABLE
from app.services.proxy.dispatch_token import (
    DISPATCH_TOKEN_AUDIENCE,
    extract_bearer_token,
    verify_dispatch_token,
)


@dataclass(frozen=True)
class DispatchModelCall:
    """提問者透過已核准 agent 呼叫其註冊底層模型。"""

    user: User
    agent: Agent
    department_id: int | None
    # 只來自驗過的派工 claims。請求標頭與 body 不能改這兩個值。
    task_id: int | None = None
    conversation_id: int | None = None


def _dispatch_audience(token: str) -> bool:
    try:
        unverified = jose_jwt.get_unverified_claims(token)
    except JWTError:
        return False
    aud = unverified.get("aud")
    if isinstance(aud, list):
        return DISPATCH_TOKEN_AUDIENCE in aud
    return aud == DISPATCH_TOKEN_AUDIENCE


def _department_id(claims: dict, user: User) -> int | None:
    raw = claims.get("department")
    if raw is None:
        return user.department_id
    try:
        return int(raw)
    except (TypeError, ValueError):
        return user.department_id


def _claim_positive_int(claims: dict, name: str) -> int | None:
    raw = claims.get(name)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def dispatch_model_call(db: Session, claims: dict) -> DispatchModelCall:
    """驗過的派工 claims → 提問者與已核准 agent。不查提問者的模型授權。"""
    agent = db.query(Agent).filter(Agent.id == int(claims["agent_id"])).first()
    if agent is None:
        raise HTTPException(status_code=401, detail="dispatch token 對應的 agent 不存在")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=403, detail="此 agent 尚未核准")
    if agent.unavailable_reason:
        raise HTTPException(status_code=403, detail=AGENT_TEMPORARILY_UNAVAILABLE)
    user = db.query(User).filter(User.id == int(claims["user_id"])).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="派工 JWT 對應的使用者不存在")
    return DispatchModelCall(
        user=user,
        agent=agent,
        department_id=_department_id(claims, user),
        task_id=_claim_positive_int(claims, "task_id"),
        conversation_id=_claim_positive_int(claims, "conversation_id"),
    )


def enforce_dispatched_model_ceiling(db: Session, call: DispatchModelCall, model) -> None:
    """出向前用簽過的任務或對話等級，跑與一般模型路徑相同的上限檢查。

    任務 claim 優先於對話。等級只從資料庫讀，不讀 agent 帶來的標頭或 body。
    """
    from app.models.conversation import Conversation
    from app.models.task import Task, TaskRun
    from app.services.proxy.ceiling import enforce_model_ceiling
    from app.services.proxy.task_link import TaskRunContext

    task_ctx = None
    conv_id: int | None = None
    if call.task_id is not None:
        task = db.get(Task, call.task_id)
        if task is None or task.requester_user_id != call.user.id:
            raise HTTPException(status_code=403, detail="派工 JWT 的任務不屬於提問者")
        run = (
            db.query(TaskRun)
            .filter(TaskRun.task_id == task.id, TaskRun.status == "running")
            .order_by(TaskRun.id.desc())
            .first()
        )
        task_ctx = TaskRunContext(
            task_id=task.id,
            trace_id=task.trace_id,
            task_run_id=run.id if run is not None else -1,
        )
    elif call.conversation_id is not None:
        conversation = db.get(Conversation, call.conversation_id)
        if conversation is None or conversation.user_id != call.user.id:
            raise HTTPException(status_code=403, detail="派工 JWT 的對話不屬於提問者")
        conv_id = conversation.id
    enforce_model_ceiling(
        db,
        model=model,
        caller=call,
        task_ctx=task_ctx,
        conv_id_int=conv_id,
    )


def resolve_chat_caller(
    request: Request,
    db: Session = Depends(get_db),
) -> Caller | DispatchModelCall:
    """聊天端點：派工 JWT 優先，其餘仍走使用者 JWT 或 sk-。"""
    token = extract_bearer_token(request.headers.get("Authorization"))
    if token and not token.startswith(("sk-", "csk-")):
        claims = verify_dispatch_token(token)
        if claims is not None:
            return dispatch_model_call(db, claims)
        if _dispatch_audience(token):
            raise HTTPException(status_code=401, detail="派工 JWT 無效或已過期")
    return get_caller(request, db)
