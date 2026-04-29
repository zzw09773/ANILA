"""Function CRUD with advisory-lock-protected version save.

The append-only ``action_function_versions`` table has a unique
``(function_id, version_no)`` constraint. Concurrent ``save_version``
calls on the same function would otherwise race and one of them would
hit the unique violation. Solution: a per-function Postgres advisory
lock (``pg_advisory_xact_lock(NS, function_id)``) with namespace
``42`` reserved for this table family.

Advisory locks are transaction-scoped (``_xact_``) so they release on
COMMIT or ROLLBACK without manual cleanup. Different functions don't
contend with each other — only writes against the *same* function get
serialized.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import (
    ActionFunction,
    ActionFunctionStatus,
    ActionFunctionVersion,
)


# Namespace key for advisory locks on the action_function_* table family.
# Picked arbitrarily; just needs to not collide with other advisory-lock
# users in the codebase. If another module starts using NS=42, change one
# of them.
ADVISORY_LOCK_NS = 42


def _take_advisory_lock(db: Session, function_id: int) -> None:
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :fid)"),
        {"ns": ADVISORY_LOCK_NS, "fid": function_id},
    )


def create_function(
    db: Session,
    *,
    author_user_id: int,
    slug: str,
    title: str,
    description: str | None,
    icon_data_url: str | None,
    code: str,
    tags: Iterable[str],
    actions_meta: list[dict],
    valves_schema: dict,
    metadata: dict,
) -> ActionFunction:
    """Atomically create a Function row + its v1 version + cache the
    version id on the parent.

    Caller is responsible for calling ``db.commit()`` after this returns
    so the transaction (and the advisory lock taken inside) holds for
    the API handler's lifetime.
    """
    fn = ActionFunction(
        slug=slug,
        title=title,
        description=description,
        icon_data_url=icon_data_url,
        author_user_id=author_user_id,
        status=ActionFunctionStatus.DRAFT,
        tags=list(tags) if tags else [],
    )
    db.add(fn)
    db.flush()  # populate fn.id before taking the lock

    _take_advisory_lock(db, fn.id)

    version = ActionFunctionVersion(
        function_id=fn.id,
        version_no=1,
        code=code,
        metadata_json=metadata,
        actions_meta_json=actions_meta,
        valves_schema_json=valves_schema,
        editor_user_id=author_user_id,
        commit_message=None,
    )
    db.add(version)
    db.flush()
    fn.latest_version_id = version.id
    db.flush()
    return fn


def save_version(
    db: Session,
    function_id: int,
    *,
    editor_user_id: int,
    code: str,
    commit_message: str | None,
    actions_meta: list[dict],
    valves_schema: dict,
    metadata: dict,
) -> ActionFunctionVersion:
    """Append a new version and update the parent's denormalized cache.

    Wraps the increment-and-insert in an advisory-lock-protected critical
    section so concurrent saves on the same function serialize cleanly.
    """
    _take_advisory_lock(db, function_id)
    next_no_row = db.execute(
        text(
            "SELECT COALESCE(MAX(version_no), 0) + 1 "
            "FROM action_function_versions WHERE function_id = :fid"
        ),
        {"fid": function_id},
    ).scalar()
    next_no = int(next_no_row or 1)

    version = ActionFunctionVersion(
        function_id=function_id,
        version_no=next_no,
        code=code,
        metadata_json=metadata,
        actions_meta_json=actions_meta,
        valves_schema_json=valves_schema,
        editor_user_id=editor_user_id,
        commit_message=commit_message,
    )
    db.add(version)
    db.flush()
    db.execute(
        text(
            "UPDATE action_functions "
            "SET latest_version_id = :vid, updated_at = now() "
            "WHERE id = :fid"
        ),
        {"vid": version.id, "fid": function_id},
    )
    return version


def get_function_by_slug(db: Session, slug: str) -> ActionFunction | None:
    return db.query(ActionFunction).filter_by(slug=slug).first()


def list_functions(
    db: Session,
    *,
    author_user_id: int | None = None,
    status: str | None = None,
    tag: str | None = None,
    q: str | None = None,
) -> list[ActionFunction]:
    query = db.query(ActionFunction)
    if author_user_id is not None:
        query = query.filter(ActionFunction.author_user_id == author_user_id)
    if status:
        query = query.filter(ActionFunction.status == status)
    if tag:
        # ARRAY contains a specific tag
        query = query.filter(ActionFunction.tags.any(tag))
    if q:
        like = f"%{q}%"
        query = query.filter(
            (ActionFunction.title.ilike(like))
            | (ActionFunction.description.ilike(like))
        )
    return query.order_by(ActionFunction.updated_at.desc()).all()


def get_latest_version(
    db: Session, function_id: int
) -> ActionFunctionVersion | None:
    return (
        db.query(ActionFunctionVersion)
        .filter(ActionFunctionVersion.function_id == function_id)
        .order_by(ActionFunctionVersion.version_no.desc())
        .first()
    )


def get_version(
    db: Session, function_id: int, version_no: int
) -> ActionFunctionVersion | None:
    return (
        db.query(ActionFunctionVersion)
        .filter(
            ActionFunctionVersion.function_id == function_id,
            ActionFunctionVersion.version_no == version_no,
        )
        .first()
    )


def list_versions(
    db: Session, function_id: int
) -> list[ActionFunctionVersion]:
    return (
        db.query(ActionFunctionVersion)
        .filter(ActionFunctionVersion.function_id == function_id)
        .order_by(ActionFunctionVersion.version_no.desc())
        .all()
    )
