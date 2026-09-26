from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, Float, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON
from app.database import Base

# Postgres → JSONB; SQLite create_all (pytest) → plain JSON.
_JSON_LIST = JSON().with_variant(JSONB(), "postgresql")


class ModelRegistry(Base):
    __tablename__ = "model_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Exact UNIQUE(name) is case-sensitive on PostgreSQL. Case-folded
    # uniqueness is uq_model_registry_name_lower (r1_0043) below —
    # nvidia/NV-embed-V2 and nvidia/nv-embed-v2 must not both exist.
    name = Column(String(200), unique=True, nullable=False, index=True)  # e.g. "aia/asrd"
    display_name = Column(String(200), nullable=False)
    model_type = Column(String(20), nullable=False)  # 'llm' / 'vlm' / 'embedding' / 'agent' / 'image' / 'asr'
    endpoint_url = Column(String(500), nullable=False)
    api_version = Column(String(10), default="v1")  # 'v1' / 'v2'
    # Slice 6a (doc 04 §2): 'openai_compatible' / 'custom_adapter'. formalize 既
    # 有全 openai-compatible 上游的隱含契約;custom adapter 之後才落地。
    protocol = Column(
        String(30), nullable=False, default="openai_compatible",
        server_default="openai_compatible",
    )
    is_active = Column(Boolean, default=True)
    is_router_primary = Column(Boolean, nullable=False, default=False)
    # Distinct from is_router_primary (campus default only).
    router_enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    # 舊的模型列標記。語音解碼位址改由 external_services（治理中心「外部服務」）
    # 決定，asr-gateway 不再讀這一欄。
    is_asr_primary = Column(Boolean, nullable=False, default=False)
    # 主簡報模型：anila-studio 寫簡報與做視覺 QA 用的 LLM，由管理員在模型頁指定。
    # 同一時間最多一筆為 true（partial unique index，見 r1_0037）。
    is_slides_primary = Column(Boolean, nullable=False, default=False)
    # P4.8: at most one designated platform embedding model (partial unique
    # index). embedding_native_dim is measured by calling the model at
    # designation time — never a configured guess.
    is_platform_embedding = Column(Boolean, nullable=False, default=False)
    embedding_native_dim = Column(Integer, nullable=True)
    # Slice 6a (doc 04 §9 / doc 01 §32 拍板五態):
    # unknown / healthy / degraded / unhealthy / disabled。舊三值
    # (online/connecting/offline) 由 r1_0005 就地遷移;'disabled' 由讀取端
    # 依 is_active 呈現(見 health_checker.normalize_health_status)。
    health_status = Column(String(20), default="unknown")
    health_checked_at = Column(DateTime(timezone=True), nullable=True)
    # Slice 6a (doc 04 §3): per-model API key 的 enc::v1:: envelope(與 csk- /
    # ingestion 憑證同一套 credential_crypto)。NULL = 退回全域
    # MODEL_GATEWAY_API_KEY(MVP fallback)。永不隨 API 回傳明文,GET 只露
    # ``has_api_key: bool``。
    api_key_secret_ref = Column(Text, nullable=True)
    # Slice 6a (doc 04 §5): 分類上限(四級字串);NULL = 不設限。出向呼叫前的
    # ceiling 檢查依此判 allow/deny(app/services/proxy/ceiling.py)。
    classification_ceiling = Column(String(20), nullable=True)
    # Slice 6a (doc 04 §2): supports_* 能力宣告。
    supports_streaming = Column(Boolean, nullable=False, default=True)
    supports_json_schema = Column(Boolean, nullable=False, default=False)
    supports_tools = Column(Boolean, nullable=False, default=False)
    # Migration 0033: rows whose endpoint lives on the anila-models-net
    # cross-stack docker network (e.g. ``http://gemma4:8000/v1``). Hint for
    # _build_response sentinel + ModelsView lock indicator. Validation /
    # network isolation is enforced elsewhere (SSRF guard + compose layout).
    is_internal = Column(Boolean, nullable=False, default=False)
    description = Column(Text, nullable=True)
    context_window = Column(Integer, nullable=True)
    # Per-model thinking / sampling (r1_0038). NULL = upstream / platform default.
    # thinking_effort: off | low | medium | high | xhigh | max | default
    thinking_effort = Column(String(20), nullable=True)
    # Probed vendor levels this endpoint accepts, e.g. ["none","low","medium","xhigh"].
    # NULL = not probed (register-time discover failed or never ran).
    thinking_levels_supported = Column(_JSON_LIST, nullable=True)
    # Admin lock: when False, conversation / per-turn tiers are ignored.
    thinking_user_selectable = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    temperature = Column(Float, nullable=True)
    top_p = Column(Float, nullable=True)
    presence_penalty = Column(Float, nullable=True)
    max_tokens = Column(Integer, nullable=True)

    # Slice 6a (doc 04 §2): owner department（SET NULL）。
    owner_department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Agent -> base model relationship
    base_model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=True)
    base_model = relationship("ModelRegistry", remote_side=[id], backref="derived_agents")

    # P4.6b follow-up: who registered this row. Designated endpoint authors
    # list/fetch their own creations via authorship — not via the
    # inference-permission table (which an admin rewrite would wipe).
    created_by_user_id = Column(
        Integer,
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_model_registry_created_by_user",
        ),
        nullable=True,
    )

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index(
            "uq_model_registry_name_lower",
            func.lower(name),
            unique=True,
        ),
    )
