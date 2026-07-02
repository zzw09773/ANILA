# -*- coding: utf-8 -*-
"""Data-migration: platform_links → registered_services (Slice 7, doc 07 §14).

Extracted from the alembic migration so it is DB-agnostic (ORM handles JSON
serialisation per-dialect) and directly unit-testable on the SQLite fixture.
Each legacy ``platform_links`` row is copied into ``registered_services``
PRESERVING ITS INTEGER id (so grants map 1:1) and its create timestamp; the 14
new target fields get safe defaults, ``allowed_origins`` is backfilled from the
entry URL origin, and ``config_source`` is detected per doc §15.1:

* name present in ``AUTO_REGISTER_LINKS`` → ``env_seeded`` (env_seed_key=name,
  db_editable_fields=["is_active"], last_seeded_at=now)
* otherwise → ``db`` (UI-authored / unknown origin; never clobbered by seed)

Finally every ``service_access_grant`` gets ``service_id = platform_link_id``
so the grant history follows the upgrade intact (idempotent — skips rows that
already migrated).
"""

from __future__ import annotations

import json
import logging
import os
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


def env_seeded_names() -> set[str]:
    """Names present in the AUTO_REGISTER_LINKS env → env_seeded detection."""
    raw = os.environ.get("AUTO_REGISTER_LINKS", "") or ""
    if not raw.strip():
        return set()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for item in parsed if isinstance(parsed, list) else []:
        name = (item or {}).get("name")
        if name:
            names.add(name)
    return names


def backfill_registered_services(bind: Connection) -> int:
    """Copy platform_links → registered_services + backfill grants.service_id.

    Returns the number of services created. Idempotent: a platform_link whose
    id already exists in registered_services is skipped."""
    # Local imports keep alembic module import cheap and avoid cycles.
    from app.models.platform_link import PlatformLink
    from app.models.registered_service import RegisteredService
    from app.models.service_access_grant import ServiceAccessGrant
    from app.utils.slug import unique_slug

    seeded = env_seeded_names()
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
            is_seeded = link.name in seeded
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
                    config_source="env_seeded" if is_seeded else "db",
                    env_seed_key=link.name if is_seeded else None,
                    db_editable_fields=["is_active"] if is_seeded else [],
                    last_seeded_at=now if is_seeded else None,
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
