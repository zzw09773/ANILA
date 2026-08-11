"""Per-conversation attachment token budget helpers (P1.5).

Budget = int(context_window * limits.attachment_budget_ratio).
Effective cost of one attachment = int(token_count * 1.15).

The attachment budget ratio is resolved per request via ``get_setting``
(platform_settings row -> ANILA_* env -> code default). The context fallback,
safety multiplier, and stored-token ceiling are fixed program constants.

Admission is a DERIVED value computed at the moment of use (meter / inject /
API), never persisted. Among rows with extract_status == 'ok' and a
token_count, oldest-first by (created_at, id): accumulate effective cost and
admit while the running total stays within budget. A row that does not fit
is excluded; later smaller rows may still be admitted (existing rule).

extract_status is purely an extraction outcome:
  pending | ok | failed | unsupported | too_large
(too_large = refused to store text past the built-in stored-token ceiling)

DELIBERATE NON-CHANGE: conversation history is NOT subtracted dynamically
from the attachment budget. The 0.7 ratio exists precisely so that
attachments can never occupy more than 70% of the window, leaving the
remaining 30% as the allowance for history, the current question and the
answer. A budget that shrank as the conversation grew would make the
capacity meter a moving target and could retroactively evict an
already-admitted document. Conversations that outgrow the remaining 30%
are the separate 'conversation too long' problem and are not in scope.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

from sqlalchemy.orm import Session, defer

from app.models.attachment import Attachment
from app.models.platform_setting import get_setting
from app.models.model_registry import ModelRegistry


def get_context_window(db: Session, model_name: str | None) -> int:
    """Resolve context window: model_registry value when set, else fixed fallback.

    When ``model_name`` is None or the model has no ``context_window``, this
    falls back to the built-in context window (upload before any turn
    has chosen a model — that fallback is intentional and explicit).
    """
    if model_name:
        row = (
            db.query(ModelRegistry.context_window)
            .filter(ModelRegistry.name == model_name)
            .first()
        )
        if row is not None and row[0] is not None:
            return int(row[0])
    # Explicit fallback: model unknown (pre-turn upload) or registry NULL.
    return 128_000


def attachment_budget_tokens(db: Session, context_window: int) -> int:
    # History is NOT subtracted here — see module docstring (0.7 ratio).
    return int(context_window * float(get_setting(db, "limits.attachment_budget_ratio")))


def max_stored_tokens(db: Session) -> int:
    """Refuse to persist extracted_text beyond this raw-token ceiling.

    Absolute, not budget-derived: extraction runs before any model is known,
    so a budget-derived ceiling would discard text a larger-context model
    could still admit, with re-upload the only recovery. See config.
    """
    return 800_000


def effective_cost(db: Session, token_count: int | None) -> int:
    """Apply the safety multiplier to a raw estimate."""
    if token_count is None or token_count <= 0:
        return 0
    return int(token_count * 1.15)


class _AdmitRow(Protocol):
    id: int
    extract_status: str | None
    token_count: int | None


def admit(
    db: Session,
    attachments: Sequence[_AdmitRow],
    budget: int,
) -> tuple[list[int], list[int]]:
    """Derive which extracted attachments fit the budget (oldest-first).

    Only rows with ``extract_status == 'ok'`` and a non-None ``token_count``
    compete. Caller must pass them already ordered by ``(created_at, id)``
    ascending. Accumulates ``effective_cost``; a row that does not fit is
    excluded, but later rows may still be admitted if they fit the remainder
    (existing deterministic oldest-first rule).
    """
    admitted: list[int] = []
    excluded: list[int] = []
    running = 0
    for att in attachments:
        if (att.extract_status or "") != "ok":
            continue
        if att.token_count is None:
            continue
        cost = effective_cost(db, att.token_count)
        if running + cost <= budget:
            admitted.append(att.id)
            running += cost
        else:
            excluded.append(att.id)
    return admitted, excluded


def get_conversation_attachment_usage(
    db: Session,
    conversation_id: int,
    context_window: int,
) -> dict[str, Any]:
    """Capacity snapshot surfaced to clients (meter / upload response).

    ``used_tokens`` is the sum of admitted effective costs (≤ budget).
    Excess from ok-but-excluded rows is ``over_budget_tokens``.
    Invariant: ``used_tokens + remaining_tokens == budget_tokens``.
    """
    budget = attachment_budget_tokens(db, context_window)
    # 排除 extracted_text(可能到數 MB);容量計算用不到,注入路徑另行只對
    # 已納入的 id 撈文字。用 defer 而非逐一列舉,漏欄位會退化成 lazy load。
    rows = (
        db.query(Attachment)
        .options(defer(Attachment.extracted_text))
        .filter(Attachment.conversation_id == conversation_id)
        .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        .all()
    )
    admitted_ids, excluded_ids = admit(db, rows, budget)
    admitted_set = set(admitted_ids)
    excluded_set = set(excluded_ids)
    used_raw = 0
    excluded_raw = 0
    for att in rows:
        if att.id in admitted_set:
            used_raw += effective_cost(db, att.token_count)
        elif att.id in excluded_set:
            excluded_raw += effective_cost(db, att.token_count)
    used = min(used_raw, budget)
    remaining = budget - used
    percent = 0 if budget <= 0 else min(100, int(round(100.0 * used / budget)))
    return {
        "used_tokens": used,
        "budget_tokens": budget,
        "remaining_tokens": remaining,
        "over_budget_tokens": excluded_raw,
        "percent": percent,
        "attachment_count": len(rows),
        "admitted_ids": admitted_ids,
        "excluded_ids": excluded_ids,
    }


def fits(cost: int, usage: dict[str, Any]) -> bool:
    return cost <= int(usage.get("remaining_tokens", 0))


# Status labels for the prompt notice line (zh-TW).
# Budget exclusion is NOT a stored status — see build_attachment_prompt_block.
_STATUS_REASON = {
    "failed": "解析失敗",
    "unsupported": "不支援的檔案格式",
    "too_large": "抽取文字超過儲存上限",
}

_BUDGET_EXCLUDED_REASON = "超出附件 token 預算"


class AttachmentPromptView(Protocol):
    id: int
    filename: str
    page_count: int | None
    token_count: int | None
    extract_status: str | None
    extract_error: str | None
    extracted_text: str | None


def build_attachment_prompt_block(
    attachments: list[AttachmentPromptView],
    *,
    admitted_ids: set[int] | None = None,
) -> str | None:
    """Build the system-message block for chat injection.

    Returns None when there are no attachments at all (caller must not
    mutate messages). Admitted ok documents contribute full text; ok-but-
    excluded ones are named with the budget reason; pending / failed /
    unsupported / too_large are named so the model does not invent content.
    """
    if not attachments:
        return None

    admitted = admitted_ids if admitted_ids is not None else set()
    lines: list[str] = ["### 本次對話的附件"]
    pending = 0
    omitted: list[tuple[str, str]] = []

    for att in attachments:
        status = att.extract_status or "pending"
        if status == "ok" and att.id in admitted and att.extracted_text:
            header_bits = []
            if att.page_count is not None:
                header_bits.append(f"{att.page_count} 頁")
            tok = att.token_count if att.token_count is not None else "?"
            header_bits.append(f"約 {tok} tokens")
            # Design: 「（N 頁,約 T tokens）」— English comma between clauses.
            lines.append(
                f"#### {att.filename}（{','.join(header_bits)}）"
            )
            lines.append(att.extracted_text)
        elif status == "ok":
            # Extracted successfully but not admitted for this model's budget.
            omitted.append((att.filename, _BUDGET_EXCLUDED_REASON))
        elif status == "pending":
            pending += 1
        elif status in _STATUS_REASON:
            reason = _STATUS_REASON[status]
            # Surface actionable extract_error (e.g. "請另存為 UTF-8") so the
            # user sees more than a generic unsupported/failed label. The
            # stored message is already user-facing — do not invent a new UI.
            if status in {"failed", "too_large", "unsupported"} and att.extract_error:
                reason = f"{reason}：{att.extract_error}"
            omitted.append((att.filename, reason))
        else:
            omitted.append((att.filename, f"狀態={status}"))

    if pending:
        lines.append(
            f"（有 {pending} 份附件仍在處理中,本次回答未包含）"
        )
    if omitted:
        names = "、".join(f"{name}（{reason}）" for name, reason in omitted)
        lines.append(f"（以下附件未納入本次回答：{names}）")

    # Only the header with no body and no notices → still return the header
    # so the model knows the conversation has attachments being tracked.
    return "\n".join(lines)


class AttachmentInjectResult:
    """Status returned by ``_inject_attachments`` for anila_meta.trace."""

    __slots__ = ("status", "label", "detail", "injected_count", "skipped")

    def __init__(
        self,
        *,
        status: str,
        label: str,
        detail: str,
        injected_count: int = 0,
        skipped: bool = False,
    ) -> None:
        self.status = status
        self.label = label
        self.detail = detail
        self.injected_count = injected_count
        self.skipped = skipped

    def to_trace_entry(self) -> dict[str, Any]:
        return {
            "kind": "attachment",
            "label": self.label,
            "detail": self.detail,
            "status": self.status,
        }
