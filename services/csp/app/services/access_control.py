"""Service-level access control for the Service Registry (doc 07 §12).

Single source of truth for "can this user see / launch this service?". All API
endpoints that surface or gate on a RegisteredService MUST go through this
module — never reimplement the algorithm inline. The authoritative algorithm
(doc 07 §12; steps 1–5 preserved from the legacy platform_links algorithm,
steps 6–8 added by Slice 7):

    1. Service must be active (``is_active = True``). Else: deny.
    2. Admin / owner bypass — ``is_admin_tier(user)`` sees & manages every
       active service, crossing the per-service ``service_admin_user_ids``
       boundary and grant checks (doc §12; "superuser sees everything").
    3. Role gate — if ``required_roles`` is non-empty, ``user.role`` must be in
       it. Empty list = open gate.
    4. Public bypass — ``is_public`` skips the per-user / per-department grant.
    5. Grant check — active grant (``revoked_at IS NULL``) targeting this
       service, either user-level or department-level.
    6. Classification clearance — when a ``context_level`` is supplied (launch
       time), the launch's classification must be ``<=`` the service's
       ``classification_ceiling``. This is a HARD ceiling checked for ALL tiers
       (admins included) — the admin bypass in step 2 does NOT lift it, so the
       single-directional classification invariant is never weakened.
    7. Project membership check — MVP no-op pass (per-user project membership
       is not yet modelled; ``service_project_bindings`` exist but there is no
       user↔project store to gate on). Wired here for the future.
    8. Service launch policy check — MVP no-op pass (per-service launch policy
       beyond steps 3–6 is not yet modelled). Wired here for the future.

Default deny for non-admin / non-public services with no grant. Grants are
matched on ``service_id`` (new) OR the legacy ``platform_link_id`` (migrated
grants carry both; the ids mirror 1:1), so no grant is lost in the upgrade.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.registered_service import RegisteredService
from app.models.service_access_grant import ServiceAccessGrant
from app.models.user import User
from anila_contracts import Classification as ClassificationLevel
from app.services.auth_service import is_admin_tier


def _active_service_ids_for_user(db: Session, user: User) -> set[int]:
    """Return RegisteredService ids the user has an active grant for, either
    directly (user-level) or via their department (department-level).

    Both the new ``service_id`` and the legacy ``platform_link_id`` columns are
    collected so migrated grants (keyed by platform_link_id) and new grants
    (keyed by service_id) both count."""
    who = [ServiceAccessGrant.user_id == user.id]
    if user.department_id is not None:
        who.append(ServiceAccessGrant.department_id == user.department_id)

    rows = (
        db.query(
            ServiceAccessGrant.service_id,
            ServiceAccessGrant.platform_link_id,
        )
        .filter(ServiceAccessGrant.revoked_at.is_(None), or_(*who))
        .all()
    )
    ids: set[int] = set()
    for service_id, platform_link_id in rows:
        if service_id is not None:
            ids.add(service_id)
        if platform_link_id is not None:
            ids.add(platform_link_id)
    return ids


def _classification_ok(context_level: str | None, ceiling: str | None) -> bool:
    """Step 6: launch level must be ``<=`` the service ceiling. No context or
    no ceiling = pass. Unknown level strings fail-closed via ValueError from
    ``ClassificationLevel.from_storage`` (propagated to the caller)."""
    if context_level is None or not ceiling:
        return True
    return ClassificationLevel.from_storage(context_level) <= (
        ClassificationLevel.from_storage(ceiling)
    )


def can_access_service(
    db: Session,
    user: User,
    service: RegisteredService,
    *,
    context_level: str | None = None,
) -> bool:
    """Return True iff user may see / launch this service (doc 07 §12)."""
    if not service.is_active:
        return False
    # Step 6 first so the hard classification ceiling binds every tier.
    if not _classification_ok(context_level, service.classification_ceiling):
        return False
    if is_admin_tier(user):
        return True  # steps 3–5, 7–8 bypass (doc §12)
    required = service.required_roles or []
    if required and user.role not in required:
        return False
    if not (service.is_public or service.id in _active_service_ids_for_user(db, user)):
        return False
    # Steps 7 (project membership) & 8 (launch policy): MVP no-op pass.
    return True


def accessible_services_for(
    db: Session,
    user: User,
    *,
    include_inactive: bool = False,
) -> list[RegisteredService]:
    """Return all services the user can see, sorted by sort_order.

    Single-query implementation: pre-fetches the user's grant set, then filters
    in Python (avoids N+1). No ``context_level`` here — classification clearance
    (step 6) is a launch-time gate, not a list-time one."""
    query = db.query(RegisteredService).order_by(
        RegisteredService.sort_order, RegisteredService.created_at
    )
    if not include_inactive:
        query = query.filter(RegisteredService.is_active.is_(True))
    services: list[RegisteredService] = query.all()

    if is_admin_tier(user):
        return services

    grant_set = _active_service_ids_for_user(db, user)
    out: list[RegisteredService] = []
    for service in services:
        required = service.required_roles or []
        if required and user.role not in required:
            continue
        if service.is_public or service.id in grant_set:
            out.append(service)
    return out


def filter_accessible(
    db: Session, user: User, service_ids: Iterable[int]
) -> set[int]:
    """Return the subset of service_ids the user can access."""
    ids = list(service_ids)
    if not ids:
        return set()
    services = (
        db.query(RegisteredService)
        .filter(RegisteredService.id.in_(ids))
        .all()
    )
    return {s.id for s in services if can_access_service(db, user, s)}


# ── Legacy compat aliases (platform_links vocabulary) ───────────────────────
# The /api/platform-links façade and any older callers keep these names; they
# now operate on RegisteredService rows transparently.
can_access_link = can_access_service
accessible_links_for = accessible_services_for
