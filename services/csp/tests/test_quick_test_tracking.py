"""Recoverable test requests: synthetic SQLite, real run/ACL, no model network.

SQLite does not prove PostgreSQL row mutex behavior. The PostgreSQL statement
and lock-error contract are covered here; genuine two-Session evidence is separate.
"""

import asyncio
from copy import deepcopy
import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Query, Session

from app.api import quick_builder as api
from app.models.department import Department
from app.models.quick_builder import QuickRun, QuickVersion
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User, UserModelPermission
from app.schemas.quick_builder import TestInput
from app.services import internal_llm
from app.services import quick_execution as execution
from app.services.proxy import service as proxy_service
from tests.test_quick_builder import create, http as http, login, people as people
from tests.test_quick_builder_review import _assistant, review_rows as review_rows
from tests.test_quick_builder_review import _graph
from tests.test_quick_live_sources import live_sources as live_sources


def _run(db, user, assistant, version, nonce, message="合成提問"):
    return execution.run(
        db, user, assistant.id, version.id, message, testing=True, test_request_id=nonce
    )


def _list(db, user, assistant, version=None, nonce=None):
    return api.test_runs(
        assistant.id,
        version_id=version.id if version else None,
        request_id=UUID(str(nonce)) if nonce else None,
        user=user,
        db=db,
    )


def _assert_404(call):
    with pytest.raises(HTTPException) as raised:
        call()
    assert raised.value.status_code == 404
    assert raised.value.detail["type"] == "NOT_FOUND"


def test_running_lookup_duplicate_and_completion_preserve_server_marker(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    nonce = uuid4()
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def complete(**kwargs):
            assert not db.in_transaction(), (
                "User mutex/readtx must not cross model await"
            )
            calls.append(True)
            entered.set()
            await release.wait()
            return {
                "choices": [
                    {
                        "message": {"content": "可查回的合成回答"},
                        "finish_reason": "stop",
                    }
                ]
            }

        monkeypatch.setattr(proxy_service, "proxy_request", complete)
        task = asyncio.create_task(
            _run(db, owner, assistant, version, str(nonce).upper())
        )
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with Session(db.get_bind(), expire_on_commit=False) as reader:
                actor = reader.get(User, owner.id)
                rows = _list(reader, actor, assistant, version, nonce)["runs"]
                assert len(rows) == 1 and rows[0]["status"] == "running"
                run_id = rows[0]["id"]
                running = api.read_run(run_id, user=actor, db=reader)
                duplicate = await _run(reader, actor, assistant, version, nonce)
                assert duplicate == running
                assert duplicate["version_id"] == version.id
                assert duplicate["revision"] == 1
                assert duplicate["request_id"] == str(nonce)
                assert "request_hash" not in json.dumps(duplicate)
                assert "test_tracking" not in duplicate
                assert calls == [True]
        finally:
            release.set()
        result = await asyncio.wait_for(task, 2)
        assert result["status"] == "completed"
        assert result["id"] == run_id
        assert result["version_id"] == version.id
        return result

    result = asyncio.run(scenario())
    row = db.get(QuickRun, result["id"], populate_existing=True)
    marker = execution.test_tracking(row)
    assert marker == {
        "testing": True,
        "request_id": str(nonce),
        "request_hash": hashlib.sha256("合成提問".encode()).hexdigest(),
        "actor_department_id": owner.department_id,
    }
    again = asyncio.run(_run(db, owner, assistant, version, nonce))
    assert again == result and len(calls) == 1
    assert db.query(QuickRun).count() == 1


@pytest.mark.parametrize("conflict", ["message", "version", "assistant"])
def test_nonce_conflict_is_caller_global_before_target_lookup(
    db, review_rows, monkeypatch, conflict
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    calls = []

    async def complete(*args, **kwargs):
        calls.append(True)
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    nonce = uuid4()
    asyncio.run(_run(db, owner, assistant, version, nonce))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            execution.run(
                db,
                owner,
                assistant.id + (10000 if conflict == "assistant" else 0),
                version.id + (10000 if conflict == "version" else 0),
                "不同正文" if conflict == "message" else "合成提問",
                testing=True,
                test_request_id=nonce,
            )
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["type"] == "TEST_REQUEST_CONFLICT"
    assert len(calls) == 1 and db.query(QuickRun).count() == 1


def test_same_nonce_different_callers_independent_and_admin_cross_unit_recoverable(
    db, review_rows, monkeypatch
):
    owner, _, admin, _, _ = review_rows
    other = Department(name="合成管理員原單位")
    db.add(other)
    db.flush()
    admin.department_id = other.id
    db.commit()
    assistant, version, _ = _assistant(db, review_rows, published=False)

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    nonce = uuid4()
    first = asyncio.run(_run(db, owner, assistant, version, nonce))
    second = asyncio.run(_run(db, admin, assistant, version, nonce))
    assert first["id"] != second["id"]
    assert _list(db, admin, assistant, version, nonce)["runs"][0]["id"] == second["id"]
    assert api.read_run(second["id"], user=admin, db=db)["status"] == "completed"
    _assert_404(lambda: api.read_run(first["id"], user=admin, db=db))
    admin.department_id = owner.department_id
    db.commit()
    _assert_404(lambda: api.read_run(second["id"], user=admin, db=db))
    assert _list(db, admin, assistant, version, nonce) == {"runs": []}


@pytest.mark.parametrize(
    "revocation",
    ["model_grant", "model_disabled", "model_ceiling", "owner_unit", "inactive"],
)
def test_test_history_rechecks_current_owner_model_and_classification(
    db, review_rows, monkeypatch, revocation
):
    owner, colleague, admin, model, _ = review_rows
    assistant, version, _ = _assistant(db, review_rows, published=False)
    # The immutable version is originally authorized at a nonzero level.
    payload = deepcopy(version.payload)
    payload["classification_level"] = "營業秘密"
    second = QuickVersion(
        assistant_id=assistant.id,
        revision=2,
        payload=payload,
        payload_hash="b" * 64,
        created_by=owner.id,
        status="draft",
    )
    db.add(second)
    db.commit()

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    result = asyncio.run(_run(db, owner, assistant, second, uuid4()))
    assert result["status"] == "completed"
    _assert_404(lambda: api.read_run(result["id"], user=colleague, db=db))
    _assert_404(lambda: api.read_run(result["id"], user=admin, db=db))
    if revocation == "model_grant":
        db.query(UserModelPermission).filter_by(
            user_id=owner.id, model_id=model.id
        ).delete()
    elif revocation == "model_disabled":
        model.is_active = False
    elif revocation == "model_ceiling":
        model.classification_ceiling = "無機密"
    elif revocation == "owner_unit":
        owner.department_id = None
    else:
        owner.is_active = False
    db.commit()
    _assert_404(lambda: api.read_run(result["id"], user=owner, db=db))
    _assert_404(lambda: _list(db, owner, assistant, second))


def test_reviewer_revocation_hides_own_previous_tests(db, review_rows, monkeypatch):
    owner, reviewer, admin, model, department = review_rows
    assignment = UnitAdminAssignment(
        user_id=reviewer.id,
        department_id=department.id,
        granted_by=admin.id,
        source="manual",
    )
    db.add_all(
        [assignment, UserModelPermission(user_id=reviewer.id, model_id=model.id)]
    )
    db.commit()
    assistant, version, _ = _assistant(db, review_rows, published=False)

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    result = asyncio.run(_run(db, reviewer, assistant, version, uuid4()))
    db.delete(assignment)
    db.commit()
    _assert_404(lambda: api.read_run(result["id"], user=reviewer, db=db))
    _assert_404(lambda: _list(db, reviewer, assistant, version))
    assert owner.id != reviewer.id


def test_sql_filters_test_owner_nonce_before_limit_and_uses_row_status(
    db, review_rows, monkeypatch
):
    owner, colleague, admin, _, _ = review_rows
    assistant, version, _ = _assistant(db, review_rows, published=False)

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    nonce = uuid4()
    result = asyncio.run(_run(db, owner, assistant, version, nonce))
    stored = db.get(QuickRun, result["id"])
    marker = deepcopy(stored.result["test_tracking"])
    for index in range(30):
        db.add(
            QuickRun(
                version_id=version.id, user_id=owner.id, status="completed", result={}
            )
        )
        db.add(
            QuickRun(
                version_id=version.id,
                user_id=colleague.id,
                status="completed",
                result={"test_tracking": marker},
            )
        )
        db.add(
            QuickRun(
                version_id=version.id,
                user_id=owner.id,
                status="running",
                result={"test_tracking": {**marker, "request_id": str(uuid4())}},
            )
        )
    # Deliberately conflict JSON status/version with authoritative row fields.
    stored.status = "failed"
    stored.result = {
        **stored.result,
        "status": "completed",
        "version_id": 9999,
        "revision": 9999,
    }
    db.commit()
    exact = _list(db, owner, assistant, version, nonce)["runs"]
    assert (
        len(exact) == 1
        and exact[0]["id"] == stored.id
        and exact[0]["status"] == "failed"
    )
    assert len(_list(db, owner, assistant)["runs"]) == 20
    public = api.read_run(stored.id, user=owner, db=db)
    assert (
        public["status"] == "failed"
        and public["version_id"] == version.id
        and public["revision"] == 1
    )
    assert "test_tracking" not in public and "request_hash" not in json.dumps(public)
    legacy = db.query(QuickRun).filter_by(user_id=owner.id, result={}).first()
    assert api.read_run(legacy.id, user=admin, db=db)["status"] == "completed"


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_finish_keeps_marker_and_does_not_relabel_cancel_as_completed(
    db, review_rows, monkeypatch, terminal
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    nonce = uuid4()

    async def complete(*args, **kwargs):
        if terminal == "cancelled":
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic private exception")

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    if terminal == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_run(db, owner, assistant, version, nonce))
    else:
        assert (
            asyncio.run(_run(db, owner, assistant, version, nonce))["status"]
            == "failed"
        )
    row = db.query(QuickRun).one()
    assert row.status == terminal
    assert execution.test_tracking(row)["request_id"] == str(nonce)
    assert api.read_run(row.id, user=owner, db=db)["status"] == terminal
    duplicate = asyncio.run(_run(db, owner, assistant, version, nonce))
    assert duplicate["id"] == row.id and duplicate["status"] == terminal
    assert db.query(QuickRun).count() == 1


@pytest.mark.parametrize("changed", ["token_version", "department_id"])
def test_authenticated_baseline_cannot_be_laundered_by_user_refresh(
    db, review_rows, monkeypatch, changed
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    original = getattr(owner, changed)
    calls = []

    async def complete(*args, **kwargs):
        calls.append(True)
        return "must not be called"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    # A separate Session commits after authentication; the request still holds
    # the original authenticated ORM identity until the lock SELECT refreshes.
    with Session(db.get_bind()) as other:
        other.execute(
            update(User)
            .where(User.id == owner.id)
            .values(**{changed: original + 1 if changed == "token_version" else None})
        )
        other.commit()
    assert getattr(owner, changed) == original
    _assert_404(lambda: asyncio.run(_run(db, owner, assistant, version, uuid4())))
    assert not calls and db.query(QuickRun).count() == 0
    assert not db.in_transaction() or db.query(QuickRun).count() == 0


def test_user_lock_is_nowait_and_targets_only_user_not_joined_department(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    locks = []
    original = Query.with_for_update

    def capture(self, **kwargs):
        statement = original(self, **kwargs)
        locks.append(str(statement.statement.compile(dialect=postgresql.dialect())))
        return statement

    monkeypatch.setattr(Query, "with_for_update", capture)

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    asyncio.run(_run(db, owner, assistant, version, uuid4()))
    assert len(locks) == 1 and "FOR UPDATE OF users NOWAIT" in locks[0]


def test_lock_busy_rolls_back_and_returns_fixed_retryable_conflict(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)
    original = Query.first

    class Busy(Exception):
        pgcode = "55P03"

    def busy_on_mutex(self):
        if self._for_update_arg is not None:
            raise OperationalError("private SQL", {}, Busy("private provider error"))
        return original(self)

    monkeypatch.setattr(Query, "first", busy_on_mutex)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_run(db, owner, assistant, version, uuid4()))
    assert raised.value.status_code == 409
    assert raised.value.detail["type"] == "TEST_REQUEST_BUSY"
    assert "private" not in json.dumps(raised.value.detail)
    assert not db.in_transaction()


def test_legacy_test_request_without_uuid_is_marked_but_never_deduplicated(
    db, review_rows, monkeypatch
):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    first = asyncio.run(_run(db, owner, assistant, version, None))
    second = asyncio.run(_run(db, owner, assistant, version, None))
    assert first["id"] != second["id"] and first["request_id"] is None
    assert len(_list(db, owner, assistant)["runs"]) == 2
    parsed = TestInput(version_id=version.id, message="合成")
    assert parsed.request_id is None


@pytest.mark.parametrize("nonce", ["not-a-uuid", "", "../../guess"])
def test_invalid_uuid_rejected_by_http_schema(nonce):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TestInput(version_id=1, message="合成", request_id=nonce)


def test_public_result_strips_private_marker_even_on_sse_projection():
    projected = execution.public_result(
        1,
        {
            "status": "running",
            "test_tracking": {"request_hash": "private"},
            "execution_context": {"task_id": "private"},
        },
        [],
    )
    assert projected == {"id": 1, "status": "running", "artifacts": []}


def test_http_uuid_contract_real_auth_conflict_and_safe_read(http, people, monkeypatch):
    calls = []

    async def complete(**kwargs):
        calls.append(True)
        return {
            "choices": [
                {"message": {"content": "合成 HTTP 回答"}, "finish_reason": "stop"}
            ]
        }

    monkeypatch.setattr(proxy_service, "proxy_request", complete)
    item, _, headers = create(http, people)
    assistant_id = item["id"]
    version_id = item["versions"][0]["id"]
    nonce = str(uuid4())
    body = {
        "version_id": version_id,
        "message": "合成HTTP試跑",
        "request_id": nonce.upper(),
    }
    first = http.post(
        f"/api/quick-builder/{assistant_id}/test", json=body, headers=headers
    )
    assert first.status_code == 200
    result = first.json()
    assert result["status"] == "completed" and result["request_id"] == nonce
    repeat = http.post(
        f"/api/quick-builder/{assistant_id}/test", json=body, headers=headers
    )
    assert repeat.status_code == 200 and repeat.json() == result
    assert len(calls) == 1
    listing = http.get(
        f"/api/quick-builder/{assistant_id}/test-runs",
        params={"request_id": nonce, "version_id": version_id},
        headers=headers,
    )
    assert listing.status_code == 200 and len(listing.json()["runs"]) == 1
    row = listing.json()["runs"][0]
    assert row["id"] == result["id"] and row["revision"] == 1
    read = http.get(f"/api/quick-builder/runs/{row['id']}", headers=headers)
    assert read.status_code == 200 and read.json() == result
    assert "request_hash" not in read.text and "test_tracking" not in read.text
    conflict = http.post(
        f"/api/quick-builder/{assistant_id}/test",
        json={**body, "message": "不同內容"},
        headers=headers,
    )
    assert conflict.status_code == 409
    for method, path, kwargs in (
        (
            "post",
            f"/api/quick-builder/{assistant_id}/test",
            {"json": {**body, "request_id": "invalid-secret-value"}},
        ),
        (
            "get",
            f"/api/quick-builder/{assistant_id}/test-runs",
            {"params": {"request_id": "invalid-secret-value"}},
        ),
    ):
        invalid = getattr(http, method)(path, headers=headers, **kwargs)
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["type"] == "INVALID_INPUT"
        assert "invalid-secret-value" not in invalid.text
    for name in ("outsider", "administrator"):
        outsider_headers = login(http, name)
        denied = http.get(
            f"/api/quick-builder/runs/{row['id']}", headers=outsider_headers
        )
        assert denied.status_code == 404
    # Cookie/token identity changed silently in another tab: the optional
    # expected actor protects both recovery GETs, without changing legacy GETs.
    administrator_headers = login(http, "administrator")
    original_actor_id = people[0]["creator"].id
    for path in (
        f"/api/quick-builder/runs/{row['id']}",
        f"/api/quick-builder/{assistant_id}/test-runs",
    ):
        changed = http.get(
            path,
            headers={
                **administrator_headers,
                "X-ANILA-Actor-Id": str(original_actor_id),
            },
        )
        assert changed.status_code == 409
        assert changed.json()["detail"]["type"] == "IDENTITY_CHANGED"
        assert "request_id" not in changed.text and "runs" not in changed.json()
    valid_actor_headers = {**headers, "X-ANILA-Actor-Id": str(original_actor_id)}
    assert (
        http.get(
            f"/api/quick-builder/runs/{row['id']}", headers=valid_actor_headers
        ).status_code
        == 200
    )
    for expected in ("9" * 5000, "0" + str(original_actor_id), "not-an-actor"):
        for path in (
            f"/api/quick-builder/runs/{row['id']}",
            f"/api/quick-builder/{assistant_id}/test-runs",
        ):
            invalid_actor = http.get(
                path, headers={**headers, "X-ANILA-Actor-Id": expected}
            )
            assert invalid_actor.status_code == 409
            assert invalid_actor.json()["detail"]["type"] == "IDENTITY_CHANGED"


def test_revoked_nonce_conflicts_do_not_reveal_prior_run(db, review_rows, monkeypatch):
    owner = review_rows[0]
    assistant, version, _ = _assistant(db, review_rows, published=False)

    async def complete(*args, **kwargs):
        return "合成回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    nonce = uuid4()
    asyncio.run(_run(db, owner, assistant, version, nonce))
    assistant.disabled = True
    db.commit()
    _assert_404(
        lambda: asyncio.run(_run(db, owner, assistant, version, nonce, "已變更正文"))
    )


@pytest.mark.parametrize("change", ["publication", "classification", "disabled"])
def test_recovered_tests_recheck_current_collection_access(
    db, live_sources, monkeypatch, change
):
    rows = live_sources
    version = QuickVersion(
        assistant_id=rows.assistant.id,
        revision=1,
        created_by=rows.reader.id,
        agent_id=rows.agent.id,
        payload_hash="c" * 64,
        status="draft",
        payload={
            "name": "來源撤權試跑",
            "model_id": rows.model.id,
            "classification_level": "無機密",
            "collection_ids": [rows.coll.id],
            "scope": "unit",
            "target_department_ids": [],
            "tool_fingerprints": {},
            "graph": _graph({"id": "model", "kind": "model"}),
        },
    )
    db.add(version)
    db.commit()

    async def complete(*args, **kwargs):
        return "合成授權來源回答"

    monkeypatch.setattr(internal_llm, "complete_chat", complete)
    nonce = uuid4()
    result = asyncio.run(_run(db, rows.reader, rows.assistant, version, nonce))
    assert result["status"] == "completed"
    if change == "publication":
        rows.publication.scope = "unit"
    elif change == "classification":
        rows.coll.classification_level = "密"
    else:
        rows.coll.status = "disabled"
    db.commit()
    _assert_404(lambda: api.read_run(result["id"], user=rows.reader, db=db))
    _assert_404(lambda: _list(db, rows.reader, rows.assistant, version, nonce))
    _assert_404(
        lambda: asyncio.run(_run(db, rows.reader, rows.assistant, version, nonce))
    )
