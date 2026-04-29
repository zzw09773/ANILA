"""Tests for ANILA Functions audit retention purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    ActionFunctionRun,
    ActionFunctionRunContext,
    ActionFunctionRunStatus,
)
from app.services.action_function.retention import (
    DEFAULT_RETENTION_DAYS,
    purge_expired_runs,
)


def _make_run(*, started_at: datetime) -> ActionFunctionRun:
    return ActionFunctionRun(
        function_id=1,
        version_no=1,
        action_id="x",
        triggered_by_user_id=1,
        context_type=ActionFunctionRunContext.CHAT_MESSAGE,
        status=ActionFunctionRunStatus.SUCCESS,
        started_at=started_at,
    )


@pytest.mark.usefixtures("db_session")
def test_purge_removes_old_runs(db_session) -> None:
    now = datetime.now(timezone.utc)
    old = _make_run(started_at=now - timedelta(days=DEFAULT_RETENTION_DAYS + 40))
    new = _make_run(started_at=now - timedelta(days=10))
    db_session.add_all([old, new])
    db_session.commit()

    deleted = purge_expired_runs(db_session)
    assert deleted == 1

    remaining = db_session.query(ActionFunctionRun).all()
    assert len(remaining) == 1
    assert remaining[0].started_at == new.started_at


@pytest.mark.usefixtures("db_session")
def test_purge_with_custom_window(db_session) -> None:
    now = datetime.now(timezone.utc)
    db_session.add_all(
        [
            _make_run(started_at=now - timedelta(days=20)),
            _make_run(started_at=now - timedelta(days=5)),
        ]
    )
    db_session.commit()

    deleted = purge_expired_runs(db_session, retention_days=10)
    assert deleted == 1
