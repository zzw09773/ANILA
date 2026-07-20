from datetime import datetime, timezone
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base


# PostgreSQL stores provider snapshots as JSONB; SQLite unit tests use the
# portable JSON type.  API writes always use TransportTarget.to_dict().
JSONValue = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)


class ModelRegistry(Base):
    __tablename__ = "model_registry"
    __table_args__ = (
        CheckConstraint(
            "provider_locality IN ('external_governed', 'internal_isolated', 'internal_shim', 'unclassified')",
            name="ck_model_registry_provider_locality",
        ),
        CheckConstraint(
            "upstream_provider_locality IS NULL OR upstream_provider_locality IN ('external_governed', 'internal_isolated', 'internal_shim', 'unclassified')",
            name="ck_model_registry_upstream_provider_locality",
        ),
        # Classified rows must carry one complete, immutable provider
        # snapshot.  ``unclassified`` remains intentionally permissive for
        # historical rows whose provenance predates Gate 5.
        CheckConstraint(
            "provider_locality != 'external_governed' OR ("
            "transport_target IS NOT NULL "
            "AND CAST(transport_target AS TEXT) != 'null' "
            "AND transport_target_sha256 IS NOT NULL "
            "AND model_registry_revision IS NOT NULL "
            "AND egress_policy_id IS NOT NULL "
            "AND upstream_provider_locality IS NULL "
            "AND upstream_transport_target IS NULL "
            "AND upstream_transport_target_sha256 IS NULL "
            "AND upstream_egress_policy_id IS NULL"
            ")",
            name="ck_model_registry_external_snapshot",
        ),
        CheckConstraint(
            "provider_locality != 'internal_isolated' OR ("
            "transport_target IS NOT NULL "
            "AND CAST(transport_target AS TEXT) != 'null' "
            "AND transport_target_sha256 IS NOT NULL "
            "AND model_registry_revision IS NOT NULL "
            "AND upstream_provider_locality IS NULL "
            "AND upstream_transport_target IS NULL "
            "AND upstream_transport_target_sha256 IS NULL "
            "AND egress_policy_id IS NULL "
            "AND upstream_egress_policy_id IS NULL"
            ")",
            name="ck_model_registry_isolated_snapshot",
        ),
        CheckConstraint(
            "provider_locality != 'internal_shim' OR ("
            "transport_target IS NOT NULL "
            "AND CAST(transport_target AS TEXT) != 'null' "
            "AND transport_target_sha256 IS NOT NULL "
            "AND model_registry_revision IS NOT NULL "
            "AND upstream_provider_locality IS NOT NULL "
            "AND upstream_provider_locality != 'internal_shim' "
            "AND upstream_transport_target IS NOT NULL "
            "AND CAST(upstream_transport_target AS TEXT) != 'null' "
            "AND upstream_transport_target_sha256 IS NOT NULL "
            "AND egress_policy_id IS NULL "
            "AND ((upstream_provider_locality = 'external_governed' "
            "AND upstream_egress_policy_id IS NOT NULL) "
            "OR (upstream_provider_locality IN "
            "('internal_isolated', 'unclassified') "
            "AND upstream_egress_policy_id IS NULL))"
            ")",
            name="ck_model_registry_shim_snapshot",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), unique=True, nullable=False, index=True)  # e.g. "aia/asrd"
    display_name = Column(String(200), nullable=False)
    model_type = Column(String(20), nullable=False)  # 'llm' / 'vlm' / 'embedding' / 'agent' / 'image'
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
    # Slice 8b (doc 2026-07-06-flux-image-primary-design.md §1): 完全比照
    # is_router_primary 的模式,但選的是 flux2-dev-agent / anila-studio 消費的
    # 主圖像模型(migration r1_0009 同款 partial unique index)。
    is_image_primary = Column(Boolean, nullable=False, default=False)
    # Slice 6a (doc 04 §9 / doc 01 §32 拍板五態):
    # unknown / healthy / degraded / unhealthy / disabled。舊三值
    # (online/connecting/offline) 由 r1_0005 就地遷移;'disabled' 由讀取端
    # 依 is_active 呈現(見 health_checker.normalize_health_status)。
    health_status = Column(String(20), default="unknown")
    health_checked_at = Column(DateTime, nullable=True)
    # Slice 6a (doc 04 §3): per-model API key 的 enc::v1:: envelope(與 csk- /
    # ingestion 憑證同一套 credential_crypto)。NULL = 退回全域
    # MODEL_GATEWAY_API_KEY(MVP fallback)。永不隨 API 回傳明文,GET 只露
    # ``has_api_key: bool``。
    api_key_secret_ref = Column(Text, nullable=True)
    # Gate 2: NULL is never an unlimited ceiling. New rows start at the
    # explicit least-privilege level and may be raised only by governance.
    classification_ceiling = Column(
        String(20), nullable=False, default="無機密", server_default="無機密"
    )
    # Slice 6a (doc 04 §2): supports_* 能力宣告。
    supports_streaming = Column(Boolean, nullable=False, default=True)
    supports_json_schema = Column(Boolean, nullable=False, default=False)
    supports_tools = Column(Boolean, nullable=False, default=False)
    # Migration 0033: rows whose endpoint lives on the anila-models-net
    # cross-stack docker network (e.g. ``http://gemma4:8000/v1``). Hint for
    # _build_response sentinel + ModelsView lock indicator. Validation /
    # network isolation is enforced elsewhere (SSRF guard + compose layout).
    is_internal = Column(Boolean, nullable=False, default=False)

    # Gate 5 provider authority.  ``provider_locality`` is the policy input;
    # ``is_internal`` above remains only a legacy/UI compatibility field.
    # Historical rows are deliberately ``unclassified`` and carry no inferred
    # locality or transport snapshot.
    provider_locality = Column(
        String(32),
        nullable=False,
        default="unclassified",
        server_default="unclassified",
    )
    transport_target = Column(JSONValue, nullable=True)
    transport_target_sha256 = Column(String(64), nullable=True)
    model_registry_revision = Column(String(256), nullable=True)
    upstream_provider_locality = Column(String(32), nullable=True)
    upstream_transport_target = Column(JSONValue, nullable=True)
    upstream_transport_target_sha256 = Column(String(64), nullable=True)
    egress_policy_id = Column(String(128), nullable=True)
    upstream_egress_policy_id = Column(String(128), nullable=True)

    description = Column(Text, nullable=True)
    context_window = Column(Integer, nullable=True)

    # Slice 6a (doc 04 §2): owner department（SET NULL）。
    owner_department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Agent -> base model relationship
    base_model_id = Column(Integer, ForeignKey("model_registry.id"), nullable=True)
    base_model = relationship("ModelRegistry", remote_side=[id], backref="derived_agents")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
