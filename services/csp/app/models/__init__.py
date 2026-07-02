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
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.banner import Banner
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.conversation import Conversation
from app.models.department import Department
from app.models.handoff import Handoff
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
from app.models.model_registry import ModelRegistry
from app.models.platform_link import PlatformLink
from app.models.policy_decision import PolicyDecision
from app.models.service_client import ServiceClient
from app.models.source_snapshot import Citation, SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.token_revocation import TokenRevocation
from app.models.token_usage import TokenUsage
from app.models.trace_span import TraceSpan
from app.models.user import User, UserModelPermission
from app.models.user_memory import ConversationMemoryChunk, UserFact

__all__ = [
    "Agent",
    "AgentCredential",
    "AgentFunction",
    "AgentPrompt",
    "Alert",
    "ApiKey",
    "ApiKeyAgentPermission",
    "ApiKeyModelPermission",
    "Attachment",
    "AuditLog",
    "Banner",
    "Citation",
    "ClassificationAuthorityAssignment",
    "ClassificationEvent",
    "Conversation",
    "DeclassificationRequest",
    "Department",
    "AgentLlmCredential",
    "ConversationMemoryChunk",
    "DocumentRelation",
    "Handoff",
    "IngestionCollection",
    "IngestionDocument",
    "IngestionEvalRun",
    "IngestionJob",
    "Message",
    "ModelRegistry",
    "PlatformLink",
    "PolicyDecision",
    "ServiceClient",
    "SourceSnapshot",
    "Task",
    "TaskRun",
    "TokenRevocation",
    "TokenUsage",
    "TraceSpan",
    "User",
    "UserAgentPermission",
    "UserFact",
    "UserLlmCredential",
    "UserModelPermission",
]
