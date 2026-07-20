# -*- coding: utf-8 -*-
"""Slice 2b-A — Task Service module(app.modules.tasks)測試。

依 doc 01(十值狀態機、SourceSnapshot 三規則、trace_id 必產生)、
doc 03(admin/owner bypass 必寫 audit)、doc 09(Task API:POST /api/tasks、
GET /api/tasks/{task_id};list / runs 為 Slice 2b-A 附加讀面)。

涵蓋:
- create_task happy path(trace_id、初始 draft、snapshot 三規則)
- 明確宣告無來源(origin="none")仍寫 snapshot
- 狀態機合法鏈 / 非法轉移 / 終態凍結 / 未知狀態 fail-closed
- run_sequence 遞增與 start/finish 生命週期
- ensure_task_access:requester / 他人 / admin bypass(附 audit)/ 不存在
- API:POST→GET roundtrip、401、403、list 過濾、admin user 過濾、runs
"""

from __future__ import annotations

import os
import json
from unittest.mock import AsyncMock

import pytest

# TestClient 啟動會過 startup_security 的 prod 檢查;測試環境沿用
# dev 預設 SECRET_KEY/ADMIN_PASSWORD,需明示放行(同 test_relations_api.py)。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.audit_log import AuditLog
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.modules import tasks as tasks_module
from app.schemas.contracts.tasks import (
    SnapshotOrigin,
    SourceScope,
    TaskCreate,
    TaskStatus,
)
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    """test_startup_security.py 會 mid-suite reload app.config,cache 到
    髒掉的 settings,害後續 TestClient lifespan 過不了 dev-default gate。
    這裡不 reload(reload 會「治好」下游其他測試檔、改變基線構成),改以
    monkeypatch 把 gate no-op 掉 —— 每測試自動還原,零套件級副作用。"""
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _payload(**overrides) -> TaskCreate:
    base: dict = {"title": "測試任務", "task_type": "query"}
    base.update(overrides)
    return TaskCreate(**base)


# ── create_task ───────────────────────────────────────────────────────────────

class TestCreateTask:
    def test_happy_path_trace_id_and_snapshot(self, db):
        user = make_user(db)
        task = tasks_module.create_task(
            db,
            requester_user_id=user.id,
            payload=_payload(
                source_scope="project", selected_collection_ids=[1, 2]
            ),
        )
        assert task.id is not None
        assert task.trace_id  # doc 01 驗收 2:建立必產生 trace_id
        assert task.status == TaskStatus.DRAFT.value
        assert task.requester_user_id == user.id

        snap = db.query(SourceSnapshot).filter_by(task_id=task.id).one()
        assert task.source_snapshot_id == snap.id
        assert snap.origin == SnapshotOrigin.COLLECTION.value
        assert snap.source_scope == SourceScope.PROJECT.value
        assert snap.collection_ids == [1, 2]
        # 規則 3:來源無可導分類 → 無機密
        assert snap.classification_level == "無機密"

    def test_trace_ids_unique_across_tasks(self, db):
        user = make_user(db)
        t1 = tasks_module.create_task(
            db, requester_user_id=user.id, payload=_payload()
        )
        t2 = tasks_module.create_task(
            db, requester_user_id=user.id, payload=_payload()
        )
        assert t1.trace_id != t2.trace_id

    def test_declared_none_still_writes_snapshot(self, db):
        """規則 1:無來源也要「明確宣告」——落一筆 origin=none snapshot。"""
        user = make_user(db)
        task = tasks_module.create_task(
            db, requester_user_id=user.id, payload=_payload()
        )
        snap = db.query(SourceSnapshot).filter_by(task_id=task.id).one()
        assert task.source_snapshot_id == snap.id
        assert snap.origin == SnapshotOrigin.NONE.value
        assert snap.source_scope == SourceScope.NONE.value
        assert snap.collection_ids == []
        assert snap.classification_level == "無機密"

    def test_none_scope_with_sources_rejected(self, db):
        user = make_user(db)
        with pytest.raises(ValueError):
            tasks_module.create_task(
                db,
                requester_user_id=user.id,
                payload=_payload(
                    source_scope="none", selected_collection_ids=[1]
                ),
            )

    def test_service_scope_derives_service_origin(self, db):
        user = make_user(db)
        task = tasks_module.create_task(
            db,
            requester_user_id=user.id,
            payload=_payload(
                task_type="launch_service",
                source_scope="registered_service",
                selected_service_id="svc-9",
            ),
        )
        snap = db.query(SourceSnapshot).filter_by(task_id=task.id).one()
        assert snap.origin == SnapshotOrigin.SERVICE.value

    def test_payload_classification_is_floor_for_task_not_snapshot(self, db):
        """任務分類 = max(payload 宣告, snapshot 導出);snapshot 分類
        僅由來源導出(規則 3),payload 宣告不灌入 snapshot。"""
        user = make_user(db)
        task = tasks_module.create_task(
            db,
            requester_user_id=user.id,
            payload=_payload(
                source_scope="personal",
                selected_collection_ids=[7],
                classification_level="機密",
            ),
        )
        snap = db.query(SourceSnapshot).filter_by(task_id=task.id).one()
        assert task.classification_level == "機密"
        assert snap.classification_level == "無機密"

    def test_unknown_requester_raises_lookup_error(self, db):
        with pytest.raises(LookupError):
            tasks_module.create_task(
                db, requester_user_id=999_999, payload=_payload()
            )


# ── 狀態機 ────────────────────────────────────────────────────────────────────

class TestStateMachine:
    def _task(self, db):
        user = make_user(db)
        return tasks_module.create_task(
            db, requester_user_id=user.id, payload=_payload()
        )

    def test_legal_lifecycle_chain(self, db):
        task = self._task(db)
        for status in (
            "submitted", "policy_checking", "source_resolving",
            "running", "waiting_for_user", "running", "completed",
        ):
            task = tasks_module.transition_task(db, task=task, new_status=status)
        assert task.status == "completed"

    def test_illegal_transition_raises(self, db):
        task = self._task(db)
        with pytest.raises(ValueError):
            tasks_module.transition_task(db, task=task, new_status="running")

    def test_policy_block_path(self, db):
        task = self._task(db)
        task = tasks_module.transition_task(db, task=task, new_status="submitted")
        task = tasks_module.transition_task(
            db, task=task, new_status="policy_checking"
        )
        task = tasks_module.transition_task(
            db, task=task, new_status="blocked_by_policy"
        )
        assert task.status == "blocked_by_policy"

    def test_terminal_states_frozen(self, db):
        task = self._task(db)
        task = tasks_module.transition_task(db, task=task, new_status="cancelled")
        with pytest.raises(ValueError):
            tasks_module.transition_task(db, task=task, new_status="submitted")

    def test_unknown_status_fail_closed(self, db):
        task = self._task(db)
        with pytest.raises(ValueError):
            tasks_module.transition_task(db, task=task, new_status="bogus")


# ── TaskRun 生命週期 ──────────────────────────────────────────────────────────

class TestTaskRuns:
    def _task(self, db):
        user = make_user(db)
        return tasks_module.create_task(
            db, requester_user_id=user.id, payload=_payload()
        )

    def test_run_sequence_increments_and_task_goes_running(self, db):
        task = self._task(db)
        run1 = tasks_module.start_task_run(db, task=task, dispatch_target="model")
        assert run1.run_sequence == 1
        assert run1.status == "running"
        assert run1.started_at is not None
        assert task.status == "running"  # draft → … → running 快轉

        with pytest.raises(ValueError, match="active TaskRun"):
            tasks_module.start_task_run(
                db, task=task, dispatch_target="agent"
            )

        # A retry/handoff may allocate the next sequence only after the
        # previous attempt is terminal.  Keep Task running to model the
        # coordinator's between-attempt state.
        run1.status = "failed"
        db.commit()
        run2 = tasks_module.start_task_run(db, task=task, dispatch_target="agent")
        assert run2.run_sequence == 2

    def test_finish_run_records_terminal_fields(self, db):
        task = self._task(db)
        run = tasks_module.start_task_run(db, task=task, dispatch_target="model")
        run = tasks_module.finish_task_run(
            db, task_run=run, status="failed",
            error={"code": "UPSTREAM_TIMEOUT", "message": "上游逾時"},
        )
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.error["code"] == "UPSTREAM_TIMEOUT"

    def test_finish_run_rejects_non_terminal_status(self, db):
        task = self._task(db)
        run = tasks_module.start_task_run(db, task=task, dispatch_target="model")
        with pytest.raises(ValueError):
            tasks_module.finish_task_run(db, task_run=run, status="running")
        with pytest.raises(ValueError):
            tasks_module.finish_task_run(db, task_run=run, status="bogus")

    def test_start_run_on_terminal_task_rejected(self, db):
        task = self._task(db)
        task = tasks_module.transition_task(db, task=task, new_status="cancelled")
        with pytest.raises(ValueError):
            tasks_module.start_task_run(db, task=task, dispatch_target="model")

    def test_start_run_unknown_dispatch_target_rejected(self, db):
        task = self._task(db)
        with pytest.raises(ValueError):
            tasks_module.start_task_run(db, task=task, dispatch_target="ftp")


# ── ensure_task_access ────────────────────────────────────────────────────────

class TestEnsureTaskAccess:
    def test_requester_allowed(self, db):
        user = make_user(db)
        task = tasks_module.create_task(
            db, requester_user_id=user.id, payload=_payload()
        )
        got = tasks_module.ensure_task_access(
            db, task_id=task.id, user_id=user.id
        )
        assert got.id == task.id

    def test_other_user_denied(self, db):
        alice = make_user(db, username="alice")
        bob = make_user(db, username="bob")
        task = tasks_module.create_task(
            db, requester_user_id=alice.id, payload=_payload()
        )
        with pytest.raises(PermissionError):
            tasks_module.ensure_task_access(db, task_id=task.id, user_id=bob.id)

    def test_admin_bypass_writes_audit(self, db):
        alice = make_user(db, username="alice")
        admin = make_user(db, username="root", role="admin")
        task = tasks_module.create_task(
            db, requester_user_id=alice.id, payload=_payload()
        )
        got = tasks_module.ensure_task_access(
            db, task_id=task.id, user_id=admin.id
        )
        assert got.id == task.id
        audit = (
            db.query(AuditLog)
            .filter_by(action="task.admin_access", resource_id=str(task.id))
            .one()
        )
        assert audit.actor_user_id == admin.id

    def test_missing_task_raises_lookup(self, db):
        user = make_user(db)
        with pytest.raises(LookupError):
            tasks_module.ensure_task_access(db, task_id=424242, user_id=user.id)


# ── HTTP API ──────────────────────────────────────────────────────────────────

def _auth_headers(client, db, username="alice", role="user") -> dict:
    make_user(db, username=username, role=role)
    token = login(client, username=username)
    return {"Authorization": f"Bearer {token}"}


class TestTasksApi:
    def test_post_then_get_roundtrip_and_audit(self, client, db):
        headers = _auth_headers(client, db)
        resp = client.post(
            "/api/tasks",
            json={"title": "整理報告", "task_type": "summarize"},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "draft"
        assert body["trace_id"]
        assert body["source_snapshot_id"] is not None

        got = client.get(f"/api/tasks/{body['id']}", headers=headers)
        assert got.status_code == 200
        assert got.json()["trace_id"] == body["trace_id"]

        audit = (
            db.query(AuditLog)
            .filter_by(action="task.created", resource_id=str(body["id"]))
            .one()
        )
        assert audit.status == "success"

    def test_post_unknown_task_type_422(self, client, db):
        headers = _auth_headers(client, db)
        resp = client.post(
            "/api/tasks",
            json={"title": "x", "task_type": "hack_the_planet"},
            headers=headers,
        )
        assert resp.status_code == 422

    def test_unauthenticated_401(self, client):
        assert client.get("/api/tasks").status_code == 401
        assert client.post(
            "/api/tasks", json={"title": "x", "task_type": "query"}
        ).status_code == 401

    def test_foreign_task_403(self, client, db):
        alice_headers = _auth_headers(client, db, username="alice")
        bob_headers = _auth_headers(client, db, username="bob")
        task_id = client.post(
            "/api/tasks",
            json={"title": "私人任務", "task_type": "query"},
            headers=alice_headers,
        ).json()["id"]
        resp = client.get(f"/api/tasks/{task_id}", headers=bob_headers)
        assert resp.status_code == 403

    def test_get_missing_task_404(self, client, db):
        headers = _auth_headers(client, db)
        assert client.get("/api/tasks/424242", headers=headers).status_code == 404

    def test_list_own_with_status_filter(self, client, db):
        alice_headers = _auth_headers(client, db, username="alice")
        bob_headers = _auth_headers(client, db, username="bob")
        for title in ("任務一", "任務二"):
            r = client.post(
                "/api/tasks",
                json={"title": title, "task_type": "query"},
                headers=alice_headers,
            )
            assert r.status_code == 201

        assert len(client.get("/api/tasks", headers=alice_headers).json()) == 2
        assert len(
            client.get("/api/tasks?status=draft", headers=alice_headers).json()
        ) == 2
        assert client.get(
            "/api/tasks?status=running", headers=alice_headers
        ).json() == []
        assert client.get("/api/tasks", headers=bob_headers).json() == []
        # 未知狀態 fail-closed
        assert client.get(
            "/api/tasks?status=bogus", headers=alice_headers
        ).status_code == 422

    def test_admin_may_filter_by_user_others_may_not(self, client, db):
        alice_headers = _auth_headers(client, db, username="alice")
        bob_headers = _auth_headers(client, db, username="bob")
        admin_headers = _auth_headers(client, db, username="root", role="admin")
        client.post(
            "/api/tasks",
            json={"title": "alice 的任務", "task_type": "query"},
            headers=alice_headers,
        )
        alice_id = (
            db.query(Task).filter_by(title="alice 的任務").one().requester_user_id
        )

        rows = client.get(
            f"/api/tasks?user_id={alice_id}", headers=admin_headers
        ).json()
        assert len(rows) == 1 and rows[0]["requester_user_id"] == alice_id

        resp = client.get(f"/api/tasks?user_id={alice_id}", headers=bob_headers)
        assert resp.status_code == 403

    def test_runs_endpoint(self, client, db):
        headers = _auth_headers(client, db)
        task_id = client.post(
            "/api/tasks",
            json={"title": "跑一次", "task_type": "query"},
            headers=headers,
        ).json()["id"]

        task = db.query(Task).filter_by(id=task_id).one()
        tasks_module.start_task_run(db, task=task, dispatch_target="model")

        resp = client.get(f"/api/tasks/{task_id}/runs", headers=headers)
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) == 1
        assert runs[0]["run_sequence"] == 1
        assert runs[0]["dispatch_target"] == "model"
        assert runs[0]["status"] == "running"

    def test_cancel_endpoint_signals_live_registry_and_audits(self, client, db, monkeypatch):
        headers = _auth_headers(client, db)
        task_id = client.post(
            "/api/tasks",
            json={"title": "可取消任務", "task_type": "query"},
            headers=headers,
        ).json()["id"]
        from app.services.proxy.cancellation import registry

        from app.services.proxy.cancellation import (
            CancellationDisposition,
            CancellationResult,
        )

        signal = AsyncMock(
            return_value=CancellationResult(CancellationDisposition.ACCEPTED)
        )
        monkeypatch.setattr(registry, "cancel", signal)
        response = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
        assert response.status_code == 202
        assert response.json() == {
            "task_id": task_id,
            "accepted": True,
            "status": "cancellation_requested",
        }
        signal.assert_awaited_once_with(task_id)
        audit = db.query(AuditLog).filter_by(
            action="task.cancel_requested", resource_id=str(task_id)
        ).one()
        assert json.loads(audit.metadata_json)["in_session_signal_delivered"] is True

    def test_duplicate_cancel_remains_accepted_while_in_progress(
        self, client, db, monkeypatch
    ):
        headers = _auth_headers(client, db)
        task_id = client.post(
            "/api/tasks",
            json={"title": "重複取消任務", "task_type": "query"},
            headers=headers,
        ).json()["id"]
        from app.services.proxy.cancellation import (
            CancellationDisposition,
            CancellationResult,
            registry,
        )

        signal = AsyncMock(
            side_effect=[
                CancellationResult(CancellationDisposition.ACCEPTED),
                CancellationResult(CancellationDisposition.IN_PROGRESS),
            ]
        )
        monkeypatch.setattr(registry, "cancel", signal)

        first = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)
        second = client.post(f"/api/tasks/{task_id}/cancel", headers=headers)

        assert first.json()["accepted"] is True
        assert first.json()["status"] == "cancellation_requested"
        assert second.json()["accepted"] is True
        assert second.json()["status"] == "cancellation_in_progress"
        assert signal.await_count == 2
        audits = (
            db.query(AuditLog)
            .filter_by(action="task.cancel_requested", resource_id=str(task_id))
            .order_by(AuditLog.id)
            .all()
        )
        assert [json.loads(row.metadata_json) for row in audits] == [
            {
                "cancel_disposition": "accepted",
                "in_session_signal_delivered": True,
            },
            {
                "cancel_disposition": "in_progress",
                "in_session_signal_delivered": False,
            },
        ]
