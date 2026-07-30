from fastapi import APIRouter
from app.api.agents import router as agents_router
from app.api.banners import router as banners_router
from app.api.auth import router as auth_router
from app.api.auth_providers import router as auth_providers_router
from app.api.api_keys import router as api_keys_router
from app.api.alerts import router as alerts_router
from app.api.audit_logs import router as audit_logs_router
from app.api.models import router as models_router
from app.api.usage import router as usage_router
from app.api.users import router as users_router
from app.api.unit_admins import router as unit_admins_router
from app.api.endpoint_authors import router as endpoint_authors_router
from app.api.message_actions import router as message_actions_router
from app.api.departments import router as departments_router
from app.api.memory import router as memory_router
from app.api.platform_links import router as platform_links_router
from app.api.proxy import router as proxy_router
from app.api.traces import router as traces_router
from app.api.artifacts import router as artifacts_router
from app.api.service_access_grants import router as service_access_grants_router
from app.api.service_clients import router as service_clients_router
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
from app.api.jwks import router as jwks_router
from app.api.classification_inventory import router as classification_inventory_router
from app.modules.policy import router as policy_decisions_router
from app.modules.tasks import router as tasks_router

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(auth_providers_router)
api_router.include_router(api_keys_router)
api_router.include_router(alerts_router)
api_router.include_router(audit_logs_router)
api_router.include_router(models_router)
api_router.include_router(usage_router)
api_router.include_router(users_router)
api_router.include_router(unit_admins_router)
api_router.include_router(endpoint_authors_router)
api_router.include_router(message_actions_router)
api_router.include_router(departments_router)
api_router.include_router(memory_router)
api_router.include_router(platform_links_router)
api_router.include_router(service_access_grants_router)
api_router.include_router(service_clients_router)
api_router.include_router(services_router)
api_router.include_router(agents_router)
api_router.include_router(banners_router)
api_router.include_router(ingestion_collections_router)
api_router.include_router(ingestion_credentials_router)
api_router.include_router(ingestion_documents_router)
api_router.include_router(ingestion_eval_runs_router)
api_router.include_router(ingestion_jobs_router)
api_router.include_router(ingestion_preview_router)
api_router.include_router(ingestion_relations_router)
api_router.include_router(ingestion_search_router)
api_router.include_router(ingestion_image_blob_router)
api_router.include_router(trusted_hosts_router)
api_router.include_router(tasks_router)
api_router.include_router(policy_decisions_router)
api_router.include_router(proxy_router)
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
