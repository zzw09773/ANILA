# -*- coding: utf-8 -*-
"""Slice 2b-B — Policy Engine「裁決紀錄」模組測試(recording only)。

依 doc 03 §5(PolicyDecision 九動作 enum、decision 三值、Done Criteria 4:
所有 deny 必有可解釋原因)與 doc 08 §10(classification ceiling 判定式)。
本 slice 只做紀錄(record)與純函式 ceiling helper;完整規則引擎在
Slice 3 / 6。

涵蓋:
- record_decision happy path(預設值、metadata、task 掛載、round-trip)
- fail-closed:非法 action / decision / actor_type → ValueError
- deny 必附 reason(doc 03 Done Criteria 4,service 層強制)
- append-only:模組公開面不得暴露任何改寫 / 刪除 API
- evaluate_classification_ceiling 全 5 級 × (5 ceiling + None) 真值表
- API:admin 可列表 + 過濾(action / task_id / decision / 時間範圍 / 分頁),
  非 admin 403,未登入 401
"""

from __future__ import annotations

import importlib
import os
from datetime import datetime, timedelta, timezone

import pytest

# TestClient 進 lifespan 會跑 startup_security 檢查;測試環境沿
# test_phase1_integration_smoke.py 慣例放行 dev 預設 secret。
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.policy_decision import PolicyDecision
from app.modules.policy import evaluate_classification_ceiling, record_decision
from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyActorType,
    PolicyDecisionOut,
    PolicyDecisionVerdict,
)
from tests.conftest import make_user

LEVELS = [level.value for level in ClassificationLevel]


def _record_allow(db, **overrides) -> PolicyDecision:
    fields = dict(
        action=PolicyAction.TASK_RUN.value,
        resource_type="task",
        resource_id="1",
        decision=PolicyDecisionVerdict.ALLOW.value,
        actor_type=PolicyActorType.USER.value,
        actor_id="1",
    )
    fields.update(overrides)
    return record_decision(db, **fields)


def _auth_headers(client, username: str) -> dict:
    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── record_decision ───────────────────────────────────────────────────────────


class TestRecordDecision:
    def test_happy_path_persists_row_with_defaults(self, db):
        user = make_user(db)
        row = _record_allow(
            db,
            actor_id=str(user.id),
            reason="clearance >= resource level",
            metadata={"source": "unit-test"},
        )
        assert row.id is not None
        assert row.action == "task.run"
        assert row.resource_type == "task"
        assert row.resource_id == "1"
        assert row.decision == "allow"
        assert row.actor_type == "user"
        assert row.actor_id == user.id
        assert row.task_id is None
        assert row.reason == "clearance >= resource level"
        assert row.matched_policy_ids == []
        assert row.policy_version == "r1"
        assert row.metadata_json == {"source": "unit-test"}
        assert row.classification_level == ClassificationLevel.UNCLASSIFIED.value
        assert row.created_at is not None
        # 落地後可由封閉契約 round-trip(fail-closed 驗證同一份資料)
        out = PolicyDecisionOut.model_validate(row)
        assert out.action is PolicyAction.TASK_RUN
        assert out.decision is PolicyDecisionVerdict.ALLOW

    def test_row_is_committed_durable(self, db):
        _record_allow(db, actor_id=str(make_user(db).id))
        db.rollback()  # 未殘留未提交交易 → rollback 後列仍在
        assert db.query(PolicyDecision).count() == 1

    def test_task_id_and_matched_policy_ids_recorded(self, db):
        user = make_user(db)
        row = record_decision(
            db,
            action=PolicyAction.MODEL_INVOKE.value,
            resource_type="model",
            resource_id="7",
            decision=PolicyDecisionVerdict.DENY.value,
            actor_type=PolicyActorType.SERVICE.value,
            actor_id=str(user.id),
            task_id=None,
            reason="classification ceiling exceeded",
            matched_policy_ids=["ceiling.model"],
            policy_version="r1",
            metadata=None,
        )
        assert row.decision == "deny"
        assert row.matched_policy_ids == ["ceiling.model"]

    def test_invalid_action_raises_and_persists_nothing(self, db):
        with pytest.raises(ValueError):
            _record_allow(db, action="task.launch-missiles")
        assert db.query(PolicyDecision).count() == 0

    def test_invalid_decision_raises_and_persists_nothing(self, db):
        with pytest.raises(ValueError):
            _record_allow(db, decision="maybe")
        assert db.query(PolicyDecision).count() == 0

    def test_invalid_actor_type_raises(self, db):
        with pytest.raises(ValueError):
            _record_allow(db, actor_type="robot")
        assert db.query(PolicyDecision).count() == 0

    def test_deny_requires_reason(self, db):
        # doc 03 Done Criteria 4:所有 policy deny 必有可解釋原因
        with pytest.raises(ValueError):
            _record_allow(db, decision=PolicyDecisionVerdict.DENY.value,
                          reason=None)
        with pytest.raises(ValueError):
            _record_allow(db, decision=PolicyDecisionVerdict.DENY.value,
                          reason="   ")
        assert db.query(PolicyDecision).count() == 0

    def test_non_numeric_actor_id_preserved_in_metadata(self, db):
        row = _record_allow(db, actor_id="csk-router-1")
        assert row.actor_id is None
        assert row.metadata_json["actor_id_raw"] == "csk-router-1"

    def test_caller_metadata_dict_not_mutated(self, db):
        metadata = {"k": "v"}
        _record_allow(db, actor_id="not-a-number", metadata=metadata)
        assert metadata == {"k": "v"}

    def test_caller_matched_policy_ids_not_aliased(self, db):
        ids = ["p1"]
        row = _record_allow(db, reason="r", matched_policy_ids=ids)
        ids.append("p2")
        assert row.matched_policy_ids == ["p1"]


# ── append-only 公開面 ────────────────────────────────────────────────────────


class TestAppendOnlySurface:
    MUTATOR_MARKERS = ("update", "delete", "modify", "purge", "revoke",
                       "overwrite", "edit", "remove")

    def test_package_public_surface_exact(self):
        policy = importlib.import_module("app.modules.policy")
        # Slice 3a 加入四級分類 latch core(apply/effective/降級申請三件組
        # + 權責查核 hook);仍無任何 mutator 命名(append-only 面不變)。
        assert set(policy.__all__) == {
            "apply_classification",
            "create_declassification_request",
            "decide_declassification",
            "effective_level",
            "evaluate_classification_ceiling",
            "has_declassification_authority",
            "record_decision",
            "router",
        }

    def test_no_mutator_exposed_anywhere(self):
        for module_name in ("app.modules.policy",
                            "app.modules.policy.service",
                            "app.modules.policy.router"):
            module = importlib.import_module(module_name)
            public = [n for n in dir(module) if not n.startswith("_")]
            offenders = [
                name for name in public
                if any(marker in name.lower()
                       for marker in self.MUTATOR_MARKERS)
            ]
            assert offenders == [], f"{module_name} 暴露疑似改寫 API:{offenders}"

    def test_update_and_delete_absent_by_name(self):
        policy = importlib.import_module("app.modules.policy")
        for absent in ("update_decision", "delete_decision"):
            with pytest.raises(AttributeError):
                getattr(policy, absent)


# ── evaluate_classification_ceiling 真值表 ────────────────────────────────────


class TestClassificationCeiling:
    @pytest.mark.parametrize("task_level", LEVELS)
    @pytest.mark.parametrize("ceiling", [None] + LEVELS)
    def test_truth_table(self, task_level, ceiling):
        expected = (
            ceiling is None
            or ClassificationLevel(task_level).rank
            <= ClassificationLevel(ceiling).rank
        )
        assert evaluate_classification_ceiling(
            task_level=task_level, ceiling=ceiling,
        ) is expected

    def test_unknown_task_level_fail_closed(self):
        with pytest.raises(ValueError):
            evaluate_classification_ceiling(task_level="公開", ceiling=None)

    def test_unknown_ceiling_fail_closed(self):
        with pytest.raises(ValueError):
            evaluate_classification_ceiling(
                task_level=ClassificationLevel.UNCLASSIFIED.value,
                ceiling="不是等級",
            )


# ── GET /api/policy-decisions ─────────────────────────────────────────────────


class TestPolicyDecisionsApi:
    def _seed(self, db) -> tuple[int, int]:
        """回傳 (admin_id, deny_row_id 掛的 task 佔位 id)。"""
        admin = make_user(db, "boss", role="admin")
        make_user(db, "mallory", role="user")
        _record_allow(db, actor_id=str(admin.id), reason="ok")
        record_decision(
            db,
            action=PolicyAction.AGENT_INVOKE.value,
            resource_type="agent",
            resource_id="42",
            decision=PolicyDecisionVerdict.DENY.value,
            actor_type=PolicyActorType.SERVICE.value,
            actor_id=str(admin.id),
            reason="ceiling exceeded",
            matched_policy_ids=["ceiling.agent"],
        )
        return admin.id, 42

    def test_unauthenticated_401(self, client, db):
        resp = client.get("/api/policy-decisions")
        assert resp.status_code == 401

    def test_non_admin_403(self, client, db):
        self._seed(db)
        headers = _auth_headers(client, "mallory")
        resp = client.get("/api/policy-decisions", headers=headers)
        assert resp.status_code == 403

    def test_admin_lists_newest_first(self, client, db):
        self._seed(db)
        headers = _auth_headers(client, "boss")
        resp = client.get("/api/policy-decisions", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [item["action"] for item in body] == ["agent.invoke", "task.run"]
        assert body[0]["decision"] == "deny"
        assert body[0]["matched_policy_ids"] == ["ceiling.agent"]

    def test_filter_by_action_and_decision(self, client, db):
        self._seed(db)
        headers = _auth_headers(client, "boss")
        resp = client.get("/api/policy-decisions",
                          params={"action": "agent.invoke"}, headers=headers)
        assert [item["action"] for item in resp.json()] == ["agent.invoke"]
        resp = client.get("/api/policy-decisions",
                          params={"decision": "allow"}, headers=headers)
        assert [item["decision"] for item in resp.json()] == ["allow"]

    def test_filter_by_invalid_action_422(self, client, db):
        self._seed(db)
        headers = _auth_headers(client, "boss")
        resp = client.get("/api/policy-decisions",
                          params={"action": "not.an.action"}, headers=headers)
        assert resp.status_code == 422

    def test_filter_by_task_id(self, client, db):
        admin = make_user(db, "boss", role="admin")
        from app.models.task import Task  # 只作測試佈景,非模組依賴
        task = Task(title="t", task_type="query", requester_user_id=admin.id)
        db.add(task)
        db.commit()
        _record_allow(db, actor_id=str(admin.id), task_id=task.id)
        _record_allow(db, actor_id=str(admin.id))
        headers = _auth_headers(client, "boss")
        resp = client.get("/api/policy-decisions",
                          params={"task_id": task.id}, headers=headers)
        body = resp.json()
        assert len(body) == 1
        assert body[0]["task_id"] == task.id

    def test_filter_by_time_range(self, client, db):
        self._seed(db)
        headers = _auth_headers(client, "boss")
        now = datetime.now(timezone.utc)

        def _iso(dt: datetime) -> str:
            return dt.isoformat().replace("+00:00", "Z")

        wide = client.get(
            "/api/policy-decisions",
            params={"created_from": _iso(now - timedelta(days=1)),
                    "created_to": _iso(now + timedelta(days=1))},
            headers=headers,
        )
        assert len(wide.json()) == 2
        future = client.get(
            "/api/policy-decisions",
            params={"created_from": _iso(now + timedelta(hours=1))},
            headers=headers,
        )
        assert future.json() == []

    def test_pagination_limit_offset(self, client, db):
        self._seed(db)
        headers = _auth_headers(client, "boss")
        page1 = client.get("/api/policy-decisions",
                           params={"limit": 1}, headers=headers)
        page2 = client.get("/api/policy-decisions",
                           params={"limit": 1, "offset": 1}, headers=headers)
        assert len(page1.json()) == 1
        assert len(page2.json()) == 1
        assert page1.json()[0]["id"] != page2.json()[0]["id"]
