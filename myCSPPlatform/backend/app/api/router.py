from fastapi import APIRouter
from app.api.agents import router as agents_router
from app.api.auth import router as auth_router
from app.api.api_keys import router as api_keys_router
from app.api.alerts import router as alerts_router
from app.api.audit_logs import router as audit_logs_router
from app.api.models import router as models_router
from app.api.usage import router as usage_router
from app.api.users import router as users_router
from app.api.departments import router as departments_router
from app.api.auth_providers import router as auth_providers_router
from app.api.platform_links import router as platform_links_router
from app.api.proxy import router as proxy_router
from app.api.service_access_grants import router as service_access_grants_router
from app.api.studio import router as studio_router
from app.api.action_function import (
    crud_router as action_function_crud_router,
    valves_router as action_function_valves_router,
    marketplace_router as action_function_marketplace_router,
    run_router as action_function_run_router,
    runs_router as action_function_runs_router,
    enabled_actions_router as action_function_enabled_actions_router,
)
from app.api.ingestion import (
    collections_router as ingestion_collections_router,
    credentials_router as ingestion_credentials_router,
    documents_router as ingestion_documents_router,
    eval_runs_router as ingestion_eval_runs_router,
    jobs_router as ingestion_jobs_router,
    search_router as ingestion_search_router,
)

api_router = APIRouter()

api_router.include_router(auth_router)
api_router.include_router(api_keys_router)
api_router.include_router(alerts_router)
api_router.include_router(audit_logs_router)
api_router.include_router(models_router)
api_router.include_router(usage_router)
api_router.include_router(users_router)
api_router.include_router(departments_router)
api_router.include_router(auth_providers_router)
api_router.include_router(platform_links_router)
api_router.include_router(service_access_grants_router)
api_router.include_router(agents_router)
api_router.include_router(ingestion_collections_router)
api_router.include_router(ingestion_credentials_router)
api_router.include_router(ingestion_documents_router)
api_router.include_router(ingestion_eval_runs_router)
api_router.include_router(ingestion_jobs_router)
api_router.include_router(ingestion_search_router)
api_router.include_router(studio_router)
# ANILA Functions v1 — order chosen so `/enabled-actions` (a fixed path)
# is registered before `/{slug}` matchers in crud_router; FastAPI routes
# in order of include, so this avoids a slug match swallowing the literal.
api_router.include_router(action_function_enabled_actions_router)
api_router.include_router(action_function_runs_router)
api_router.include_router(action_function_run_router)
api_router.include_router(action_function_marketplace_router)
api_router.include_router(action_function_valves_router)
api_router.include_router(action_function_crud_router)
api_router.include_router(proxy_router)
