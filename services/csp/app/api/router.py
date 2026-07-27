from fastapi import APIRouter
from app.api.agents import router as agents_router
from app.api.agents.registry import router as agent_registry_router
from app.api.banners import router as banners_router
from app.api.capabilities import router as capabilities_router
from app.api.auth import router as auth_router
from app.api.auth_providers import router as auth_providers_router
from app.api.api_keys import router as api_keys_router
from app.api.alerts import router as alerts_router
from app.api.audit_logs import router as audit_logs_router
from app.api.admin_inference_audit import router as admin_inference_audit_router
from app.api.admin import health_overview_router
from app.api.models import router as models_router
from app.api.usage import router as usage_router
from app.api.users import router as users_router
from app.api.departments import router as departments_router
from app.api.memory import router as memory_router
from app.api.platform_links import router as platform_links_router
from app.api.proxy import router as proxy_router
from app.api.router_direct_governance import router as router_direct_governance_router
from app.api.traces import router as traces_router
from app.api.artifacts import router as artifacts_router
from app.api.service_access_grants import router as service_access_grants_router
from app.api.service_clients import router as service_clients_router
from app.api.execution_grants import router as execution_grants_router
from app.api.agent_dispatch import router as agent_dispatch_router
from app.api.services import router as services_router
from app.api.trusted_hosts import router as trusted_hosts_router
from app.api.ingestion import (
    collections_router as ingestion_collections_router,
    credentials_router as ingestion_credentials_router,
    documents_router as ingestion_documents_router,
    eval_runs_router as ingestion_eval_runs_router,
    image_blob_router as ingestion_image_blob_router,
    jobs_router as ingestion_jobs_router,
    preview_router as ingestion_preview_router,
    relations_router as ingestion_relations_router,
    search_router as ingestion_search_router,
)
from app.api.ingestion.surface import GOVERNANCE_PREFIX, PERSONAL_PREFIX
from app.api.jwks import router as jwks_router
from app.api.classification_inventory import router as classification_inventory_router
from app.api.studio_runtime import router as studio_runtime_router
from app.modules.policy import router as policy_decisions_router
from app.modules.tasks import router as tasks_router
from app.modules.clearance import router as clearance_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(auth_providers_router)
api_router.include_router(api_keys_router)
api_router.include_router(alerts_router)
api_router.include_router(audit_logs_router)
api_router.include_router(admin_inference_audit_router)
# 服務健康總覽(W3-3⑦):admin 不用 SSH 也看得到基礎服務狀態。
api_router.include_router(health_overview_router)
api_router.include_router(models_router)
api_router.include_router(usage_router)
api_router.include_router(users_router)
api_router.include_router(departments_router)
api_router.include_router(memory_router)
api_router.include_router(platform_links_router)
api_router.include_router(service_access_grants_router)
api_router.include_router(service_clients_router)
# Versioned, service-only CSP grant mint seam.  The caller is a named Router
# service client; the user context is carried in the dedicated header and is
# revalidated against durable Task/AuthSession/registry state.
api_router.include_router(execution_grants_router, prefix="/internal/v1/execution-grants")
api_router.include_router(agent_dispatch_router, prefix="/internal/v1/agents")
api_router.include_router(services_router)
api_router.include_router(agents_router)
# Versioned service-only registry projection.  It is intentionally outside
# ``/api/agents`` (JWT control-plane CRUD) and never replaces legacy /v1/agents.
api_router.include_router(agent_registry_router, prefix="/internal/v1/agents")
# R7.1 service-only direct-answer model governance projection.  The Router
# derives its DIRECT_ANSWER classification ceiling from the model registry
# through this seam instead of a manual env knob.
api_router.include_router(
    router_direct_governance_router, prefix="/internal/v1/router"
)
api_router.include_router(banners_router)
# 部署能力旗標(W1-3):只回布林白名單,給前端決定「怎麼說」。
api_router.include_router(capabilities_router)

# Collection-scoped ingestion routers are mounted twice:
#   /api/ingestion/*  → governance product (origin='csp')
#   /api/personal/*   → ANILALM personal KB (origin='anilalm')
# Dual-mount is convenience for URL geometry; the load-bearing gate is
# the required ``origin=`` argument on collection resolvers (see
# ``app.api.ingestion.surface``). Credentials and chunking-preview stay
# governance-only (absolute paths, single mount).
_COLLECTION_SURFACE_ROUTERS = (
    ingestion_collections_router,
    ingestion_documents_router,
    ingestion_eval_runs_router,
    ingestion_jobs_router,
    ingestion_relations_router,
    ingestion_search_router,
    ingestion_image_blob_router,
)
for _surface_router in _COLLECTION_SURFACE_ROUTERS:
    api_router.include_router(_surface_router, prefix=GOVERNANCE_PREFIX)
    api_router.include_router(_surface_router, prefix=PERSONAL_PREFIX)
api_router.include_router(ingestion_credentials_router)
api_router.include_router(ingestion_preview_router)

api_router.include_router(trusted_hosts_router)
api_router.include_router(tasks_router)
api_router.include_router(policy_decisions_router)
api_router.include_router(clearance_router)
api_router.include_router(proxy_router)
api_router.include_router(studio_runtime_router)
# Trace REST 面(Slice 4a):POST /v1/traces/{trace_id}/spans(data plane,和
# proxy 一樣寫完整路徑無 prefix,nginx /v1 直通吃得到)+ GET /api/traces/{id}。
api_router.include_router(traces_router)
# Artifact 契約面(Slice 8a):/v1/artifact-jobs、/v1/artifacts(service token,
# 寫完整路徑無 prefix,nginx /v1 直通吃得到)+ /api/artifacts 治理讀面。
api_router.include_router(artifacts_router)
# JWKS (RFC 7517) public key endpoint for cross-service JWT verification.
# Mounted at the application level so it sits at /.well-known/jwks.json
# rather than under the /api/* prefix.
api_router.include_router(jwks_router)
# 機敏分類盤點(doc 08 §15 Classification Inventory Before Cutover;admin/owner)。
api_router.include_router(classification_inventory_router)
