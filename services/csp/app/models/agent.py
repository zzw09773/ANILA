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
    # RAG agents: the single collection this agent's csk- is allowed to search
    # (S-Q1, least-privilege). NULL = non-RAG agent (no collection search at
    # all). The agent acts as its owner but is hard-scoped to this one id.
    bound_collection_id = Column(
        Integer, ForeignKey("ingestion_collections.id", ondelete="SET NULL"), nullable=True
    )
    endpoint_url = Column(String(500), nullable=False)
    # doc 05 §3 optional manifest / healthcheck URLs(GET /.well-known/anila-agent.json
    # 與 GET /health;nullable —— endpoint_url 之外的可選探點)。
    manifest_url = Column(String(500), nullable=True)
    healthcheck_url = Column(String(500), nullable=True)
    api_version = Column(String(20), nullable=False, default="v1")
    # Explicit lifecycle gate.  ``approval_status`` is a governance workflow
    # state, not a replacement for an operator's active/inactive switch.
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
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
    # Last successful or failed probe timestamp.  ``health_status`` without a
    # fresh timestamp is not evidence of readiness (fail-closed).
    health_checked_at = Column(DateTime, nullable=True)
    # approval_status(doc 05 §3,7 值):draft / pending_connection_test /
    # pending_trace_test / pending_security_review / approved / rejected / disabled。
    # 現況三值由 r1_0004 backfill(pending → pending_connection_test)。註冊落地
    # 預設 = pending_connection_test(現況 pending 的七值等價,第一關 = 連線測試)。
    approval_status = Column(
        String(30), nullable=False, default="pending_connection_test"
    )
    # doc 05 §2 v1 policy:approved Agent 必為 full_trace(approval blocker)。
    audit_level = Column(
        String(20), nullable=False, default="full_trace", server_default="full_trace"
    )
    # Gate 2 registry default is explicit 無機密; NULL remains legal for the
    # developer UI 「未設定（不可派工）」. Runtime admission fail-closes on a
    # missing ceiling — null is never an unbounded privilege grant.
    classification_ceiling = Column(
        String(20), nullable=True, default="無機密", server_default="無機密"
    )
    # doc 05 §6 Full Trace 是 approval blocker:trace-test 全過才落章。
    # trace_test_passed_at 非空 + approval_status=pending_security_review 才可 approve。
    trace_test_passed_at = Column(DateTime, nullable=True)
    trace_test_report = Column(JSONValue, nullable=True)
    # Deterministic identity of the stored canonical manifest.  The revision
    # is a CSP ``sha256:<content-hash>`` identifier, not the Agent-authored
    # semantic ``manifest.version``.  These fields are populated only after
    # CSP parses the manifest; old rows stay null and therefore cannot become
    # ready by migration backfill.
    manifest_sha256 = Column(String(64), nullable=True)
    manifest_revision = Column(String(128), nullable=True)
    # Fingerprint of every governance input used by the trace-test evidence:
    # manifest, endpoint, base model and classification/trace posture.
    trace_test_governance_fingerprint = Column(String(64), nullable=True)
    # When true, runtime must treat every conversation routed to this agent as
    # classified / encrypted. Set by admin in the control panel.
    requires_encryption = Column(Boolean, nullable=False, default=False, server_default="false")
    # doc 08 §3 migration bridge(Slice 3a):requires_encryption=true 的
    # agent backfill 為 機密(migration floor,最終等級以人工盤點為準,
    # doc 08 §15)。task.level 傳遞公式的 selected_agent.default_level
    # 來源(doc 08 §4);boolean 欄位保留為 compatibility read model。
    default_classification_level = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    approved_by = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Sprint 8 X / Phase A — bootstrap-then-provision flow.
    # Admin issues a single-use ``bsk-`` token via
    # ``POST /api/agents/{id}/issue-bootstrap``; agent then calls
    # ``POST /api/agents/{id}/bootstrap`` to exchange it for a long-lived
    # service token written to ``agent_credentials``. Atomic CAS on
    # ``bootstrap_token_consumed_at`` is what stops a leaked bsk- token
    # from being replayed.
    bootstrap_token_hash = Column(String(64), nullable=True)
    bootstrap_token_expires_at = Column(DateTime, nullable=True)
    bootstrap_token_consumed_at = Column(DateTime, nullable=True)
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
