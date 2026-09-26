"""Import every model up front so SQLAlchemy's class registry is fully
populated before the first mapper configure.

String-based relationships (e.g. ``relationship("Department")``) are resolved
by name against the class registry at configure-time. If the referenced class
hasn't been imported yet, configure raises ``InvalidRequestError: failed to
locate a name``. Import order here is alphabetical — SQLAlchemy handles the
actual dependency resolution once every class is registered.
"""

from app.models.agent import (
    Agent,
    AgentCollectionBinding,
    ApiKeyAgentPermission,
    UserAgentPermission,
)
from app.models.agent_credential import AgentCredential
from app.models.agent_prompt import AgentFunction, AgentPrompt
from app.models.agent_session_owner import AgentSessionOwner
from app.models.alert import Alert
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.artifact import (
    Artifact,
    ArtifactJob,
    ArtifactVersion,
    ExportRecord,
)
from app.models.attachment import Attachment
from app.models.audit_checkpoint import AuditCheckpoint
from app.models.audit_log import AuditLog
from app.models.banner import Banner
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.conversation import Conversation, ConversationUserMeta
from app.models.department import Department
from app.models.handoff import Handoff
from app.models.jwt_signing_key import JwtSigningKey
from app.models.ingestion import (
    AgentLlmCredential,  # back-compat alias for UserLlmCredential
    DocumentRelation,
    IngestionCollection,
    IngestionDocument,
    IngestionEvalRun,
    IngestionJob,
    UserLlmCredential,
)
from app.models.message import Message
from app.models.message_action import MessageAction, MessageActionBinding
from app.models.model_access_group import ModelAccessGroup, ModelAccessGroupMember
from app.models.model_registry import ModelRegistry
from app.models.model_role import ModelRole
from app.models.router_model_grant import RouterModelGrant
from app.models.platform_link import PlatformLink
from app.models.platform_setting import PlatformSetting
from app.models.policy_decision import PolicyDecision
from app.models.registered_service import (
    RegisteredService,
    ServiceProjectBinding,
)
from app.models.service_access_grant import ServiceAccessGrant
from app.models.service_client import ServiceClient
from app.models.service_launch import ServiceAuditCallback, ServiceLaunch
from app.models.source_snapshot import Citation, SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.token_revocation import TokenRevocation
from app.models.token_usage import TokenUsage
from app.models.endpoint_author_grant import EndpointAuthorGrant
from app.models.external_service import ExternalService
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User, UserModelPermission
from app.models.user_memory import (
    ConversationMemoryChunk,
    ConversationSummary,
    MemoryRefreshLease,
    MemoryTombstone,
    UserFact,
)

__all__ = [
    "Agent",
    "AgentCollectionBinding",
    "AgentCredential",
    "AgentFunction",
    "AgentPrompt",
    "AgentSessionOwner",
    "Alert",
    "ApiKey",
    "ApiKeyAgentPermission",
    "ApiKeyModelPermission",
    "Artifact",
    "ArtifactJob",
    "ArtifactVersion",
    "Attachment",
    "ExportRecord",
    "AuditCheckpoint",
    "AuditLog",
    "Banner",
    "Citation",
    "ClassificationAuthorityAssignment",
    "ClassificationEvent",
    "Conversation",
    "ConversationUserMeta",
    "DeclassificationRequest",
    "Department",
    "AgentLlmCredential",
    "ConversationMemoryChunk",
    "ConversationSummary",
    "MemoryRefreshLease",
    "MemoryTombstone",
    "DocumentRelation",
    "EndpointAuthorGrant",
    "ExternalService",
    "Handoff",
    "IngestionCollection",
    "IngestionDocument",
    "IngestionEvalRun",
    "IngestionJob",
    "JwtSigningKey",
    "Message",
    "MessageAction",
    "MessageActionBinding",
    "ModelAccessGroup",
    "ModelAccessGroupMember",
    "ModelRegistry",
    "ModelRole",
    "PlatformLink",
    "PlatformSetting",
    "PolicyDecision",
    "RegisteredService",
    "RouterModelGrant",
    "ServiceAccessGrant",
    "ServiceAuditCallback",
    "ServiceClient",
    "ServiceLaunch",
    "ServiceProjectBinding",
    "SourceSnapshot",
    "Task",
    "TaskRun",
    "TokenRevocation",
    "TokenUsage",
    "UnitAdminAssignment",
    "User",
    "UserAgentPermission",
    "UserFact",
    "UserLlmCredential",
    "UserModelPermission",
]
