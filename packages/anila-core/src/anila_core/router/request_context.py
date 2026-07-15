"""Trusted request context primitives for the Gate 5 Router runtime.

The HTTP adapter is responsible for authenticating a request and obtaining the
authoritative task/session values from CSP.  This module deliberately accepts
only that server-derived view.  Caller messages can be retained as *untrusted
history*, but they never become routing control data.

The types in this module are intentionally framework-light.  They are frozen
dataclasses so a context can be passed through filtering, decision and policy
checks without a later stage mutating the values that an earlier stage checked.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from anila_contracts import Classification
from anila_contracts.classification import ClassificationLevel
from anila_contracts.contexts import AuthAssurance


_TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/@-")
_MAX_HISTORY_ITEMS = 64
_MAX_HISTORY_TEXT = 16_384
_MAX_HISTORY_SUMMARY = 4_096


def _require_identifier(
    value: object | None, *, prefix: str, allow_positive_int: bool = True
) -> str:
    """Normalize a required server identifier without coercing arbitrary data."""

    if value is None:
        raise ValueError(f"{prefix} 為必要的 server-derived 欄位")
    if isinstance(value, bool):
        raise TypeError(f"{prefix} 不得是 bool")
    if isinstance(value, int):
        if not allow_positive_int or value <= 0:
            raise ValueError(f"{prefix} 必須是正整數")
        return str(value)
    if not isinstance(value, str):
        raise TypeError(f"{prefix} 必須是正整數或安全字串")
    text = value.strip()
    if not text or len(text) > 255:
        raise ValueError(f"{prefix} 不得為空白或過長")
    if text[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        raise ValueError(f"{prefix} 必須以英數字開頭")
    if any(char not in _TOKEN_CHARS for char in text):
        raise ValueError(f"{prefix} 含有不安全字元")
    return text


def _require_positive_int(value: object | None, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} 必須是正整數")
    return value


def _require_token(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必須是字串")
    text = value.strip()
    if not text or len(text) > 128 or any(char not in _TOKEN_CHARS for char in text):
        raise ValueError(f"{field_name} 必須是安全控制 token")
    return text


def _normalise_tokens(values: object | None, *, field_name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise TypeError(f"{field_name} 必須是 token sequence")
    normalised = tuple(_require_token(value, field_name=field_name) for value in values)
    if len(set(normalised)) != len(normalised):
        raise ValueError(f"{field_name} 不允許重複值")
    return normalised


def _parse_classification(value: object | None, *, field_name: str) -> ClassificationLevel:
    if value is None:
        raise ValueError(f"{field_name} 為必要的 server-derived 欄位")
    if isinstance(value, ClassificationLevel):
        return value
    if isinstance(value, str):
        try:
            return ClassificationLevel.from_storage(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} 未知分類，拒絕建立 context") from exc
    raise TypeError(f"{field_name} 必須是 ClassificationLevel 或合法 wire 值")


def _parse_auth_assurance(value: object | None) -> AuthAssurance:
    if value is None:
        raise ValueError("auth_assurance 為必要的 server-derived 欄位")
    if isinstance(value, AuthAssurance):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("auth_assurance 必須是 AuthAssurance 或 object")
    try:
        return AuthAssurance.model_validate(dict(value))
    except Exception as exc:
        raise ValueError("auth_assurance 驗證失敗，拒絕建立 context") from exc


@dataclass(frozen=True)
class ServerCeilings:
    """Server-side execution ceilings.

    Gate 5 R3 intentionally keeps the initial single-agent limit at three
    steps.  A caller may request less, but can never raise either ceiling.
    """

    max_steps: int = 3
    max_timeout_ms: int = 120_000

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or self.max_steps < 1:
            raise ValueError("server max_steps 必須為正整數")
        if isinstance(self.max_timeout_ms, bool) or self.max_timeout_ms < 1:
            raise ValueError("server max_timeout_ms 必須為正整數")
        # This is a hard R3 ceiling, not a user-configurable policy knob.
        object.__setattr__(self, "max_steps", min(int(self.max_steps), 3))
        object.__setattr__(self, "max_timeout_ms", int(self.max_timeout_ms))


@dataclass(frozen=True)
class UntrustedHistoryItem:
    """A caller message retained for context, never for control authority."""

    role: str
    content: str
    trusted: bool = False
    is_control: bool = False

    def __post_init__(self) -> None:
        if self.role not in {"user", "assistant", "system", "tool", "developer"}:
            raise ValueError("history role 不在允許集合")
        if not self.content.strip():
            raise ValueError("history content 不得為空白")
        if len(self.content) > _MAX_HISTORY_TEXT:
            object.__setattr__(self, "content", self.content[:_MAX_HISTORY_TEXT])
        # No caller-originated item is ever promoted to a trusted control item.
        object.__setattr__(self, "trusted", False)
        object.__setattr__(self, "is_control", False)


@dataclass(frozen=True)
class RequestContext:
    """Immutable, server-derived context consumed by the Router runtime."""

    identity: str
    owner_id: str
    session_id: str
    task_id: int
    run_id: int
    source_snapshot_id: int
    trace_id: str
    invocation_id: str
    task_type: str
    classification: ClassificationLevel
    scopes: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    auth_assurance: AuthAssurance
    history: tuple[UntrustedHistoryItem, ...] = ()
    history_summary: str = ""
    ceilings: ServerCeilings = ServerCeilings()
    max_steps: int = 3
    timeout_ms: int = 120_000
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        # Keep direct dataclass construction fail-closed as well; the builder
        # remains the normal trusted boundary, but a caller must not be able to
        # bypass it by manually constructing an unsafe context.
        _require_identifier(self.identity, prefix="identity", allow_positive_int=False)
        _require_identifier(self.owner_id, prefix="owner_id", allow_positive_int=False)
        _require_identifier(self.session_id, prefix="session_id", allow_positive_int=False)
        _require_positive_int(self.task_id, field_name="task_id")
        _require_positive_int(self.run_id, field_name="run_id")
        _require_positive_int(self.source_snapshot_id, field_name="source_snapshot_id")
        _require_identifier(self.trace_id, prefix="trace_id", allow_positive_int=False)
        _require_identifier(self.invocation_id, prefix="invocation_id", allow_positive_int=False)
        _require_token(self.task_type, field_name="task_type")
        if not isinstance(self.classification, ClassificationLevel):
            raise TypeError("classification 必須是 ClassificationLevel")
        if not isinstance(self.auth_assurance, AuthAssurance):
            raise TypeError("auth_assurance 必須是 AuthAssurance")
        if not isinstance(self.ceilings, ServerCeilings):
            raise TypeError("ceilings 必須是 ServerCeilings")
        if (
            isinstance(self.max_steps, bool)
            or not isinstance(self.max_steps, int)
            or self.max_steps < 1
            or self.max_steps > self.ceilings.max_steps
        ):
            raise ValueError("context max_steps 超過 server ceiling")
        if (
            isinstance(self.timeout_ms, bool)
            or not isinstance(self.timeout_ms, int)
            or self.timeout_ms < 1
            or self.timeout_ms > self.ceilings.max_timeout_ms
        ):
            raise ValueError("context timeout_ms 超過 server ceiling")
        if self.created_at is not None and (
            self.created_at.tzinfo is None or self.created_at.utcoffset() is None
        ):
            raise ValueError("created_at 必須帶時區")

    @property
    def user_id(self) -> str:
        """Alias used by CSP-facing consumers."""

        return self.identity

    @property
    def owner(self) -> str:
        return self.owner_id

    @property
    def session(self) -> str:
        return self.session_id

    @property
    def task(self) -> int:
        return self.task_id

    @property
    def run(self) -> int:
        return self.run_id

    @property
    def trace(self) -> str:
        return self.trace_id

    @property
    def invocation(self) -> str:
        return self.invocation_id

    @property
    def effective_classification(self) -> Classification:
        return self.classification

    @property
    def server_max_steps(self) -> int:
        return self.ceilings.max_steps

    @property
    def server_timeout_ms(self) -> int:
        return self.ceilings.max_timeout_ms

    @property
    def classified(self) -> bool:
        return self.classification is not ClassificationLevel.UNCLASSIFIED


class RequestContextBuilder:
    """Build a context from a trusted server-derived request projection.

    ``build`` accepts a mapping for adapter convenience, but values are still
    interpreted as server values.  Caller ``messages``/``history`` are copied
    into :class:`UntrustedHistoryItem`; a caller system message cannot replace
    an internal routing prompt or alter task, scope, classification or limits.
    """

    def __init__(self, *, max_steps: int = 3, max_timeout_ms: int = 120_000) -> None:
        self._ceilings = ServerCeilings(max_steps=max_steps, max_timeout_ms=max_timeout_ms)

    @property
    def ceilings(self) -> ServerCeilings:
        return self._ceilings

    @staticmethod
    def _history(value: object | None) -> tuple[UntrustedHistoryItem, ...]:
        if value is None:
            return ()
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TypeError("history 必須是 message sequence")
        if len(value) > _MAX_HISTORY_ITEMS:
            value = value[-_MAX_HISTORY_ITEMS:]
        result: list[UntrustedHistoryItem] = []
        for item in value:
            if isinstance(item, UntrustedHistoryItem):
                result.append(item)
                continue
            if not isinstance(item, Mapping):
                raise TypeError("每一筆 history 必須是 mapping")
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                raise TypeError("history item 必須包含 role/content 字串")
            result.append(UntrustedHistoryItem(role=role.strip().lower(), content=content))
        return tuple(result)

    def build(self, server_input: Mapping[str, Any] | None = None, **kwargs: Any) -> RequestContext:
        """Create a frozen context, clamping caller-requested limits.

        ``server_input`` is deliberately a plain mapping rather than a FastAPI
        request.  The HTTP adapter must perform authentication and CSP lookup
        before handing it to this function.
        """

        values: dict[str, Any] = dict(server_input or {})
        values.update(kwargs)

        identity = _require_identifier(
            values.get("identity", values.get("user_id")), prefix="identity"
        )
        owner_id = _require_identifier(
            values.get("owner_id", values.get("owner")), prefix="owner_id"
        )
        session_id = _require_identifier(
            values.get("session_id", values.get("session")), prefix="session_id"
        )
        task_id = _require_positive_int(
            values.get("task_id", values.get("task")), field_name="task_id"
        )
        run_id = _require_positive_int(values.get("run_id", values.get("run")), field_name="run_id")
        source_snapshot_id = _require_positive_int(
            values.get("source_snapshot_id", values.get("source_snapshot")),
            field_name="source_snapshot_id",
        )
        trace_id = _require_identifier(
            values.get("trace_id", values.get("trace")), prefix="trace_id"
        )
        invocation_id = _require_identifier(
            values.get("invocation_id", values.get("invocation")), prefix="invocation_id"
        )

        if "task_type" not in values:
            raise ValueError("task_type 為必要的 server-derived 欄位")
        task_type = _require_token(values["task_type"], field_name="task_type")
        if "scopes" not in values:
            raise ValueError("scopes 為必要的 server-derived 欄位")
        scopes = _normalise_tokens(values["scopes"], field_name="scopes")
        required_capabilities = _normalise_tokens(
            values.get("required_capabilities", values.get("capabilities", ())),
            field_name="required_capabilities",
        )

        current = _parse_classification(values.get("classification"), field_name="classification")
        previous_values = values.get(
            "prior_classifications",
            values.get("classification_history", values.get("history_classifications", ())),
        )
        if isinstance(previous_values, (str, bytes)) or not isinstance(previous_values, Sequence):
            raise TypeError("prior_classifications 必須是 sequence")
        prior = tuple(
            _parse_classification(item, field_name="prior_classification")
            for item in previous_values
        )
        # One-way classification latch: only server-derived levels participate.
        classification = ClassificationLevel.max_of((current, *prior))

        history = self._history(values.get("history", values.get("messages")))
        history_summary = values.get("history_summary", values.get("safe_history_summary", ""))
        if not isinstance(history_summary, str):
            raise TypeError("history_summary 必須是字串")
        history_summary = history_summary.strip()
        if len(history_summary) > _MAX_HISTORY_SUMMARY:
            history_summary = history_summary[:_MAX_HISTORY_SUMMARY]
        if any(ord(char) < 0x20 and char not in "\t\n" for char in history_summary):
            raise ValueError("history_summary 含有控制字元")

        server_max_steps = values.get("server_max_steps", self._ceilings.max_steps)
        server_timeout_ms = values.get("server_timeout_ms", self._ceilings.max_timeout_ms)
        if isinstance(server_max_steps, bool) or not isinstance(server_max_steps, int):
            raise TypeError("server_max_steps 必須是整數")
        if isinstance(server_timeout_ms, bool) or not isinstance(server_timeout_ms, int):
            raise TypeError("server_timeout_ms 必須是整數")
        ceilings = ServerCeilings(
            max_steps=min(server_max_steps, self._ceilings.max_steps),
            max_timeout_ms=min(server_timeout_ms, self._ceilings.max_timeout_ms),
        )

        requested_steps = values.get("requested_max_steps", values.get("max_steps"))
        if requested_steps is None:
            effective_steps = ceilings.max_steps
        else:
            if isinstance(requested_steps, bool) or not isinstance(requested_steps, int):
                raise TypeError("requested_max_steps 必須是整數")
            if requested_steps < 1:
                raise ValueError("requested_max_steps 必須為正整數")
            effective_steps = min(requested_steps, ceilings.max_steps)

        requested_timeout = values.get("requested_timeout_ms", values.get("timeout_ms"))
        if requested_timeout is None:
            effective_timeout = ceilings.max_timeout_ms
        else:
            if isinstance(requested_timeout, bool) or not isinstance(requested_timeout, int):
                raise TypeError("requested_timeout_ms 必須是整數")
            if requested_timeout < 1:
                raise ValueError("requested_timeout_ms 必須為正整數")
            effective_timeout = min(requested_timeout, ceilings.max_timeout_ms)

        auth_assurance = _parse_auth_assurance(values.get("auth_assurance", values.get("auth")))
        created_at = values.get("created_at")
        if created_at is not None and not isinstance(created_at, datetime):
            raise TypeError("created_at 必須是 datetime")
        if created_at is not None and (created_at.tzinfo is None or created_at.utcoffset() is None):
            raise ValueError("created_at 必須帶時區")

        return RequestContext(
            identity=identity,
            owner_id=owner_id,
            session_id=session_id,
            task_id=task_id,
            run_id=run_id,
            source_snapshot_id=source_snapshot_id,
            trace_id=trace_id,
            invocation_id=invocation_id,
            task_type=task_type,
            classification=classification,
            scopes=scopes,
            required_capabilities=required_capabilities,
            auth_assurance=auth_assurance,
            history=history,
            history_summary=history_summary,
            ceilings=ceilings,
            max_steps=effective_steps,
            timeout_ms=effective_timeout,
            created_at=created_at,
        )

    __call__ = build


__all__ = [
    "RequestContext",
    "RequestContextBuilder",
    "ServerCeilings",
    "UntrustedHistoryItem",
]
