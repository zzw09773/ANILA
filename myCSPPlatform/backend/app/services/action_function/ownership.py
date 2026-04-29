"""Authorisation for ``POST /api/functions/:slug/run``.

Spec §4.5 splits authz into two paths:

* **chat_message** — full owner check + message belongs to conv +
  message.role == 'assistant' + classified gate (whatever
  ``conversation_service.get_conversation`` enforces today is what
  Functions inherits).

* **test_console** — caller must be the function's author or an admin.
  Function status doesn't matter; ``conversation_id`` / ``message_id``
  may be ``None`` (synthetic context).

Every failure path raises :class:`AuthzError`. The API layer catches
that and surfaces it as ``HTTP 403`` without leaking which step
failed (avoids enumeration).
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    ActionFunction,
    ActionFunctionStatus,
    Message,
    User,
)
from app.services.conversation_service import get_conversation


class AuthzError(Exception):
    """Raised when /run authz fails. Surface as HTTP 403."""


def _is_admin(caller: User) -> bool:
    return getattr(caller, "role", None) == "admin"


def authorize_chat_message_run(
    db: Session,
    *,
    caller: User,
    fn: ActionFunction,
    conv_id: int | None,
    msg_id: int | None,
) -> None:
    """7-step authz for the regular chat-toolbar button click path.

    Order matters — earlier checks raise before we touch DB rows the
    caller might not be allowed to see.
    """
    # 3b. Function must be enabled
    if fn.status != ActionFunctionStatus.ENABLED:
        raise AuthzError("function not enabled")

    # 4b. Caller must have access to the conversation. Reuse the
    # existing gate so any future share / handoff / clearance
    # extensions land here automatically.
    if conv_id is None:
        raise AuthzError("conversation_id required for chat_message run")
    try:
        get_conversation(db, conv_id, caller)
    except HTTPException as exc:
        raise AuthzError(f"conversation access denied: {exc.detail}") from exc

    # 5b/5c. Message must belong to that conversation and be assistant role.
    if msg_id is None:
        raise AuthzError("message_id required for chat_message run")
    msg = db.query(Message).filter_by(id=msg_id).first()
    if msg is None or msg.conversation_id != conv_id:
        raise AuthzError("message not in conversation")
    if msg.role != "assistant":
        raise AuthzError("action button only on assistant messages")

    # 5d. Classified gate is already enforced by get_conversation's
    # internal _check_access. v1 surface = owner+admin only.


def authorize_test_console_run(
    db: Session,
    *,
    caller: User,
    fn: ActionFunction,
) -> None:
    """test_mode=true: caller must be function author or admin.

    Function status is irrelevant — Test Console deliberately allows
    running disabled / draft / quarantined functions so authors can
    iterate on their own code, and admins can validate quarantined
    functions before un-quarantining.
    """
    if _is_admin(caller):
        return
    if fn.author_user_id != caller.id:
        raise AuthzError("test mode: only author or admin can run")
