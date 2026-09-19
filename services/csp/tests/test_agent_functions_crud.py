"""Agent functions CRUD 權限與 404（functions.py 先前零測試）。

寫入閘門：agent owner 或 admin tier。讀取閘門：owner / admin / 有
UserAgentPermission 的使用者。非授權與不存在的 agent 一律 404
（與 models 列舉塌縮同一套，不是 403）。

不走 TestClient：沙箱 lifespan 會卡住。直接呼叫 handler + db fixture。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.agents import functions as functions_api
from app.api.agents.functions import AgentFunctionCreate, AgentFunctionUpdate
from app.models.agent import UserAgentPermission
from tests.conftest import make_agent, make_user


class _Req:
    headers: dict = {}
    client = None


@pytest.fixture(autouse=True)
def _quiet_audit(monkeypatch):
    monkeypatch.setattr(functions_api, "log_audit_event", lambda *a, **k: 1)


def _create_payload(**overrides) -> AgentFunctionCreate:
    data = {
        "kind": "preset_prompt",
        "label": "摘要",
        "config": {"text": "請摘要這段", "autosend": False},
        "sort_order": 1,
    }
    data.update(overrides)
    return AgentFunctionCreate(**data)


def test_owner_lists_create_update_delete(db):
    owner = make_user(db, username="fn_owner", role="developer")
    agent = make_agent(db, owner, name="fn-owner-agent")

    empty = functions_api.list_agent_functions(
        agent_ref=str(agent.id), current_user=owner, db=db
    )
    assert empty == []

    created = functions_api.create_agent_function(
        agent_ref=agent.name,
        payload=_create_payload(),
        http_request=_Req(),
        current_user=owner,
        db=db,
    )
    assert created["agent_id"] == agent.id
    assert created["kind"] == "preset_prompt"
    assert created["label"] == "摘要"
    assert created["config"]["text"] == "請摘要這段"
    fn_id = created["id"]

    listing = functions_api.list_agent_functions(
        agent_ref=str(agent.id), current_user=owner, db=db
    )
    assert [item["id"] for item in listing] == [fn_id]

    updated = functions_api.update_agent_function(
        agent_ref=str(agent.id),
        function_id=fn_id,
        payload=AgentFunctionUpdate(label="改寫摘要", sort_order=3),
        current_user=owner,
        db=db,
    )
    assert updated["label"] == "改寫摘要"
    assert updated["sort_order"] == 3
    assert updated["kind"] == "preset_prompt"

    functions_api.delete_agent_function(
        agent_ref=str(agent.id),
        function_id=fn_id,
        current_user=owner,
        db=db,
    )
    after = functions_api.list_agent_functions(
        agent_ref=str(agent.id), current_user=owner, db=db
    )
    assert after == []


def test_admin_can_edit_foreign_agent_functions(db):
    owner = make_user(db, username="fn_owned", role="developer")
    admin = make_user(db, username="fn_admin", role="admin")
    agent = make_agent(db, owner, name="fn-admin-agent")

    created = functions_api.create_agent_function(
        agent_ref=str(agent.id),
        payload=_create_payload(label="管理員新增"),
        http_request=_Req(),
        current_user=admin,
        db=db,
    )
    assert created["label"] == "管理員新增"
    assert created["agent_id"] == agent.id


def test_stranger_crud_collapses_to_404(db):
    owner = make_user(db, username="fn_real_owner", role="developer")
    stranger = make_user(db, username="fn_stranger", role="developer")
    agent = make_agent(db, owner, name="fn-secret-agent")
    created = functions_api.create_agent_function(
        agent_ref=str(agent.id),
        payload=_create_payload(label="不公開"),
        http_request=_Req(),
        current_user=owner,
        db=db,
    )
    fn_id = created["id"]

    with pytest.raises(HTTPException) as listed:
        functions_api.list_agent_functions(
            agent_ref=str(agent.id), current_user=stranger, db=db
        )
    with pytest.raises(HTTPException) as created_as_stranger:
        functions_api.create_agent_function(
            agent_ref=str(agent.id),
            payload=_create_payload(label="偷加"),
            http_request=_Req(),
            current_user=stranger,
            db=db,
        )
    with pytest.raises(HTTPException) as updated:
        functions_api.update_agent_function(
            agent_ref=str(agent.id),
            function_id=fn_id,
            payload=AgentFunctionUpdate(label="偷改"),
            current_user=stranger,
            db=db,
        )
    with pytest.raises(HTTPException) as deleted:
        functions_api.delete_agent_function(
            agent_ref=str(agent.id),
            function_id=fn_id,
            current_user=stranger,
            db=db,
        )
    for exc in (listed, created_as_stranger, updated, deleted):
        assert exc.value.status_code == 404
        assert exc.value.detail == "Agent 不存在"


def test_missing_agent_is_404(db):
    user = make_user(db, username="fn_missing_agent", role="developer")
    with pytest.raises(HTTPException) as by_id:
        functions_api.list_agent_functions(
            agent_ref="999999", current_user=user, db=db
        )
    with pytest.raises(HTTPException) as by_name:
        functions_api.list_agent_functions(
            agent_ref="no-such-agent", current_user=user, db=db
        )
    assert by_id.value.status_code == 404
    assert by_name.value.status_code == 404
    assert by_id.value.detail == by_name.value.detail == "Agent 不存在"


def test_missing_function_is_404_for_owner(db):
    owner = make_user(db, username="fn_missing_fn", role="developer")
    agent = make_agent(db, owner, name="fn-missing-fn-agent")
    with pytest.raises(HTTPException) as updated:
        functions_api.update_agent_function(
            agent_ref=str(agent.id),
            function_id=404404,
            payload=AgentFunctionUpdate(label="沒這筆"),
            current_user=owner,
            db=db,
        )
    with pytest.raises(HTTPException) as deleted:
        functions_api.delete_agent_function(
            agent_ref=str(agent.id),
            function_id=404404,
            current_user=owner,
            db=db,
        )
    assert updated.value.status_code == 404
    assert deleted.value.status_code == 404
    assert updated.value.detail == "功能不存在"
    assert deleted.value.detail == "功能不存在"


def test_permitted_user_can_list_but_not_write(db):
    owner = make_user(db, username="fn_perm_owner", role="developer")
    reader = make_user(db, username="fn_perm_reader", role="user")
    agent = make_agent(db, owner, name="fn-perm-agent")
    db.add(UserAgentPermission(user_id=reader.id, agent_id=agent.id))
    db.commit()

    created = functions_api.create_agent_function(
        agent_ref=str(agent.id),
        payload=_create_payload(label="給使用者看"),
        http_request=_Req(),
        current_user=owner,
        db=db,
    )
    listed = functions_api.list_agent_functions(
        agent_ref=str(agent.id), current_user=reader, db=db
    )
    assert listed[0]["id"] == created["id"]

    with pytest.raises(HTTPException) as wrote:
        functions_api.create_agent_function(
            agent_ref=str(agent.id),
            payload=_create_payload(label="使用者不該能寫"),
            http_request=_Req(),
            current_user=reader,
            db=db,
        )
    assert wrote.value.status_code == 404
    assert wrote.value.detail == "Agent 不存在"
