"""Agent management API (JWT-protected control plane).

Credential tier: JWT access token for all endpoints here.
Data plane list endpoint (GET /v1/agents, API Key auth) lives in proxy.py.

Package split (behavior-preserving refactor of the former 1384-line
``app/api/agents.py`` god-module — doc-10 Slice 1):

- ``registration``   register / list / get / update / delete + template download
- ``approval``       approve / reject
- ``runtime_config`` runtime-config get/patch + ``/me/runtime-config``
- ``credentials``    bootstrap / issue / rotate / revoke + encryption toggle
- ``functions``      agent functions (prompts) CRUD + system-prompt suggest
- ``health``         health-check + test-connection
- ``_common``        helpers shared by 2+ submodules

This ``__init__`` assembles ONE router identical to the old module's
(same prefix / tags) and re-exports every module-level symbol the old
module exposed, so ``app.api.agents.<name>`` keeps resolving for both
imports and test monkeypatching.
"""
import io
import os as _os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from anila_core.security import UnsafeEndpointError, validate_outbound_url
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.agent_credential import AgentCredential
from app.models.agent_prompt import AgentFunction
from app.models.user import User
from app.services import agent_credential_service
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    require_admin,
    verify_service_token,
)
from app.services.proxy_service import invalidate_agent_token_cache

from app.api.agents import (
    approval,
    credentials,
    functions,
    health,
    registration,
    runtime_config,
)
from app.api.agents._common import (
    _client_ip,
    _require_developer_or_admin,
    _resolve_agent,
)
from app.api.agents.approval import approve_agent, reject_agent
from app.api.agents.credentials import (
    AgentEncryptionUpdate,
    BootstrapExchangeRequest,
    BootstrapExchangeResponse,
    CredentialResponse,
    IssueBootstrapRequest,
    IssueBootstrapResponse,
    IssueStaticRequest,
    RotateCredentialRequest,
    _resolve_credential,
    _serialize_credential,
    bootstrap_exchange,
    get_my_credential,
    issue_bootstrap,
    issue_static_credential,
    list_credentials,
    revoke_credential,
    rotate_credential,
    set_agent_encryption,
)
from app.api.agents.functions import (
    _FUNCTION_KINDS,
    AgentFunctionCreate,
    AgentFunctionResponse,
    AgentFunctionUpdate,
    SystemPromptSuggestRequest,
    SystemPromptSuggestResponse,
    _agent_or_404,
    _require_function_editor,
    _require_function_reader,
    _serialize_function,
    _validate_kind,
    create_agent_function,
    delete_agent_function,
    list_agent_functions,
    suggest_system_prompt,
    update_agent_function,
)
from app.api.agents.health import (
    TestConnectionResponse,
    run_agent_trace_test,
    test_agent_connection,
    trigger_agent_health_check,
)
from app.api.agents.registration import (
    _AGENT_HEALTH_MAP,
    _IGNORED_TEMPLATE_PARTS,
    _IGNORED_TEMPLATE_SUFFIXES,
    _TEMPLATE_DIR,
    AgentRegisterRequest,
    AgentResponse,
    AgentUpdateRequest,
    _enforce_endpoint_url,
    _serialize_agent,
    _should_include_template_path,
    delete_agent,
    download_template,
    get_agent,
    list_agents,
    register_agent,
    update_agent,
)
from app.api.agents.runtime_config import (
    AgentRuntimeConfigUpdate,
    get_agent_runtime_config,
    get_my_runtime_config,
    patch_agent_runtime_config,
)

# Same final paths/tags as the old single router (prefix="/api/agents",
# tags=["Agent 管理"]). The prefix goes on each include_router call instead
# of the constructor because FastAPI rejects including a router whose route
# path is "" (GET /api/agents list endpoint) under an empty include-prefix.
router = APIRouter(tags=["Agent 管理"])
router.include_router(registration.router, prefix="/api/agents")
router.include_router(approval.router, prefix="/api/agents")
router.include_router(runtime_config.router, prefix="/api/agents")
router.include_router(credentials.router, prefix="/api/agents")
router.include_router(health.router, prefix="/api/agents")
router.include_router(functions.router, prefix="/api/agents")

__all__ = [
    # router (assembled; same prefix/tags/paths as the old module)
    "router",
    # submodules
    "approval",
    "credentials",
    "functions",
    "health",
    "registration",
    "runtime_config",
    # original module-level imports (kept importable / monkeypatchable)
    "io",
    "_os",
    "zipfile",
    "datetime",
    "timezone",
    "Path",
    "httpx",
    "UnsafeEndpointError",
    "validate_outbound_url",
    "APIRouter",
    "Depends",
    "HTTPException",
    "Request",
    "StreamingResponse",
    "BaseModel",
    "Field",
    "Session",
    "get_db",
    "Agent",
    "UserAgentPermission",
    "AgentCredential",
    "AgentFunction",
    "User",
    "agent_credential_service",
    "log_audit_event",
    "get_current_user",
    "is_admin_tier",
    "require_admin",
    "verify_service_token",
    "invalidate_agent_token_cache",
    # shared helpers
    "_client_ip",
    "_require_developer_or_admin",
    "_resolve_agent",
    # registration
    "_AGENT_HEALTH_MAP",
    "_IGNORED_TEMPLATE_PARTS",
    "_IGNORED_TEMPLATE_SUFFIXES",
    "_TEMPLATE_DIR",
    "AgentRegisterRequest",
    "AgentResponse",
    "AgentUpdateRequest",
    "_enforce_endpoint_url",
    "_serialize_agent",
    "_should_include_template_path",
    "download_template",
    "register_agent",
    "list_agents",
    "get_agent",
    "update_agent",
    "delete_agent",
    # approval
    "approve_agent",
    "reject_agent",
    # runtime config
    "AgentRuntimeConfigUpdate",
    "get_agent_runtime_config",
    "patch_agent_runtime_config",
    "get_my_runtime_config",
    # credentials + encryption
    "AgentEncryptionUpdate",
    "IssueBootstrapRequest",
    "IssueBootstrapResponse",
    "BootstrapExchangeRequest",
    "BootstrapExchangeResponse",
    "IssueStaticRequest",
    "CredentialResponse",
    "RotateCredentialRequest",
    "_serialize_credential",
    "_resolve_credential",
    "set_agent_encryption",
    "issue_bootstrap",
    "bootstrap_exchange",
    "issue_static_credential",
    "list_credentials",
    "rotate_credential",
    "revoke_credential",
    "get_my_credential",
    # health / probes
    "TestConnectionResponse",
    "trigger_agent_health_check",
    "test_agent_connection",
    "run_agent_trace_test",
    # functions (prompts) + system prompt suggest
    "_FUNCTION_KINDS",
    "AgentFunctionCreate",
    "AgentFunctionUpdate",
    "AgentFunctionResponse",
    "_serialize_function",
    "_agent_or_404",
    "_require_function_editor",
    "_require_function_reader",
    "_validate_kind",
    "list_agent_functions",
    "create_agent_function",
    "update_agent_function",
    "delete_agent_function",
    "SystemPromptSuggestRequest",
    "SystemPromptSuggestResponse",
    "suggest_system_prompt",
]
