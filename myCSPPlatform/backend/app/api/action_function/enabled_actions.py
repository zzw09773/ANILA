"""``GET /api/functions/enabled-actions`` — flat actions list for the
ChatRuntime toolbar.

Returns one row per declared action across all enabled functions, so
the frontend can render buttons without making N round-trips per
function. Cached client-side; invalidated on save / status change.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import (
    ActionFunction,
    ActionFunctionStatus,
    ActionFunctionVersion,
    User,
)
from app.schemas.action_function import (
    EnabledAction,
    EnabledActionsResponse,
)


router = APIRouter(prefix="/api/functions", tags=["action-functions"])


@router.get(
    "/enabled-actions", response_model=EnabledActionsResponse
)
def enabled_actions(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),  # auth required, role agnostic
):
    rows = (
        db.query(ActionFunction, ActionFunctionVersion)
        .join(
            ActionFunctionVersion,
            ActionFunction.latest_version_id == ActionFunctionVersion.id,
        )
        .filter(ActionFunction.status == ActionFunctionStatus.ENABLED)
        .all()
    )
    actions: list[EnabledAction] = []
    for fn, version in rows:
        for a in (version.actions_meta_json or []):
            actions.append(
                EnabledAction(
                    function_slug=fn.slug,
                    action_id=a["id"],
                    name=a["name"],
                    icon_data_url=a.get("icon_url"),
                    function_version=version.version_no,
                )
            )
    return EnabledActionsResponse(actions=actions)
