"""Import every model up front so SQLAlchemy's class registry is fully
populated before the first mapper configure.

String-based relationships (e.g. ``relationship("Department")``) are resolved
by name against the class registry at configure-time. If the referenced class
hasn't been imported yet, configure raises ``InvalidRequestError: failed to
locate a name``. Import order here is alphabetical — SQLAlchemy handles the
actual dependency resolution once every class is registered.
"""

from app.models.agent import Agent, ApiKeyAgentPermission, UserAgentPermission
from app.models.alert import Alert
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.attachment import Attachment
from app.models.audit_log import AuditLog
from app.models.auth_provider import AuthProvider
from app.models.conversation import Conversation
from app.models.department import Department
from app.models.external_identity import ExternalIdentity
from app.models.handoff import Handoff
from app.models.ingestion import (
    AgentLlmCredential,
    IngestionCollection,
    IngestionDocument,
    IngestionJob,
)
from app.models.message import Message
from app.models.model_registry import ModelRegistry
from app.models.platform_link import PlatformLink
from app.models.token_usage import TokenUsage
from app.models.user import User, UserModelPermission

__all__ = [
    "Agent",
    "Alert",
    "ApiKey",
    "ApiKeyAgentPermission",
    "ApiKeyModelPermission",
    "Attachment",
    "AuditLog",
    "AuthProvider",
    "Conversation",
    "Department",
    "ExternalIdentity",
    "AgentLlmCredential",
    "Handoff",
    "IngestionCollection",
    "IngestionDocument",
    "IngestionJob",
    "Message",
    "ModelRegistry",
    "PlatformLink",
    "TokenUsage",
    "User",
    "UserAgentPermission",
    "UserModelPermission",
]
