"""SQLAlchemy ORM models for ANILA Functions v1.

Five tables introduced by migration 0025 to support the OpenWebUI-style
"Action functions" feature (assistant-message-bound dev-authored buttons):

* :class:`ActionFunction` — main metadata row per Function
* :class:`ActionFunctionVersion` — append-only code history (DB trigger
  rejects UPDATE/DELETE so audit trail stays immutable)
* :class:`ActionFunctionValves` — admin-set parameters, AES-256-GCM at
  rest (see ``app.services.action_function.valves_crypto``)
* :class:`ActionFunctionRun` — every Action button click + Test Console
  run; events_json is redacted before commit (see ``redaction.py``)
* :class:`ActionFunctionReport` — abuse reports filed by users

The ``status`` enum has four values: ``draft``, ``enabled``, ``disabled``,
``quarantined``. Quarantine is the admin "abuse suspected" state — code
becomes invisible to non-author developers (see spec §3.6 / §7.1 +
``api/action_function/crud.py:_can_view_code``).

``ActionFunction.latest_version_id`` is a denormalized cache of the most
recent version's ``id`` and is **not** an FK (deliberately broken to
avoid a circular FK with :class:`ActionFunctionVersion` — see migration
0025 docstring).
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import relationship

from app.database import Base


# ── Enums ───────────────────────────────────────────────────────────────


class ActionFunctionStatus(str, enum.Enum):
    DRAFT = "draft"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class ActionFunctionRunContext(str, enum.Enum):
    CHAT_MESSAGE = "chat_message"
    TEST_CONSOLE = "test_console"


class ActionFunctionRunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


class ActionFunctionReportStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    DISMISSED = "dismissed"
    ACTIONED = "actioned"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Tables ──────────────────────────────────────────────────────────────


class ActionFunction(Base):
    __tablename__ = "action_functions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    slug = Column(Text, unique=True, nullable=False, index=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    icon_data_url = Column(Text, nullable=True)
    author_user_id = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    status = Column(
        Enum(ActionFunctionStatus, name="action_function_status"),
        nullable=False,
        default=ActionFunctionStatus.DRAFT,
        server_default="draft",
        index=True,
    )
    disabled_reason = Column(Text, nullable=True)
    # No FK on latest_version_id — denormalized cache, breaks circular FK.
    # Read paths LEFT JOIN versions and treat missing as "no version yet".
    latest_version_id = Column(BigInteger, nullable=True)
    forked_from_id = Column(
        BigInteger,
        ForeignKey("action_functions.id"),
        nullable=True,
    )
    tags = Column(ARRAY(Text), nullable=False, default=list, server_default="{}")
    created_at = Column(DateTime(timezone=True), default=_utcnow, server_default="now()")
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default="now()",
    )

    versions = relationship(
        "ActionFunctionVersion",
        back_populates="function",
        cascade="all, delete-orphan",
        order_by="ActionFunctionVersion.version_no",
    )
    valves_row = relationship(
        "ActionFunctionValves",
        back_populates="function",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ActionFunctionVersion(Base):
    __tablename__ = "action_function_versions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    function_id = Column(
        BigInteger,
        ForeignKey("action_functions.id"),
        nullable=False,
    )
    version_no = Column(Integer, nullable=False)
    code = Column(Text, nullable=False)
    metadata_json = Column(JSONB, nullable=False, default=dict, server_default="{}")
    actions_meta_json = Column(JSONB, nullable=False, default=list, server_default="[]")
    valves_schema_json = Column(JSONB, nullable=False, default=dict, server_default="{}")
    editor_user_id = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
    )
    commit_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, server_default="now()")

    function = relationship("ActionFunction", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("function_id", "version_no", name="uq_function_version_no"),
    )


class ActionFunctionValves(Base):
    __tablename__ = "action_function_valves"

    function_id = Column(
        BigInteger,
        ForeignKey("action_functions.id"),
        primary_key=True,
    )
    values_encrypted = Column(LargeBinary, nullable=False)
    nonce = Column(LargeBinary, nullable=False)
    key_version = Column(Integer, nullable=False, default=1, server_default="1")
    updated_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default="now()",
    )

    function = relationship("ActionFunction", back_populates="valves_row")


class ActionFunctionRun(Base):
    __tablename__ = "action_function_runs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    function_id = Column(
        BigInteger,
        ForeignKey("action_functions.id"),
        nullable=False,
    )
    version_no = Column(Integer, nullable=False)
    action_id = Column(Text, nullable=False)
    triggered_by_user_id = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
    )
    context_type = Column(
        Enum(ActionFunctionRunContext, name="action_function_run_context"),
        nullable=False,
    )
    conversation_id = Column(
        BigInteger,
        ForeignKey("conversations.id"),
        nullable=True,
    )
    message_id = Column(
        BigInteger,
        ForeignKey("messages.id"),
        nullable=True,
    )
    request_payload_json = Column(JSONB, nullable=False, default=dict, server_default="{}")
    status = Column(
        Enum(ActionFunctionRunStatus, name="action_function_run_status"),
        nullable=False,
    )
    error_message = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    events_json = Column(JSONB, nullable=False, default=list, server_default="[]")
    started_at = Column(DateTime(timezone=True), default=_utcnow, server_default="now()")
    ended_at = Column(DateTime(timezone=True), nullable=True)


class ActionFunctionReport(Base):
    __tablename__ = "action_function_reports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    function_id = Column(
        BigInteger,
        ForeignKey("action_functions.id"),
        nullable=False,
    )
    reporter_user_id = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=False,
    )
    reason = Column(Text, nullable=False)
    status = Column(
        Enum(ActionFunctionReportStatus, name="action_function_report_status"),
        nullable=False,
        default=ActionFunctionReportStatus.OPEN,
        server_default="open",
    )
    acknowledged_by = Column(
        BigInteger,
        ForeignKey("users.id"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), default=_utcnow, server_default="now()")
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        server_default="now()",
    )
