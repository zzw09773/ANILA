"""Idempotent bootstrap for Studio's task-scoped runtime identity."""

from __future__ import annotations

import hmac

from sqlalchemy.orm import Session

from app.models.registered_service import RegisteredService
from app.models.service_client import ServiceClient
from app.services.service_token_envelope import (
    compute_lookup_hash,
    decode_service_token_envelope,
    encode_service_token_envelope,
)


CLIENT_NAME = "anila-studio-runtime"
SERVICE_SLUG = "anila-studio-runtime"
CAPABILITY = "studio_runtime"


def ensure_studio_runtime_service(
    db: Session,
    *,
    token: str,
    artifact_token: str,
) -> RegisteredService:
    plaintext = token.strip()
    if not plaintext.startswith("csk-"):
        raise RuntimeError("STUDIO_RUNTIME_SERVICE_TOKEN 必須使用 csk- token")
    if artifact_token.strip() and hmac.compare_digest(plaintext, artifact_token.strip()):
        raise RuntimeError("Studio runtime 與 artifact writer token 不得共用")

    client = (
        db.query(ServiceClient)
        .filter(ServiceClient.client_name == CLIENT_NAME)
        .first()
    )
    if client is None:
        client = ServiceClient(
            client_name=CLIENT_NAME,
            client_type="worker",
            description="anila-studio task-scoped retrieval/inference runner",
            service_token_envelope=encode_service_token_envelope(plaintext),
            service_token_lookup_hash=compute_lookup_hash(plaintext),
            is_legacy=False,
            is_active=True,
        )
        db.add(client)
        db.flush()
    else:
        stored = decode_service_token_envelope(client.service_token_envelope)
        if (
            not client.is_active
            or client.is_legacy
            or stored is None
            or not hmac.compare_digest(stored, plaintext)
        ):
            raise RuntimeError("Studio runtime Service Client 狀態或 token 漂移")

    services = (
        db.query(RegisteredService)
        .filter(RegisteredService.service_client_id == client.id)
        .all()
    )
    if not services:
        service = RegisteredService(
            name="ANILA Studio Task Runtime",
            slug=SERVICE_SLUG,
            description="Narrow task/snapshot-bound Studio runtime",
            service_type="artifact_tool",
            entry_url="http://anila-studio:8100",
            allowed_origins=[],
            data_ingress=[],
            data_egress=[CAPABILITY],
            required_roles=[],
            service_admin_user_ids=[],
            service_client_id=client.id,
            is_public=False,
            is_active=True,
            config_source="env_seeded",
            env_seed_key="STUDIO_RUNTIME_SERVICE_TOKEN",
            db_editable_fields=[],
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service
    if len(services) != 1:
        raise RuntimeError("Studio runtime Service Client 必須唯一綁定一個服務")
    service = services[0]
    if (
        not service.is_active
        or service.slug != SERVICE_SLUG
        or service.service_type != "artifact_tool"
        or set(service.data_egress or []) != {CAPABILITY}
    ):
        raise RuntimeError("Studio runtime registry capability 漂移")
    db.commit()
    return service


__all__ = [
    "CAPABILITY",
    "CLIENT_NAME",
    "SERVICE_SLUG",
    "ensure_studio_runtime_service",
]
