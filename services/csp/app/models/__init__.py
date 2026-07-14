"""Import every model up front so SQLAlchemy's class registry is fully
populated before the first mapper configure.

String-based relationships (e.g. ``relationship("Department")``) are resolved
by name against the class registry at configure-time. If the referenced class
hasn't been imported yet, configure raises ``InvalidRequestError: failed to
locate a name``. Import order here is alphabetical — SQLAlchemy handles the
actual dependency resolution once every class is registered.
"""

from app.models.agent import Agent, ApiKeyAgentPermission, UserAgentPermission
from app.models.agent_credential import AgentCredential
from app.models.agent_prompt import AgentFunction, AgentPrompt
from app.models.alert import Alert
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.artifact import (
    Artifact,
    ArtifactJob,
    ArtifactVersion,
    ExportRecord,
)
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.auth_session import AuthRefreshToken, AuthSession
from app.models.banner import Banner
from app.models.card_login_challenge import CardLoginChallenge
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    DocumentRequiredCompartment,
    SecurityCompartment,
)
from app.models.conversation import Conversation
from app.models.department import Department
from app.models.handoff import Handoff
from app.models.ingestion import (
    AgentLlmCredential,  # back-compat alias for UserLlmCredential
    DocumentRelation,
    IngestionCollection,
    IngestionDocument,
    IngestionDocumentGeneration,
    IngestionEvalRun,
    IngestionJob,
    IngestionOutbox,
    SimilarityRecomputeRequest,
    UserLlmCredential,
)
from app.models.message import Message
from app.models.model_registry import ModelRegistry
from app.models.platform_link import PlatformLink
from app.models.policy_decision import PolicyDecision
from app.models.registered_service import (
    RegisteredService,
    ServiceProjectBinding,
)
from app.models.retention import RetentionReaperLease
from app.models.service_access_grant import ServiceAccessGrant
from app.models.service_client import ServiceClient
from app.models.service_launch import ServiceAuditCallback, ServiceLaunch
from app.models.source_snapshot import Citation, SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.token_revocation import TokenRevocation
from app.models.token_usage import TokenUsage
from app.models.trace_span import TraceSpan
from app.models.user import User, UserModelPermission
from app.models.user_memory import (
    ConversationMemoryChunk,
    MemoryChunkRequiredCompartment,
    MemoryChunkSourceCollection,
    UserFact,
    UserFactRequiredCompartment,
    UserFactSourceCollection,
)

__all__ = [
    "Agent",
    "AgentCredential",
    "AgentFunction",
    "AgentPrompt",
    "Alert",
    "ApiKey",
    "ApiKeyAgentPermission",
    "ApiKeyModelPermission",
    "Artifact",
    "ArtifactJob",
    "ArtifactVersion",
    "Attachment",
    "ExportRecord",
    "AuditLog",
    "AuthRefreshToken",
    "AuthSession",
    "Banner",
    "CardLoginChallenge",
    "Citation",
    "ClassificationAuthorityAssignment",
    "ClassificationEvent",
    "ClearanceGrant",
    "ClearanceGrantCompartment",
    "CollectionAccessGrant",
    "CollectionRequiredCompartment",
    "Conversation",
    "DeclassificationRequest",
    "Department",
    "AgentLlmCredential",
    "ConversationMemoryChunk",
    "MemoryChunkRequiredCompartment",
    "MemoryChunkSourceCollection",
    "DocumentRelation",
    "DocumentRequiredCompartment",
    "Handoff",
    "IngestionCollection",
    "IngestionDocument",
    "IngestionDocumentGeneration",
    "IngestionEvalRun",
    "IngestionJob",
    "IngestionOutbox",
    "SimilarityRecomputeRequest",
    "Message",
    "ModelRegistry",
    "PlatformLink",
    "PolicyDecision",
    "RegisteredService",
    "RetentionReaperLease",
    "ServiceAccessGrant",
    "ServiceAuditCallback",
    "ServiceClient",
    "ServiceLaunch",
    "ServiceProjectBinding",
    "SourceSnapshot",
    "SecurityCompartment",
    "Task",
    "TaskRun",
    "TokenRevocation",
    "TokenUsage",
    "TraceSpan",
    "User",
    "UserAgentPermission",
    "UserFact",
    "UserFactRequiredCompartment",
    "UserFactSourceCollection",
    "UserLlmCredential",
    "UserModelPermission",
]
