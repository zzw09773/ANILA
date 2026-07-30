"""OW-3 message-action CRUD, visibility, invoke
(docs/plans/ow3-message-actions-blueprint.md §Q1–Q2 / §Q9).

Authoring audit = fail-closed (commit=False + return-check → None ⇒ 500 +
rollback, same transaction; P1.4 idiom). Invoke audit = write-ahead
(committed BEFORE render return). Per-user rate limit runs immediately after
action resolution and before any refusal audit so throttle loops cannot
flood ``message_action_invoke_refused`` (429 itself is unrecorded).
Classification / access / not-branchable refusals write
``message_action_invoke_refused`` (commit=True, fail-closed) before the
gate HTTPException.

Declarative only: server renders the prompt; the client dispatches through
the existing chat path. No in-process execution surface.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.department import Department
from app.models.message import Message
from app.models.message_action import MessageAction, MessageActionBinding
from app.models.user import User
from app.schemas.contracts.classification import (
    ClassificationLevel,
    outbound_action_allowed,
)
from app.schemas.message_action import (
    ALLOWED_ACTION_ICONS,
    BindingSpec,
    ChoiceSpec,
    MessageActionCreate,
    MessageActionUpdate,
)
from app.services.audit_service import log_audit_event, serialize_audit_log
from app.services.auth_service import is_admin_tier
from app.services.conversation_service import (
    _check_access,
    _require_branchable,
)

logger = logging.getLogger(__name__)

RESOURCE_TYPE = "message_action"
_MAX_CHOICES = 20
_MAX_INPUT_CHARS = 2000

# Per-process fixed-window rate limit: user_id → (window_start_epoch, count)
_RATE_BUCKETS: dict[int, tuple[float, int]] = {}


def reset_rate_limit_for_tests() -> None:
    _RATE_BUCKETS.clear()


def body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_TEMPLATE_TOKEN_RE = re.compile(r"\{(content|choice|input)\}")


def render_template(
    body: str,
    *,
    content: str = "",
    choice: str = "",
    input: str = "",  # noqa: A002 — blueprint placeholder name
) -> str:
    """Single-pass substitution of {content}/{choice}/{input} ONLY.

    Never str.format, f-strings on user data, or any template engine
    (docs/plans/ow3-message-actions-blueprint.md §Q2). Values that
    themselves contain those tokens are not re-scanned.
    """
    values = {"content": content, "choice": choice, "input": input}
    return _TEMPLATE_TOKEN_RE.sub(lambda m: values[m.group(1)], body)


def _validate_icon(icon: str) -> str:
    if icon not in ALLOWED_ACTION_ICONS:
        raise HTTPException(
            status_code=400,
            detail=f"未知的圖示 '{icon}'",
        )
    return icon


def _validate_choices(choices: list[ChoiceSpec] | list[dict] | None) -> list[dict]:
    if choices is None:
        return []
    if len(choices) > _MAX_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"選項數量超過上限（{_MAX_CHOICES}）",
        )
    seen: set[str] = set()
    out: list[dict] = []
    for c in choices:
        if isinstance(c, ChoiceSpec):
            data = c.model_dump()
        else:
            data = dict(c)
        cid = data.get("id")
        if cid in seen:
            raise HTTPException(
                status_code=400,
                detail=f"選項 id 重複：'{cid}'",
            )
        seen.add(cid)
        out.append(data)
    return out


def _validate_body(body: str) -> None:
    max_chars = int(settings.ANILA_ACTION_MAX_BODY_CHARS)
    if len(body) > max_chars:
        raise HTTPException(status_code=413, detail="動作內容過大")


def _get_or_404(db: Session, action_id: int) -> MessageAction:
    row = db.query(MessageAction).filter(MessageAction.id == action_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="動作不存在")
    return row


def _may_modify_action(action: MessageAction, actor: User) -> bool:
    """True when actor is admin-tier or the action's author."""
    if is_admin_tier(actor):
        return True
    return action.created_by_user_id == actor.id


def _may_read_template(
    db: Session,
    action: MessageAction,
    actor: User,
    *,
    visible_ids: set[int] | None = None,
) -> bool:
    """True when the actor may press the action or may modify it.

    Owner transparency ruling (OW-3f): whoever can press can read the
    template that a press will send as them. Pressable matches
    ``list_visible`` / ``resolve_for_invoke``: bound-or-authored **and**
    enabled. A bound-but-disabled action therefore stops disclosing its
    template to anyone who cannot modify it. Modification rights stay
    author-or-admin.
    """
    if _may_modify_action(action, actor):
        return True
    if not action.is_enabled:
        return False
    ids = (
        visible_ids
        if visible_ids is not None
        else _user_visible_action_ids(db, actor)
    )
    return action.id in ids


def _require_action_author_or_admin(action: MessageAction, actor: User) -> None:
    """Developers may mutate only actions they created; admin-tier may any.

    Refusal uses the same 403 shape as ``require_admin`` so ownership is
    not a distinct probe signal against the authoring surface.
    """
    if _may_modify_action(action, actor):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="需要管理員權限",
    )


def _audit_fail_closed(
    db: Session,
    *,
    actor: User,
    action: str,
    resource_id: Any,
    detail: str,
    metadata: dict,
    ip_address: str | None,
) -> None:
    row = log_audit_event(
        db,
        actor=actor,
        action=action,
        resource_type=RESOURCE_TYPE,
        resource_id=resource_id,
        detail=detail,
        metadata=metadata,
        ip_address=ip_address,
        commit=False,
    )
    if row is None:
        raise HTTPException(
            status_code=500,
            detail="稽核紀錄寫入失敗，動作未執行",
        )


def _conversation_level_value(conv: Conversation) -> str:
    """Best-effort level string for refusal metadata.

    Prefer the contract parse; fall back to raw storage so an access
    refusal still records when the column is corrupt (classification
    gate itself continues to fail-closed via from_storage).
    """
    try:
        return ClassificationLevel.from_storage(conv.classification_level).value
    except ValueError:
        return conv.classification_level


def _audit_invoke_refused(
    db: Session,
    *,
    actor: User,
    action: MessageAction,
    conversation_id: int,
    conversation_level: str,
    reason: str,
    ip_address: str | None,
) -> None:
    """Durable refusal row (commit=True) before raising the gate HTTPException.

    Distinct action name from write-ahead ``message_action_invoke`` so
    export can separate attempts from successes. Fail-closed: if the
    row cannot be written, surface 500 rather than a silent refusal.
    """
    row = log_audit_event(
        db,
        actor=actor,
        action="message_action_invoke_refused",
        resource_type=RESOURCE_TYPE,
        resource_id=action.id,
        status="refused",
        detail=f"拒絕呼叫訊息動作「{action.name}」",
        metadata={
            "version": action.version,
            "conversation_id": conversation_id,
            "conversation_level": conversation_level,
            "outcome": "refused",
            "reason": reason,
        },
        ip_address=ip_address,
        commit=True,
    )
    if row is None:
        raise HTTPException(
            status_code=500,
            detail="稽核紀錄寫入失敗，動作未執行",
        )


def _snapshot_meta(
    action: MessageAction,
    *,
    previous_sha: str | None = None,
    bindings: list[dict] | None = None,
) -> dict:
    meta: dict[str, Any] = {
        "name": action.name,
        "label": action.label,
        "icon": action.icon,
        "body": action.body,
        "body_sha256": action.body_sha256,
        "choices": action.choices or [],
        "notes": action.notes,
        "version": action.version,
        "is_enabled": action.is_enabled,
    }
    if previous_sha is not None:
        meta["previous_body_sha256"] = previous_sha
    if bindings is not None:
        meta["bindings"] = bindings
    return meta


def _binding_dicts(bindings: list[MessageActionBinding]) -> list[dict]:
    return [
        {
            "scope_type": b.scope_type,
            "role": b.role,
            "department_id": b.department_id,
            "user_id": b.user_id,
        }
        for b in bindings
    ]


# ── CRUD ─────────────────────────────────────────────────────────────────────


def create_action(
    db: Session,
    *,
    payload: MessageActionCreate,
    actor: User,
    ip_address: str | None = None,
) -> MessageAction:
    icon = _validate_icon(payload.icon)
    choices = _validate_choices(payload.choices)
    _validate_body(payload.body)

    exists = (
        db.query(MessageAction)
        .filter(MessageAction.name == payload.name)
        .first()
    )
    if exists:
        raise HTTPException(status_code=400, detail="名稱已存在")

    now = datetime.now(timezone.utc)
    row = MessageAction(
        name=payload.name,
        label=payload.label,
        icon=icon,
        body=payload.body,
        body_sha256=body_sha256(payload.body),
        choices=choices,
        notes=payload.notes,
        version=1,
        is_enabled=payload.is_enabled,
        created_by_user_id=actor.id,
        updated_by_user_id=actor.id,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()

    _audit_fail_closed(
        db,
        actor=actor,
        action="message_action_create",
        resource_id=row.id,
        detail=f"建立訊息動作「{row.name}」",
        metadata=_snapshot_meta(row, bindings=[]),
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(row)
    return row


def update_action(
    db: Session,
    *,
    action_id: int,
    payload: MessageActionUpdate,
    actor: User,
    ip_address: str | None = None,
) -> MessageAction:
    row = _get_or_404(db, action_id)
    _require_action_author_or_admin(row, actor)
    previous_sha = row.body_sha256

    data = payload.model_dump(exclude_unset=True)
    if "icon" in data:
        data["icon"] = _validate_icon(data["icon"])
    if "choices" in data:
        data["choices"] = _validate_choices(
            [ChoiceSpec.model_validate(c) if not isinstance(c, ChoiceSpec) else c
             for c in (data["choices"] or [])]
        )
    if "name" in data and data["name"] != row.name:
        clash = (
            db.query(MessageAction)
            .filter(
                MessageAction.name == data["name"],
                MessageAction.id != row.id,
            )
            .first()
        )
        if clash:
            raise HTTPException(status_code=400, detail="名稱已存在")

    new_body = data.get("body", row.body)
    _validate_body(new_body)

    for key, value in data.items():
        setattr(row, key, value)
    if "body" in data:
        row.body_sha256 = body_sha256(row.body)
    row.version = int(row.version) + 1
    row.updated_by_user_id = actor.id
    row.updated_at = datetime.now(timezone.utc)
    db.flush()

    _audit_fail_closed(
        db,
        actor=actor,
        action="message_action_update",
        resource_id=row.id,
        detail=f"更新訊息動作「{row.name}」至 v{row.version}",
        metadata=_snapshot_meta(row, previous_sha=previous_sha),
        ip_address=ip_address,
    )
    db.commit()
    db.refresh(row)
    return row


def delete_action(
    db: Session,
    *,
    action_id: int,
    actor: User,
    ip_address: str | None = None,
) -> None:
    row = _get_or_404(db, action_id)
    _require_action_author_or_admin(row, actor)
    bindings = (
        db.query(MessageActionBinding)
        .filter(MessageActionBinding.action_id == row.id)
        .all()
    )
    meta = _snapshot_meta(row, bindings=_binding_dicts(bindings))
    name = row.name
    rid = row.id
    db.delete(row)
    db.flush()
    _audit_fail_closed(
        db,
        actor=actor,
        action="message_action_delete",
        resource_id=rid,
        detail=f"刪除訊息動作「{name}」",
        metadata=meta,
        ip_address=ip_address,
    )
    db.commit()


def list_actions_admin(db: Session) -> list[MessageAction]:
    return (
        db.query(MessageAction)
        .order_by(MessageAction.id.asc())
        .all()
    )


def list_actions_admin_serialized(
    db: Session, *, actor: User
) -> list[dict]:
    """Management list with per-caller template disclosure.

    Admin-tier always passes the modify short-circuit, so the visibility
    set (every department row + every binding) is never needed and is
    not loaded for those callers.
    """
    rows = list_actions_admin(db)
    if is_admin_tier(actor):
        return [
            serialize_admin(r, actor=actor, db=db, visible_ids=set())
            for r in rows
        ]
    visible_ids = _user_visible_action_ids(db, actor)
    return [
        serialize_admin(
            r, actor=actor, db=db, visible_ids=visible_ids
        )
        for r in rows
    ]


def serialize_admin(
    action: MessageAction,
    *,
    actor: User,
    db: Session,
    visible_ids: set[int] | None = None,
) -> dict:
    """Management list/detail shape.

    Body follows the read rule: pressable (bound/authored and enabled)
    or may modify (author or admin-tier). Callers who can neither press
    nor modify still get ``body=None``. Mutation gates are unchanged.
    """
    body = (
        action.body
        if _may_read_template(
            db, action, actor, visible_ids=visible_ids
        )
        else None
    )
    return {
        "id": action.id,
        "name": action.name,
        "label": action.label,
        "icon": action.icon,
        "body": body,
        "body_sha256": action.body_sha256,
        "choices": action.choices or [],
        "notes": action.notes,
        "version": action.version,
        "is_enabled": action.is_enabled,
        "created_by_user_id": action.created_by_user_id,
        "updated_by_user_id": action.updated_by_user_id,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
    }


# ── Bindings ─────────────────────────────────────────────────────────────────


def list_bindings(
    db: Session,
    action_id: int,
    *,
    actor: User,
) -> list[MessageActionBinding]:
    action = _get_or_404(db, action_id)
    _require_action_author_or_admin(action, actor)
    return (
        db.query(MessageActionBinding)
        .filter(MessageActionBinding.action_id == action_id)
        .order_by(MessageActionBinding.id.asc())
        .all()
    )


def replace_bindings(
    db: Session,
    *,
    action_id: int,
    specs: list[BindingSpec],
    actor: User,
    ip_address: str | None = None,
) -> list[MessageActionBinding]:
    action = _get_or_404(db, action_id)
    _require_action_author_or_admin(action, actor)
    before = _binding_dicts(
        db.query(MessageActionBinding)
        .filter(MessageActionBinding.action_id == action_id)
        .all()
    )

    # Dedupe by unique binding key so a single PUT cannot trip IntegrityError.
    seen_keys: set[tuple] = set()
    deduped: list[BindingSpec] = []
    for spec in specs:
        key = (spec.scope_type, spec.role, spec.department_id, spec.user_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(spec)
    specs = deduped

    for spec in specs:
        if spec.scope_type == "department":
            dept = (
                db.query(Department)
                .filter(Department.id == spec.department_id)
                .first()
            )
            if not dept or not dept.is_active:
                raise HTTPException(
                    status_code=400, detail="部門不存在或已停用"
                )
        if spec.scope_type == "user":
            user = db.query(User).filter(User.id == spec.user_id).first()
            if not user:
                raise HTTPException(status_code=400, detail="使用者不存在")

    db.query(MessageActionBinding).filter(
        MessageActionBinding.action_id == action_id
    ).delete(synchronize_session=False)

    now = datetime.now(timezone.utc)
    created: list[MessageActionBinding] = []
    for spec in specs:
        row = MessageActionBinding(
            action_id=action_id,
            scope_type=spec.scope_type,
            role=spec.role,
            department_id=spec.department_id,
            user_id=spec.user_id,
            created_by=actor.id,
            created_at=now,
        )
        db.add(row)
        created.append(row)
    db.flush()

    after = _binding_dicts(created)
    _audit_fail_closed(
        db,
        actor=actor,
        action="message_action_bindings_replace",
        resource_id=action.id,
        detail=f"替換訊息動作「{action.name}」的綁定",
        metadata={"before": before, "after": after},
        ip_address=ip_address,
    )
    db.commit()
    for row in created:
        db.refresh(row)
    return created


# ── Visibility ───────────────────────────────────────────────────────────────


def _user_visible_action_ids(db: Session, user: User) -> set[int]:
    """A person sees an action when it is bound to them or when they authored it.

    Same rule for every role — no owner/admin bypass. Fail-closed empty
    when neither authorship nor any binding matches.
    """
    visible: set[int] = set()

    authored = (
        db.query(MessageAction.id)
        .filter(MessageAction.created_by_user_id == user.id)
        .all()
    )
    visible |= {r[0] for r in authored}

    role_ids = (
        db.query(MessageActionBinding.action_id)
        .filter(
            MessageActionBinding.scope_type == "role",
            MessageActionBinding.role == user.role,
        )
        .all()
    )
    visible |= {r[0] for r in role_ids}

    user_ids = (
        db.query(MessageActionBinding.action_id)
        .filter(
            MessageActionBinding.scope_type == "user",
            MessageActionBinding.user_id == user.id,
        )
        .all()
    )
    visible |= {r[0] for r in user_ids}

    if user.department_id is not None:
        dept_bindings = (
            db.query(MessageActionBinding)
            .filter(MessageActionBinding.scope_type == "department")
            .all()
        )
        # Parent binding covers descendants iff the binding department is
        # on the caller's ancestor chain (self included). One table load.
        edges = {
            id_: parent_id
            for id_, parent_id in db.query(
                Department.id, Department.parent_id
            ).all()
        }
        ancestors: set[int] = set()
        current: int | None = user.department_id
        seen: set[int] = set()
        while current is not None and current in edges and current not in seen:
            ancestors.add(current)
            seen.add(current)
            current = edges[current]
        for b in dept_bindings:
            if b.department_id is not None and b.department_id in ancestors:
                visible.add(b.action_id)

    return visible


def list_visible(db: Session, user: User) -> list[MessageAction]:
    ids = _user_visible_action_ids(db, user)
    if not ids:
        return []
    return (
        db.query(MessageAction)
        .filter(
            MessageAction.id.in_(ids),
            MessageAction.is_enabled.is_(True),
        )
        .order_by(MessageAction.id.asc())
        .all()
    )


def resolve_for_invoke(db: Session, action_id: int, user: User) -> MessageAction:
    """404 for unknown / disabled / not-visible (no oracle)."""
    row = db.query(MessageAction).filter(MessageAction.id == action_id).first()
    if row is None or not row.is_enabled:
        raise HTTPException(status_code=404, detail="動作不存在")
    visible = _user_visible_action_ids(db, user)
    if row.id not in visible:
        raise HTTPException(status_code=404, detail="動作不存在")
    return row


# ── Rate limit ───────────────────────────────────────────────────────────────


def _check_rate_limit(user_id: int) -> None:
    limit = int(settings.ANILA_ACTION_INVOKE_PER_MIN)
    now = time.monotonic()
    window = 60.0
    stale = [
        uid for uid, (start, _) in _RATE_BUCKETS.items() if now - start >= window
    ]
    for uid in stale:
        del _RATE_BUCKETS[uid]
    start, count = _RATE_BUCKETS.get(user_id, (now, 0))
    if now - start >= window:
        start, count = now, 0
    count += 1
    _RATE_BUCKETS[user_id] = (start, count)
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail="動作呼叫過於頻繁，請稍候再試",
        )


# ── Invoke ───────────────────────────────────────────────────────────────────


def _resolve_choice(
    action: MessageAction,
    choice_id: str | None,
    user_input: str | None,
) -> tuple[str, str | None, dict | None]:
    """Return (choice_prompt, choice_id_used, choice_dict)."""
    choices = action.choices or []
    if not choices:
        return "", None, None

    if choice_id is None:
        if len(choices) == 1 and not choices[0].get("input"):
            c = choices[0]
            return c.get("prompt") or "", c.get("id"), c
        raise HTTPException(
            status_code=400,
            detail="此動作需要選擇一個項目",
        )

    match = next((c for c in choices if c.get("id") == choice_id), None)
    if match is None:
        raise HTTPException(status_code=400, detail="未知的選項")
    if match.get("input") and (user_input is None or user_input == ""):
        raise HTTPException(
            status_code=400,
            detail="此選項需要輸入內容",
        )
    return match.get("prompt") or "", choice_id, match


async def invoke_action(
    db: Session,
    *,
    action_id: int,
    conversation_id: int,
    message_id: int,
    choice_id: str | None,
    user_input: str | None,
    actor: User,
    ip_address: str | None = None,
) -> dict:
    """Gate order per blueprint §4 / NON-NEGOTIABLES.

    Rate limit sits immediately after action resolution so refusal-audit
    paths cannot be flooded; 429 itself leaves no audit row.
    """
    # 1. resolve action visibility
    action = resolve_for_invoke(db, action_id, actor)

    # 2. rate limit (before any refusal audit / substantive gate)
    _check_rate_limit(actor.id)

    # 3. conversation access
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="找不到此對話")
    try:
        _check_access(conv, actor)
    except HTTPException as exc:
        if exc.status_code == 403:
            # Real conversation the caller cannot access — insider signal.
            _audit_invoke_refused(
                db,
                actor=actor,
                action=action,
                conversation_id=conversation_id,
                conversation_level=_conversation_level_value(conv),
                reason="access_denied",
                ip_address=ip_address,
            )
        raise

    # 4. message validation
    msg = (
        db.query(Message)
        .filter(Message.id == message_id)
        .first()
    )
    if msg is None or msg.conversation_id != conversation_id:
        raise HTTPException(
            status_code=400, detail="訊息不屬於此對話"
        )
    if msg.role != "assistant":
        raise HTTPException(
            status_code=400, detail="只能對助理訊息執行動作"
        )

    # 5. classification gate — unknown storage values propagate (500),
    # matching ClassificationLevel.from_storage contract / sibling consumers.
    level = ClassificationLevel.from_storage(conv.classification_level)
    if not outbound_action_allowed(level):
        _audit_invoke_refused(
            db,
            actor=actor,
            action=action,
            conversation_id=conversation_id,
            conversation_level=level.value,
            reason="classification",
            ip_address=ip_address,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"此對話密等為「{level.value}」，不可執行自訂動作"
            ),
        )

    # 6. ANILALM branch exclusion (before any render)
    try:
        _require_branchable(conv)
    except HTTPException as exc:
        if exc.status_code == 409:
            # Accessible real conversation that cannot host a branch.
            _audit_invoke_refused(
                db,
                actor=actor,
                action=action,
                conversation_id=conversation_id,
                conversation_level=level.value,
                reason="not_branchable",
                ip_address=ip_address,
            )
        raise

    # 7. choice validation (400s before input-length 413; blueprint §4 table)
    choice_prompt, resolved_choice_id, _choice = _resolve_choice(
        action, choice_id, user_input
    )
    if user_input is not None and len(user_input) > _MAX_INPUT_CHARS:
        raise HTTPException(status_code=413, detail="輸入內容過長")

    # 8. write-ahead audit (committed BEFORE returning the rendered prompt)
    invocation_id = uuid.uuid4().hex
    content_text = msg.content or ""
    rendered = render_template(
        action.body,
        content=content_text,
        choice=choice_prompt,
        input=user_input or "",
    )
    invoke_meta: dict[str, Any] = {
        "invocation_id": invocation_id,
        "version": action.version,
        "body_sha256": action.body_sha256,
        "choice_id": resolved_choice_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "conversation_level": level.value,
        "rendered_prompt_sha256": body_sha256(rendered),
        "rendered_prompt_length": len(rendered),
    }
    audit_row = log_audit_event(
        db,
        actor=actor,
        action="message_action_invoke",
        resource_type=RESOURCE_TYPE,
        resource_id=action.id,
        detail=f"呼叫訊息動作「{action.name}」",
        metadata=invoke_meta,
        ip_address=ip_address,
        commit=True,
    )
    if audit_row is None:
        raise HTTPException(
            status_code=500,
            detail="稽核紀錄寫入失敗，動作未執行",
        )

    return {
        "invocation_id": invocation_id,
        "action_id": action.id,
        "version": action.version,
        "prompt": rendered,
    }


# ── Export ───────────────────────────────────────────────────────────────────


def export_audit(
    db: Session,
    *,
    actor: User,
    since: datetime | None,
    until: datetime | None,
    limit: int,
    ip_address: str | None = None,
) -> list[dict]:
    """Admin+ NDJSON export; redaction matches ``GET /api/audit-logs``."""
    actions = (
        "message_action_create",
        "message_action_update",
        "message_action_delete",
        "message_action_bindings_replace",
        "message_action_invoke",
        "message_action_invoke_refused",
        "message_action_audit_export",
    )
    q = (
        db.query(AuditLog)
        .filter(AuditLog.action.in_(actions))
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
    )
    if since is not None:
        q = q.filter(AuditLog.created_at >= since)
    if until is not None:
        q = q.filter(AuditLog.created_at <= until)
    rows = q.limit(limit).all()

    _audit_fail_closed(
        db,
        actor=actor,
        action="message_action_audit_export",
        resource_id=None,
        detail=f"匯出訊息動作稽核 {len(rows)} 筆",
        metadata={
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "limit": limit,
            "exported_count": len(rows),
        },
        ip_address=ip_address,
    )
    db.commit()

    out: list[dict] = []
    for log in rows:
        # Same owner/non-owner treatment as the audit listing endpoint.
        item = serialize_audit_log(log, caller=actor, db=db)
        created = item.get("created_at")
        item["created_at"] = created.isoformat() if created else None
        out.append(item)
    return out
