"""測試聊天每個 await 邊界都重驗目前主管權限。"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import UserModelPermission
from app.services import internal_llm
from app.services.quick_execution import run
from tests.test_quick_builder_review import (
    _assistant,
    _graph,
    review_rows as review_rows,
)


@pytest.mark.parametrize("node_count", [1, 2])
def test_supervisor_revocation_stops_test_run(db, review_rows, monkeypatch, node_count):
    _, supervisor, admin, model, department = review_rows
    assignment = UnitAdminAssignment(
        user_id=supervisor.id,
        department_id=department.id,
        granted_by=admin.id,
        source="manual",
    )
    db.add_all(
        [assignment, UserModelPermission(user_id=supervisor.id, model_id=model.id)]
    )
    db.commit()
    assistant, version, _ = _assistant(
        db,
        review_rows,
        published=False,
        graph=_graph(
            *({"id": f"model{index}", "kind": "model"} for index in range(node_count))
        ),
    )
    calls = []

    async def complete(*args, **kwargs):
        calls.append(True)
        assignment.revoked_at = datetime.now(timezone.utc)
        db.commit()
        return "合成回覆"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    result = asyncio.run(
        run(db, supervisor, assistant.id, version.id, "合成提問", testing=True)
    )
    assert result["status"] == "failed"
    assert len(calls) == 1
    assert result["error"]["type"] == "EXECUTION_FAILED"
