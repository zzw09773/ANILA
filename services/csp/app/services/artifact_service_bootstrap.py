"""Idempotent bootstrap for anila-studio's artifact-writer identity."""

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


CLIENT_NAME = "anila-studio-artifact-writer"
SERVICE_SLUG = "anila-studio-artifact"


def ensure_artifact_service(db: Session, *, token: str) -> RegisteredService:
    """Create or verify the exact named writer identity and capability.

    Existing rows are verified, never silently rotated or broadened at
    startup. Rotation remains an audited operator action.
    """
    plaintext = token.strip()
    if not plaintext:
        raise RuntimeError("STUDIO_ARTIFACT_SERVICE_TOKEN 不得為空")
    if not plaintext.startswith("csk-"):
        raise RuntimeError("STUDIO_ARTIFACT_SERVICE_TOKEN 必須使用 csk- token")

    client = (
        db.query(ServiceClient)
        .filter(ServiceClient.client_name == CLIENT_NAME)
        .first()
    )
    if client is None:
        client = ServiceClient(
            client_name=CLIENT_NAME,
            client_type="worker",
            description="anila-studio artifact control-plane writer",
            service_token_envelope=encode_service_token_envelope(plaintext),
            service_token_lookup_hash=compute_lookup_hash(plaintext),
            is_legacy=False,
            is_active=True,
        )
        db.add(client)
        db.flush()
    else:
        if not client.is_active or client.is_legacy:
            raise RuntimeError("anila-studio artifact Service Client 非 active/non-legacy")
        stored = decode_service_token_envelope(client.service_token_envelope)
        if stored is None or not hmac.compare_digest(stored, plaintext):
            raise RuntimeError(
                "STUDIO_ARTIFACT_SERVICE_TOKEN 與既有具名 Service Client 不一致;"
                "請走 audited rotation 後同步部署 secret"
            )

    services = (
        db.query(RegisteredService)
        .filter(RegisteredService.service_client_id == client.id)
        .all()
    )
    if not services:
        service = RegisteredService(
            name="ANILA Studio Artifact Writer",
            slug=SERVICE_SLUG,
            description="Internal artifact control-plane writer",
            service_type="artifact_tool",
            entry_url="http://anila-studio:8100",
            allowed_origins=[],
            data_ingress=[],
            data_egress=["artifact"],
            required_roles=[],
            service_admin_user_ids=[],
            service_client_id=client.id,
            is_public=False,
            is_active=True,
            config_source="env_seeded",
            env_seed_key="STUDIO_ARTIFACT_SERVICE_TOKEN",
            db_editable_fields=[],
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service

    if len(services) != 1:
        raise RuntimeError("artifact writer Service Client 必須唯一綁定一個服務")
    service = services[0]
    if (
        not service.is_active
        or service.service_type != "artifact_tool"
        or set(service.data_egress or []) != {"artifact"}
        or service.slug != SERVICE_SLUG
    ):
        raise RuntimeError(
            "artifact writer registry capability 漂移;"
            "必須精確為 anila-studio-artifact + artifact_tool + artifact egress"
        )
    db.commit()
    return service


__all__ = ["CLIENT_NAME", "SERVICE_SLUG", "ensure_artifact_service"]
