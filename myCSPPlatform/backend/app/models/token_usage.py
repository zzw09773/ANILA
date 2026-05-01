from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index
from app.database import Base


class TokenUsage(Base):
    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Nullable: JWT / cookie-authenticated SPA calls have no named API key.
    # Dashboards group such rows into a "Web UI" bucket (usage_service).
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=True)
    model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=False)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    request_timestamp = Column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    request_duration_ms = Column(Integer, nullable=True)
    # Authoritative audit fields — populated when client passes headers
    conversation_id = Column(String(128), nullable=True, index=True)
    trace_id = Column(String(128), nullable=True, index=True)
    # Sprint 4 (migration 0020): which kind of model invocation this row
    # represents. Existing chat rows default to 'chat'; ingestion-worker
    # writes 'embedding'; Chunking Evaluator's judge call writes 'judge'.
    # Lets dashboards split chat / embedding / judge spend without
    # having to peek at model_registry.model_type.
    request_type = Column(String(20), nullable=False, default="chat")

    # Sprint 8 X / Phase G — caller attribution. Both NULLable so legacy
    # rows (pre-migration-0027) stay valid and direct UI/SDK traffic can
    # still write rows without forcing an attribution.
    #   * ``caller_agent_id`` — set when this inference happened on
    #     behalf of a registered agent. Either CSP forwarded a request
    #     to that agent, or that agent called CSP back via service
    #     token + trusted-forwarded-user-headers. Lets dashboards group
    #     by ``which agent`` and join through ``agents.base_model_id``
    #     for "by base model" rollups.
    #   * ``caller_client_id`` — set when this inference was triggered
    #     by a non-agent service (Router primary-LLM call,
    #     ingestion-worker callback). Mutually exclusive with
    #     ``caller_agent_id`` in practice.
    caller_agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    caller_client_id = Column(
        Integer, ForeignKey("service_clients.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("idx_usage_user_time", "user_id", "request_timestamp"),
        Index("idx_usage_department_time", "department_id", "request_timestamp"),
        Index("idx_usage_model_time", "model_id", "request_timestamp"),
        Index("idx_usage_timestamp", "request_timestamp"),
        Index("idx_usage_apikey_time", "api_key_id", "request_timestamp"),
        Index("idx_usage_request_type_time", "request_type", "request_timestamp"),
        # Partial indexes — most rows have NULL here so a non-partial
        # index would be wasted bytes. WHERE-clause keeps it tight.
        Index(
            "idx_token_usage_caller_agent",
            "caller_agent_id",
            postgresql_where=caller_agent_id.isnot(None),
        ),
        Index(
            "idx_token_usage_caller_client",
            "caller_client_id",
            postgresql_where=caller_client_id.isnot(None),
        ),
    )
