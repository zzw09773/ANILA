"""Service-level access control for platform_links.

Single source of truth for "can this user see / open this service?". All API
endpoints that surface or gate on a platform_link MUST go through this module
— never reimplement the algorithm inline. The authoritative algorithm:

    1. Link must be active (``is_active = True``). Else: deny.
    2. Admin bypass — if ``user.role == 'admin'``, allow.
       Admins are universally trusted on active links: they bypass the role
       gate, public flag, and grant check entirely. This matches the
       "superuser sees everything" model and avoids the surprise of an
       admin losing visibility on a link configured with
       ``required_roles=['developer']``.
    3. ``role`` gate — if ``link.required_roles`` is non-empty, ``user.role``
       must be in it. Else: deny. Empty list means the gate is open.
    4. Public bypass — if ``link.is_public`` is True, allow. Public links
       still respect step 3's role gate (so "public to developers only"
       works), but they skip the per-user / per-department grant check.
    5. Grant check — must have an active grant (``revoked_at IS NULL``)
       targeting this link, EITHER user-level (``user_id = me``) OR
       department-level (``department_id = my_department``).

This means access is "default deny" for non-admin / non-public links — no
automatic open access just because a link exists. The role gate is the
cheap pre-filter; the grant check is the authoritative per-user /
per-department opt-in.

See docs/multi-service-integration-plan.md §7.5 for the design rationale and
the migrations 0012_add_service_access_control.py + 0013_add_platform_link_is_public.py
for the underlying schema.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.platform_link import PlatformLink
from app.models.service_access_grant import ServiceAccessGrant
from app.models.user import User


def _active_link_ids_for_user(db: Session, user: User) -> set[int]:
    """Return platform_link ids the user has an active grant for, either
    directly (user-level) or via their department (department-level)."""
    clauses = [ServiceAccessGrant.user_id == user.id]
    if user.department_id is not None:
        # Skip the department clause entirely when user has no department —
        # otherwise SQL `NULL = NULL` would silently return UNKNOWN, and
        # adding a redundant `IS NOT NULL` filter just bloats the query plan.
        clauses.append(ServiceAccessGrant.department_id == user.department_id)

    query = db.query(ServiceAccessGrant.platform_link_id).filter(
        ServiceAccessGrant.revoked_at.is_(None),
        or_(*clauses),
    )
    return {row[0] for row in query.all()}


def can_access_link(db: Session, user: User, link: PlatformLink) -> bool:
    """Return True iff user is allowed to see/open this link."""
    if not link.is_active:
        return False
    if user.role == "admin":
        # Admin bypass placed *before* the role gate so an admin viewing a
        # link configured with required_roles=['developer'] still sees it.
        # This matches the documented "superuser is universally trusted"
        # model and is consistent with accessible_links_for() below.
        return True
    required = link.required_roles or []
    if required and user.role not in required:
        return False
    if link.is_public:
        return True
    return link.id in _active_link_ids_for_user(db, user)


def accessible_links_for(
    db: Session,
    user: User,
    *,
    include_inactive: bool = False,
) -> list[PlatformLink]:
    """Return all platform_links the user can see, sorted by sort_order.

    Single-query implementation: pre-fetches user's grant set, then filters
    in Python. This is ~10x faster than per-link can_access_link() in a
    Python loop because it avoids N+1 queries.
    """
    query = db.query(PlatformLink).order_by(
        PlatformLink.sort_order, PlatformLink.created_at
    )
    if not include_inactive:
        query = query.filter(PlatformLink.is_active.is_(True))
    links: list[PlatformLink] = query.all()

    if user.role == "admin":
        # Admin sees every active link; role gate + grant check both
        # bypassed (matches can_access_link()'s admin path).
        return links

    grant_set = _active_link_ids_for_user(db, user)
    out: list[PlatformLink] = []
    for link in links:
        required = link.required_roles or []
        if required and user.role not in required:
            continue
        if link.is_public or link.id in grant_set:
            out.append(link)
    return out


def filter_accessible(
    db: Session, user: User, link_ids: Iterable[int]
) -> set[int]:
    """Return the subset of link_ids the user can access.

    Useful when caller already has link ids in hand (e.g., from a redirect /
    deep link) and just wants a yes/no per id without re-fetching every link
    row. Skips inactive links and applies the same role + grant rules as
    can_access_link().
    """
    ids = list(link_ids)
    if not ids:
        return set()
    links = db.query(PlatformLink).filter(PlatformLink.id.in_(ids)).all()
    return {link.id for link in links if can_access_link(db, user, link)}
