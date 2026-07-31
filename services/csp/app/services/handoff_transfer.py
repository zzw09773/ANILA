# -*- coding: utf-8 -*-
"""對話交接的「真的交出去」那一段 —— 接受／拒絕的實際效果。

背景：2026-07-30 之前，accept/reject 只把 `handoffs.status` 翻面＋發一則
通知，對話本身完全沒有換手。那是靜默成功：送出的人以為交出去了，收到的人
按了接受也什麼都拿不到。當時的處置是讓兩支端點回 501，寧可報錯。這個模組
把它補起來。

## 語意（擁有者尚未逐條裁決，採最可逆的一組，記在報告裡供推翻）

- **接受** → `conversations.user_id` 換成接收者。接收者從此可以讀、可以
  繼續聊(平台的寫入閘是「擁有者或管理員」)。同一個 commit 內自動幫**原
  擁有者**補一筆不過期的具名分享，原擁有者不會因為交接就看不到這串對話。
- **拒絕** → 什麼都不動，只有狀態與通知(`handoff_service.resolve_handoff`)。
- **密等** → 走平台既有的外流上限 `outbound_action_allowed`
  (SYSTEM-MAP §8 L241：可外流 = 密等 ≤ 營業秘密)，與具名分享同一個判準。
  密／機密的對話**建立交接與接受交接兩端都擋**：建立時擋是為了讓送出的人
  當下就知道；接受時再擋一次，是因為對話可能在送出之後才閂上去(agent
  policy latch、手動列管)。

## 為什麼是「換手」而不是「兩個人都是擁有者」

平台的寫入閘 `conversation_service._check_access` 只認「擁有者或管理員」，
沒有共同擁有者的概念，而那個檔案此刻由別的包持有(派工約束)。要讓接收者
真的能**繼續**這串對話(不是只能看)，在不動那個檔案的前提下，唯一的路就是
換 `user_id`。原擁有者因此保有讀取但失去寫入 —— 這是本模組唯一一處與
「誰都不失去任何東西」的差距，而它是可逆的：接收者可以用同一顆按鈕把對話
交回來。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, ConversationShare
from app.models.handoff import Handoff
from app.models.user import User
from app.schemas.contracts.classification import (
    ClassificationLevel,
    classification_audit_required,
    outbound_action_allowed,
)
from app.services import handoff_service as svc
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier


# ── 密等把關 ──────────────────────────────────────────────────────────────────

def guard_transferable(conv: Conversation) -> ClassificationLevel:
    """交接是外流動作，套 SYSTEM-MAP §8 L241 的上限。回傳現行密等。

    判準直接用契約層的 `outbound_action_allowed`，不另抄一份門檻 ——
    OE-4 的教訓：門檻散在各處就會漂移。
    """
    level = ClassificationLevel.from_storage(conv.classification_level)
    if not outbound_action_allowed(level):
        raise HTTPException(
            status_code=403,
            detail=(
                f"此對話密等為「{level.to_storage()}」，超過營業秘密，不可交接給他人。"
                "請改用密等較低的對話，或先向管理員申請降密。"
            ),
        )
    return level


def resolve_new_owner(db: Session, user_id: int, *, actor: User) -> User:
    """把 `to_user_id` 解析成一個真的、可用的帳號。

    2026-07-30 那顆假按鈕的根因就是「沒有人在檢查收件人存不存在」。這裡
    fail-closed：查不到、停用、待審、或指到自己，一律當下擋掉。
    """
    if user_id == actor.id:
        raise HTTPException(
            status_code=400,
            detail="不能把對話交給自己。請從同仁清單挑一位同事。",
        )
    target = db.query(User).filter(User.id == user_id).first()
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="找不到這位同事的帳號。請從同仁清單重新挑選。",
        )
    if not target.is_active or not target.is_approved:
        raise HTTPException(
            status_code=400,
            detail=f"帳號「{target.username}」已停用或尚未核准，無法接手對話。",
        )
    return target


# ── 接受 / 拒絕 ───────────────────────────────────────────────────────────────

def accept_handoff(db: Session, handoff_id: int, user: User) -> Handoff:
    """接受交接：對話換手給接收者，原擁有者保留讀取。

    所有檢查(權限、狀態、密等、接收帳號)都在任何寫入之前跑完，最後才由
    `resolve_handoff` 翻狀態＋發通知並 commit —— 換手與狀態同一個交易，
    不會出現「狀態是 accepted 但對話沒過去」的半套結果。
    """
    handoff = _load_pending_for_recipient(db, handoff_id, user)
    conv = _load_conversation(db, handoff)
    level = guard_transferable(conv)
    recipient = _load_recipient(db, handoff)

    previous_owner_id = conv.user_id
    if previous_owner_id != recipient.id:
        conv.user_id = recipient.id
        _keep_previous_owner_reading(db, conv, previous_owner_id, recipient)

    if classification_audit_required(level):
        log_audit_event(
            db,
            action="accept_conversation_handoff",
            resource_type="conversation",
            actor=user,
            resource_id=conv.id,
            detail=(
                f"Conversation {conv.id} transferred from user "
                f"{previous_owner_id} to {recipient.username} "
                f"at level {level.to_storage()}"
            ),
            metadata={
                "classification_level": level.to_storage(),
                "handoff_id": handoff.id,
                "previous_owner_id": previous_owner_id,
                "new_owner_id": recipient.id,
            },
        )

    return svc.resolve_handoff(db, handoff_id, user, accept=True)


def reject_handoff(db: Session, handoff_id: int, user: User) -> Handoff:
    """拒絕交接：對話一動也不動，只翻狀態並通知送出的人。"""
    _load_pending_for_recipient(db, handoff_id, user)
    return svc.resolve_handoff(db, handoff_id, user, accept=False)


# ── 內部 ──────────────────────────────────────────────────────────────────────

def _load_pending_for_recipient(
    db: Session, handoff_id: int, user: User,
) -> Handoff:
    """與 `resolve_handoff` 同一組門檻，但在任何寫入之前先跑。

    找不到與無權限收斂成同一個 404(同 cancel_handoff)，不當存在性 oracle。
    """
    handoff = db.query(Handoff).filter(Handoff.id == handoff_id).first()
    if not handoff or (
        handoff.to_user_id != user.id and not is_admin_tier(user)
    ):
        raise HTTPException(status_code=404, detail="找不到此交接請求")
    if handoff.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"此交接請求已處理 (狀態: {handoff.status})"
        )
    return handoff


def _load_conversation(db: Session, handoff: Handoff) -> Conversation:
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == handoff.conversation_id)
        .first()
    )
    if conv is None:
        raise HTTPException(status_code=404, detail="此交接的對話已不存在")
    return conv


def _load_recipient(db: Session, handoff: Handoff) -> User:
    """接受時再解析一次收件人 —— 帳號可能在送出之後被停用。"""
    if handoff.to_user_id is None:
        raise HTTPException(
            status_code=400,
            detail="這筆交接沒有指定接收人，無法接受。",
        )
    recipient = db.query(User).filter(User.id == handoff.to_user_id).first()
    if recipient is None or not recipient.is_active or not recipient.is_approved:
        raise HTTPException(
            status_code=400,
            detail="接收帳號不存在或已停用，無法接受這筆交接。",
        )
    return recipient


def _keep_previous_owner_reading(
    db: Session,
    conv: Conversation,
    previous_owner_id: int,
    recipient: User,
) -> None:
    """幫原擁有者補一筆不過期的具名分享，交接不會讓他看不到自己的對話。

    只 `db.add`／改欄位、不 commit —— 交給 `resolve_handoff` 的 commit 一起
    落地，維持整段換手的原子性。既有分享(可能是先前就分享過)只把過期時間
    清掉，不重複建列(具名分享對同一人有 unique index)。
    """
    existing = (
        db.query(ConversationShare)
        .filter(
            ConversationShare.conversation_id == conv.id,
            ConversationShare.target_user_id == previous_owner_id,
        )
        .first()
    )
    if existing is not None:
        existing.expires_at = None
        return
    db.add(
        ConversationShare(
            conversation_id=conv.id,
            target_user_id=previous_owner_id,
            target_department_id=None,
            mode="read_only",
            allow_fork=False,
            expires_at=None,
            created_by=recipient.id,
        )
    )
