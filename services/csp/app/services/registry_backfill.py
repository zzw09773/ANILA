# -*- coding: utf-8 -*-
"""Data-migration: platform_links → registered_services (Slice 7, doc 07 §14).

Extracted from the alembic migration so it is DB-agnostic (ORM handles JSON
serialisation per-dialect) and directly unit-testable on the SQLite fixture.
Each legacy ``platform_links`` row is copied into ``registered_services``
PRESERVING ITS INTEGER id (so grants map 1:1) and its create timestamp; the 14
new target fields get safe defaults, and the database is the sole source of
truth for every migrated service.

Finally every ``service_access_grant`` gets ``service_id = platform_link_id``
so the grant history follows the upgrade intact (idempotent — skips rows that
already migrated).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _origin_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def backfill_registered_services(bind: Connection) -> int:
    """Copy platform_links → registered_services + backfill grants.service_id.

    Returns the number of services created. Idempotent: a platform_link whose
    id already exists in registered_services is skipped."""
    # Local imports keep alembic module import cheap and avoid cycles.
    from app.models.platform_link import PlatformLink
    from app.models.registered_service import RegisteredService
    from app.models.service_access_grant import ServiceAccessGrant
    from app.utils.slug import unique_slug

    now = datetime.now(timezone.utc)
    created = 0

    session = Session(bind=bind)
    try:
        taken = {row[0] for row in session.query(RegisteredService.slug).all()}
        links = session.query(PlatformLink).order_by(PlatformLink.id).all()
        for link in links:
            if session.get(RegisteredService, link.id) is not None:
                continue  # already migrated
            slug = unique_slug(link.name, taken, fallback=f"link-{link.id}")
            taken.add(slug)
            origin = _origin_of(link.url)
            session.add(
                RegisteredService(
                    id=link.id,
                    name=link.name,
                    slug=slug,
                    description=link.description,
                    icon=link.icon,
                    entry_url=link.url,
                    allowed_origins=[origin] if origin else [],
                    required_roles=list(link.required_roles or []),
                    service_admin_user_ids=[],
                    data_ingress=[],
                    data_egress=[],
                    sort_order=link.sort_order or 0,
                    is_active=bool(link.is_active),
                    is_public=bool(link.is_public),
                    config_source="db",
                    env_seed_key=None,
                    db_editable_fields=[],
                    last_seeded_at=None,
                    created_at=link.created_at or now,
                    updated_at=link.created_at or now,
                )
            )
            created += 1
        session.flush()

        # Grant history follows the upgrade: service_id := platform_link_id.
        session.query(ServiceAccessGrant).filter(
            ServiceAccessGrant.service_id.is_(None),
            ServiceAccessGrant.platform_link_id.isnot(None),
        ).update(
            {ServiceAccessGrant.service_id: ServiceAccessGrant.platform_link_id},
            synchronize_session=False,
        )
        session.commit()
    finally:
        session.close()

    logger.info("registered_services 回填完成:新增 %d 筆", created)
    return created
