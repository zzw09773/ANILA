"""OW-3 message-action CRUD, visibility, invoke
(docs/plans/ow3-message-actions-blueprint.md §Q1–Q3 / §Q9).

Authoring audit = fail-closed (commit=False + return-check → None ⇒ 500 +
rollback, same transaction; P1.4 idiom). Invoke audit = write-ahead
(committed BEFORE execution). exec_result = fail-soft after.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
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
    ActionKind,
    BindingSpec,
    ChoiceSpec,
    MessageActionCreate,
    MessageActionUpdate,
    ResultMode,
)
from app.services import message_action_exec as exec_mod
from app.services.audit_service import log_audit_event, parse_metadata
from app.services.auth_service import is_owner
from app.services.conversation_service import (
    _check_access,
    _require_branchable,
)

logger = logging.getLogger(__name__)

RESOURCE_TYPE = "message_action"
BODY_REDACTED = "<owner-only>"
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


def _validate_kind(kind: str) -> str:
    allowed = {k.value for k in ActionKind}
    if kind not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"未知的動作類型 '{kind}'"
                f"（可用：{', '.join(sorted(allowed))}）"
            ),
        )
    return kind


def _validate_result_mode(mode: str) -> str:
    allowed = {m.value for m in ResultMode}
    if mode not in allowed:
        raise HTTPException(
            status_code=400,
            detail=(
                f"未知的結果模式 '{mode}'"
                f"（可用：{', '.join(sorted(allowed))}）"
            ),
        )
    return mode


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


def _validate_body(body: str, kind: str) -> None:
    max_chars = int(settings.ANILA_ACTION_MAX_BODY_CHARS)
    if len(body) > max_chars:
        raise HTTPException(status_code=413, detail="動作內容過大")
    if kind == ActionKind.EXEC.value:
        exec_mod.validate_source(body)


def _get_or_404(db: Session, action_id: int) -> MessageAction:
    row = db.query(MessageAction).filter(MessageAction.id == action_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="動作不存在")
    return row


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
        "kind": action.kind,
        "result_mode": action.result_mode,
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
    kind = _validate_kind(payload.kind)
    result_mode = _validate_result_mode(payload.result_mode)
    icon = _validate_icon(payload.icon)
    choices = _validate_choices(payload.choices)
    _validate_body(payload.body, kind)

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
        kind=kind,
        result_mode=result_mode,
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
    previous_sha = row.body_sha256

    data = payload.model_dump(exclude_unset=True)
    if "kind" in data:
        data["kind"] = _validate_kind(data["kind"])
    if "result_mode" in data:
        data["result_mode"] = _validate_result_mode(data["result_mode"])
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

    new_kind = data.get("kind", row.kind)
    new_body = data.get("body", row.body)
    _validate_body(new_body, new_kind)

    for key, value in data.items():
        setattr(row, key, value)
    if "body" in data:
        row.body_sha256 = body_sha256(row.body)
    # Version bumps on every update — always drop compiled entries so the
    # cache cannot accumulate stale (action_id, version) keys.
    exec_mod.invalidate_cache(row.id)
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
    bindings = (
        db.query(MessageActionBinding)
        .filter(MessageActionBinding.action_id == row.id)
        .all()
    )
    meta = _snapshot_meta(row, bindings=_binding_dicts(bindings))
    name = row.name
    rid = row.id
    exec_mod.invalidate_cache(rid)
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


def serialize_admin(action: MessageAction, *, caller: User) -> dict:
    body = action.body if is_owner(caller) else BODY_REDACTED
    return {
        "id": action.id,
        "name": action.name,
        "label": action.label,
        "icon": action.icon,
        "kind": action.kind,
        "result_mode": action.result_mode,
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


def list_bindings(db: Session, action_id: int) -> list[MessageActionBinding]:
    _get_or_404(db, action_id)
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
    """Union of role / department-subtree / user bindings. Fail-closed empty."""
    if is_owner(user):
        rows = db.query(MessageAction.id).all()
        return {r[0] for r in rows}

    visible: set[int] = set()

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
    q = (
        db.query(MessageAction)
        .filter(
            MessageAction.id.in_(ids),
            MessageAction.is_enabled.is_(True),
        )
        .order_by(MessageAction.id.asc())
    )
    rows = q.all()
    if not settings.ANILA_ENABLE_ACTION_EXEC:
        rows = [r for r in rows if r.kind != ActionKind.EXEC.value]
    return rows


def resolve_for_invoke(db: Session, action_id: int, user: User) -> MessageAction:
    """404 for unknown / disabled / not-visible / exec-flag-off (no oracle)."""
    row = db.query(MessageAction).filter(MessageAction.id == action_id).first()
    if row is None or not row.is_enabled:
        raise HTTPException(status_code=404, detail="動作不存在")
    if (
        row.kind == ActionKind.EXEC.value
        and not settings.ANILA_ENABLE_ACTION_EXEC
    ):
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
    """Gate order per blueprint §4 / NON-NEGOTIABLES."""
    # 1. resolve action visibility
    action = resolve_for_invoke(db, action_id, actor)

    # 2. conversation access
    conv = (
        db.query(Conversation)
        .filter(Conversation.id == conversation_id)
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="找不到此對話")
    _check_access(conv, actor)

    # 3. message validation
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

    # 4. classification gate — unknown storage values propagate (500),
    # matching ClassificationLevel.from_storage contract / sibling consumers.
    level = ClassificationLevel.from_storage(conv.classification_level)
    if not outbound_action_allowed(level):
        raise HTTPException(
            status_code=403,
            detail=(
                f"此對話密等為「{level.value}」，不可執行自訂動作"
            ),
        )

    # 5. ANILALM branch exclusion (before any execution/render)
    _require_branchable(conv)

    # 6. choice validation (400s before input-length 413; blueprint §4 table)
    choice_prompt, resolved_choice_id, _choice = _resolve_choice(
        action, choice_id, user_input
    )
    if user_input is not None and len(user_input) > _MAX_INPUT_CHARS:
        raise HTTPException(status_code=413, detail="輸入內容過長")

    # 7. rate limit
    _check_rate_limit(actor.id)

    # 8. write-ahead audit (committed BEFORE execution)
    invocation_id = uuid.uuid4().hex
    content_text = msg.content or ""
    invoke_meta: dict[str, Any] = {
        "invocation_id": invocation_id,
        "version": action.version,
        "body_sha256": action.body_sha256,
        "choice_id": resolved_choice_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "conversation_level": level.value,
    }
    rendered_for_audit: str | None = None
    if action.kind == ActionKind.DECLARATIVE.value:
        rendered_for_audit = render_template(
            action.body,
            content=content_text,
            choice=choice_prompt,
            input=user_input or "",
        )
        invoke_meta["rendered_prompt_sha256"] = body_sha256(rendered_for_audit)
        invoke_meta["rendered_prompt_length"] = len(rendered_for_audit)
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

    # 9. execute / render
    if action.kind == ActionKind.DECLARATIVE.value:
        return {
            "invocation_id": invocation_id,
            "action_id": action.id,
            "version": action.version,
            "kind": action.kind,
            "outcome": "prompt",
            "prompt": rendered_for_audit,
            "output": None,
            "truncated": False,
            "duration_ms": None,
        }

    # exec path
    ctx = {
        "message": content_text,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "choice": choice_prompt,
        "input": user_input or "",
        "user": {
            "id": actor.id,
            "username": actor.username,
            "department_id": actor.department_id,
        },
    }
    started = time.monotonic()
    truncated = False
    output = ""
    duration_ms = 0
    error_type: str | None = None
    tb: str | None = None
    try:
        output, truncated, duration_ms = await exec_mod.run_action(
            action_id=action.id,
            version=action.version,
            source=action.body,
            ctx=ctx,
        )
    except exec_mod.NonStrReturnError:
        error_type = "non_str_return"
        _log_exec_result_failsoft(
            db,
            actor=actor,
            action=action,
            invocation_id=invocation_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_chars=0,
            truncated=False,
            error_type=error_type,
            traceback_text=None,
            status="failure",
            ip_address=ip_address,
        )
        raise HTTPException(
            status_code=502,
            detail="動作回傳值必須是字串",
        )
    except HTTPException as exc:
        if exc.status_code == 504:
            error_type = "timeout"
            tb = None
            _log_exec_result_failsoft(
                db,
                actor=actor,
                action=action,
                invocation_id=invocation_id,
                duration_ms=int((time.monotonic() - started) * 1000),
                output_chars=0,
                truncated=False,
                error_type=error_type,
                traceback_text=None,
                status="failure",
                ip_address=ip_address,
            )
            raise
        if exc.status_code == 503:
            raise
        # Unexpected HTTPException from runner — re-raise
        raise
    except BaseException as exc:
        # Catch SystemExit / KeyboardInterrupt etc. after write-ahead.
        # For normal Exception → 502; BaseException subclasses re-raise
        # after recording so test 27 (SystemExit) still has invoke row.
        error_type = type(exc).__name__
        tb = exec_mod.format_traceback()
        _log_exec_result_failsoft(
            db,
            actor=actor,
            action=action,
            invocation_id=invocation_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_chars=0,
            truncated=False,
            error_type=error_type,
            traceback_text=tb,
            status="failure",
            ip_address=ip_address,
        )
        if isinstance(exc, Exception):
            raise HTTPException(
                status_code=502,
                detail=(
                    f"動作執行失敗（代號 {invocation_id}），"
                    "請聯繫平台管理員"
                ),
            ) from exc
        raise

    _log_exec_result_failsoft(
        db,
        actor=actor,
        action=action,
        invocation_id=invocation_id,
        duration_ms=duration_ms,
        output_chars=len(output),
        truncated=truncated,
        error_type=None,
        traceback_text=None,
        status="success",
        ip_address=ip_address,
    )

    if action.result_mode == ResultMode.DIRECT.value:
        return {
            "invocation_id": invocation_id,
            "action_id": action.id,
            "version": action.version,
            "kind": action.kind,
            "outcome": "text",
            "prompt": None,
            "output": output,
            "truncated": truncated,
            "duration_ms": duration_ms,
        }
    return {
        "invocation_id": invocation_id,
        "action_id": action.id,
        "version": action.version,
        "kind": action.kind,
        "outcome": "prompt",
        "prompt": output,
        "output": None,
        "truncated": truncated,
        "duration_ms": duration_ms,
    }


def _log_exec_result_failsoft(
    db: Session,
    *,
    actor: User,
    action: MessageAction,
    invocation_id: str,
    duration_ms: int,
    output_chars: int,
    truncated: bool,
    error_type: str | None,
    traceback_text: str | None,
    status: str,
    ip_address: str | None,
) -> None:
    meta = {
        "invocation_id": invocation_id,
        "duration_ms": duration_ms,
        "output_chars": output_chars,
        "truncated": truncated,
        "error_type": error_type,
        "traceback": traceback_text,
        "version": action.version,
        "body_sha256": action.body_sha256,
    }
    try:
        row = log_audit_event(
            db,
            actor=actor,
            action="message_action_exec_result",
            resource_type=RESOURCE_TYPE,
            resource_id=action.id,
            status=status,
            detail=f"訊息動作「{action.name}」執行結果",
            metadata=meta,
            ip_address=ip_address,
            commit=True,
        )
        if row is None:
            logger.exception(
                "message_action_exec_result audit returned None "
                "invocation_id=%s",
                invocation_id,
            )
    except Exception:
        logger.exception(
            "message_action_exec_result audit failed "
            "invocation_id=%s",
            invocation_id,
        )


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
    """Owner-only NDJSON export of message-action audit rows + export audit."""
    actions = (
        "message_action_create",
        "message_action_update",
        "message_action_delete",
        "message_action_bindings_replace",
        "message_action_invoke",
        "message_action_exec_result",
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
        out.append(
            {
                "id": log.id,
                "actor_user_id": log.actor_user_id,
                "actor_username": log.actor_username,
                "action": log.action,
                "resource_type": log.resource_type,
                "resource_id": log.resource_id,
                "status": log.status,
                "detail": log.detail,
                "ip_address": log.ip_address,
                "metadata": parse_metadata(log.metadata_json),
                "created_at": (
                    log.created_at.isoformat() if log.created_at else None
                ),
            }
        )
    return out
