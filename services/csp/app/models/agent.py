from datetime import datetime, timezone
from sqlalchemy import Boolean, JSON, Column, Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


class UserAgentPermission(Base):
    __tablename__ = "user_agent_permissions"

    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )


class ApiKeyAgentPermission(Base):
    __tablename__ = "api_key_agent_permissions"

    api_key_id = Column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )


class AgentCollectionBinding(Base):
    """P4.7 — many-to-many agent ↔ knowledge-base bindings.

    Same composite-PK association shape as ``UserAgentPermission`` /
    ``ApiKeyAgentPermission``. Source of truth for RAG search scope;
    ``Agent.bound_collection_id`` is a derived compatibility mirror
    (min id of the set, or NULL when unbound).
    """

    __tablename__ = "agent_collection_bindings"

    agent_id = Column(
        Integer, ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # doc 05 §3 owner_department_id?(可選;SET NULL 保留 agent 於部門刪除後)。
    owner_department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )
    # Which base LLM this agent relies on (informational, nullable)
    base_model_id = Column(
        Integer, ForeignKey("model_registry.id", ondelete="SET NULL"), nullable=True
    )
    # Derived compatibility mirror of ``agent_collection_bindings`` (P4.7).
    # Source of truth is the junction table; this column holds min(ids) or
    # NULL when unbound so legacy single-value readers keep a stable view.
    # Writers must set bindings via ``set_bound_collection_ids`` — never
    # write this column independently of the set.
    bound_collection_id = Column(
        Integer, ForeignKey("ingestion_collections.id", ondelete="SET NULL"), nullable=True
    )
    endpoint_url = Column(String(500), nullable=False)
    # doc 05 §3 optional manifest / healthcheck URLs(GET /.well-known/anila-agent.json
    # 與 GET /health;nullable —— endpoint_url 之外的可選探點)。
    manifest_url = Column(String(500), nullable=True)
    healthcheck_url = Column(String(500), nullable=True)
    api_version = Column(String(20), nullable=False, default="v1")
    # doc 05 §3/§4 agent semver(manifest.version;§13「尚未存在」欄位逐字名
    # agent_version,對映 manifest 欄位 version)。
    agent_version = Column(String(40), nullable=True)
    # doc 05 §3 runtime_type 5 值(開放 String,contracts.agents.RuntimeType 把關);
    # 現況(有 endpoint_url)backfill = openai_compatible_agent(r1_0004)。
    runtime_type = Column(
        String(40),
        nullable=False,
        default="openai_compatible_agent",
        server_default="openai_compatible_agent",
    )
    description_for_router = Column(Text, nullable=False, default="")
    # doc 05 §3 supported_task_types: string[] / output_schema / allowed_tool_ids: string[]。
    supported_task_types = Column(JSONValue, nullable=True)
    input_schema = Column(JSONValue, nullable=True)
    output_schema = Column(JSONValue, nullable=True)
    allowed_tool_ids = Column(JSONValue, nullable=True)
    capabilities = Column(JSONValue, nullable=True)
    # doc 05 §4 驗過的 manifest 快照(capabilities JSON → formal manifest schema,
    # doc 05 §12 Refactor);manifest_url = 來源、manifest_json = 驗證後留存。
    manifest_json = Column(JSONValue, nullable=True)
    # doc 05 §4 trace.callback_mode(sse_and_post 等;開放 String,契約層把關)。
    trace_callback_mode = Column(String(20), nullable=True)
    # health_status: unknown / healthy / unhealthy
    health_status = Column(String(20), nullable=False, default="unknown")
    # approval_status(OE-1,3 值):registered / approved / disabled。
    # SYSTEM-MAP:註冊 → admin 指派 → 可用;無連線／trace／安全審查三關。
    # r1_0019 將七值殘餘映射至此三態(usable 的 approved 不變)。
    approval_status = Column(
        String(30), nullable=False, default="registered", server_default="registered"
    )
    # 殘餘 audit_level 欄(不再是核准硬閘;預設仍 full_trace 以相容既有列)。
    audit_level = Column(
        String(20), nullable=False, default="full_trace", server_default="full_trace"
    )
    # SYSTEM-MAP §「稽核」:agent 上的列管標記上限(NULL = 無上限);
    # 執行時 effective_task_level <= ceiling 才允許 dispatch。OE-1 KEEP。
    classification_ceiling = Column(String(20), nullable=True)
    # Compatibility read model: derived from default_classification_level
    # (true iff level >= 密 / RESTRICTED — conversation mirror threshold).
    # Writers must set the level and derive this; do not flip the boolean alone.
    requires_encryption = Column(Boolean, nullable=False, default=False, server_default="false")
    # G9 / SYSTEM-MAP §8: developer-chosen default level at register/update.
    # Source of truth for agent_policy latch (proxy._agent_policy_level).
    # Boolean above is the derived compatibility flag (level >= 密).
    default_classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    approved_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Sprint 8 X / Phase A — bootstrap-then-provision flow.
    # Admin issues a single-use ``bsk-`` token via
    # ``POST /api/agents/{id}/issue-bootstrap``; agent then calls
    # ``POST /api/agents/{id}/bootstrap`` to exchange it for a long-lived
    # service token written to ``agent_credentials``. Atomic CAS on
    # ``bootstrap_token_consumed_at`` is what stops a leaked bsk- token
    # from being replayed.
    bootstrap_token_hash = Column(String(64), nullable=True)
    bootstrap_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    bootstrap_token_consumed_at = Column(DateTime(timezone=True), nullable=True)
    bootstrap_token_issued_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Sprint 13 PR A3 — admin-editable per-agent runtime knobs that the
    # agent process polls every 30 s (PR A4). Shape is open so admins
    # can extend it without DB migrations; the agent-side parser
    # tolerates unknown keys. Common shape:
    #
    #   {
    #     "tool_permissions": {
    #       "allow_list": ["*"],
    #       "deny_list": ["exec_bash"],
    #       "ask_tools": ["exec_python"],
    #     },
    #     "workspace": {
    #       "max_bytes": 10485760,
    #       "allow_network": false,
    #       "allowed_mounts": ["/data/agent-foo"]
    #     },
    #     "guardrails": {
    #       "input": [{"kind": "regex_block", "pattern": "sk-\\w+"}],
    #       "output": [{"kind": "max_length", "max_chars": 4096}]
    #     }
    #   }
    #
    # NULL means "agent uses its hard-coded defaults"; an explicit
    # ``{}`` means "admin set empty" (e.g. clear all guardrails).
    runtime_config = Column(JSONValue, nullable=True)

    owner = relationship("User", foreign_keys=[owner_user_id], backref="owned_agents")
    approver = relationship("User", foreign_keys=[approved_by])
    base_model = relationship("ModelRegistry", foreign_keys=[base_model_id])
    allowed_users = relationship(
        "User",
        secondary="user_agent_permissions",
        backref="allowed_agents",
        lazy="select",
    )
    collection_bindings = relationship(
        "AgentCollectionBinding",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
