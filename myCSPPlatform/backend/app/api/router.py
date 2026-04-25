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
from app.api.ingestion import (
    collections_router as ingestion_collections_router,
    documents_router as ingestion_documents_router,
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
api_router.include_router(ingestion_documents_router)
api_router.include_router(proxy_router)
