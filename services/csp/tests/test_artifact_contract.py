# -*- coding: utf-8 -*-
"""Slice 8a — CSP artifact contract(doc 02 ArtifactJob、doc 01 Artifact/
Version/Export、doc 08 §5/§10、doc 10 §12 邊界)。

鎖住凍結的 wire 契約(平行 worker 據此建 Studio 端):

- ``POST /v1/artifact-jobs``(service token)—— 冪等 upsert(job_id)。
- ``PATCH /v1/artifact-jobs/{job_id}``(service token)—— 狀態機(非法轉移
  409、未知值 422、查無 404)。
- ``POST /v1/artifacts``(service token)—— binding 規則 422(constitution
  §6)、分類繼承 effective = max(explicit, task, snapshot)(單向,不降級)。
- ``POST /v1/artifacts/{id}/versions``(service token)—— 版本遞增 + 繼承重驗。
- ``POST /v1/artifacts/{id}/exports``(user JWT / service token)—— 匯出
  policy gate:allow if target_floor >= artifact.level;deny → 403 + deny
  PolicyDecision、不落 allow 匯出列(doc 00 §6)。
- ``GET /api/artifacts`` + ``/{id}``(user JWT)—— owner-scope 治理讀面。

/v1 寫入面僅接受 service token:使用者 JWT → 403(不開放使用者直建
artifact/job);匿名 → 401。
"""

from __future__ import annotations

import os

# Same house pattern as test_trace_endpoints.py / test_proxy_task_wiring.py:
# startup_security blocks dev default secrets in production mode — allow in tests.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import Artifact, ArtifactJob, ArtifactVersion, ExportRecord
from app.models.classification import ClassificationEvent
from app.models.policy_decision import PolicyDecision
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.utils.security import create_access_token
from tests.conftest import make_user

SVC_TOKEN = "svc-slice8a-test-token"
_SVC = {"X-CSP-Service-Token": SVC_TOKEN}


@pytest.fixture(autouse=True)
def _svc_token(monkeypatch):
    """本模組內把 legacy CSP_SERVICE_TOKEN 設成已知值,供 /v1 service 面測試
    (function-scoped,測完自動還原,不外洩到別模組)。"""
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", SVC_TOKEN)
    yield


def _bearer(user) -> dict:
    token = create_access_token({
        "sub": str(user.id), "username": user.username,
        "role": user.role, "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _make_task(db: Session, user, level: str = "無機密") -> Task:
    task = Task(title="測試任務", task_type="query", requester_user_id=user.id,
                status="submitted", classification_level=level)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _job_body(job_id: str, **over) -> dict:
    body = {"job_id": job_id, "artifact_type": "report", "requester_user_id": 1}
    body.update(over)
    return body


def _artifact_body(**over) -> dict:
    body = {"artifact_type": "report", "title": "季報",
            "storage_ref": "store://artifacts/r.pdf"}
    body.update(over)
    return body


def _register_artifact(client, *, task_id=None, snapshot_id=None, level=None,
                       atype="report"):
    body = _artifact_body(artifact_type=atype)
    if task_id is not None:
        body["task_id"] = task_id
    if snapshot_id is not None:
        body["source_snapshot_id"] = snapshot_id
    if level is not None:
        body["classification_level"] = level
    return client.post("/v1/artifacts", headers=_SVC, json=body)


# ── ArtifactJob:冪等 upsert + 狀態機 ─────────────────────────────────────────


class TestJobContract:
    def test_register_job_idempotent_upsert(self, client: TestClient, db: Session):
        user = make_user(db, username="job_owner")
        r1 = client.post("/v1/artifact-jobs", headers=_SVC,
                         json=_job_body("job-abc", requester_user_id=user.id))
        assert r1.status_code == 201, r1.text
        assert r1.json()["job_id"] == "job-abc"
        assert r1.json()["owner_user_id"] == user.id
        # 再 POST 同 job_id(改 type)→ 覆寫,不新增列。
        r2 = client.post("/v1/artifact-jobs", headers=_SVC,
                         json=_job_body("job-abc", requester_user_id=user.id,
                                        artifact_type="slides"))
        assert r2.status_code == 201, r2.text
        assert r2.json()["artifact_type"] == "slides"
        assert db.query(ArtifactJob).count() == 1

    def test_register_job_resolves_employee_id(self, client: TestClient, db: Session):
        # 員編騎在 username;employee_id 可解析 owner。
        user = make_user(db, username="600123")
        resp = client.post(
            "/v1/artifact-jobs", headers=_SVC,
            json={"job_id": "job-emp", "artifact_type": "mindmap",
                  "employee_id": "600123"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_user_id"] == user.id
        assert resp.json()["requester_employee_id"] == "600123"

    def test_job_missing_requester_422(self, client: TestClient, db: Session):
        resp = client.post("/v1/artifact-jobs", headers=_SVC,
                           json={"job_id": "j", "artifact_type": "report"})
        assert resp.status_code == 422

    def test_job_status_transitions(self, client: TestClient, db: Session):
        user = make_user(db, username="job_tr")
        client.post("/v1/artifact-jobs", headers=_SVC,
                    json=_job_body("job-tr", requester_user_id=user.id))
        r_run = client.patch("/v1/artifact-jobs/job-tr", headers=_SVC,
                             json={"status": "running", "progress": 40})
        assert r_run.status_code == 200, r_run.text
        assert r_run.json()["status"] == "running"
        assert r_run.json()["progress"] == 40
        r_done = client.patch(
            "/v1/artifact-jobs/job-tr", headers=_SVC,
            json={"status": "completed", "artifact_files": [{"path": "r.pdf"}]},
        )
        assert r_done.status_code == 200
        assert r_done.json()["status"] == "completed"

    def test_illegal_transition_from_terminal_409(self, client: TestClient, db: Session):
        user = make_user(db, username="job_term")
        client.post("/v1/artifact-jobs", headers=_SVC,
                    json=_job_body("job-term", requester_user_id=user.id))
        client.patch("/v1/artifact-jobs/job-term", headers=_SVC,
                     json={"status": "completed"})
        # completed 為終態,回退 running → 409。
        resp = client.patch("/v1/artifact-jobs/job-term", headers=_SVC,
                            json={"status": "running"})
        assert resp.status_code == 409

    def test_unknown_status_value_422(self, client: TestClient, db: Session):
        user = make_user(db, username="job_bad")
        client.post("/v1/artifact-jobs", headers=_SVC,
                    json=_job_body("job-bad", requester_user_id=user.id))
        resp = client.patch("/v1/artifact-jobs/job-bad", headers=_SVC,
                            json={"status": "bogus"})
        assert resp.status_code == 422

    def test_patch_unknown_job_404(self, client: TestClient, db: Session):
        make_user(db, username="job_404")
        resp = client.patch("/v1/artifact-jobs/nope", headers=_SVC,
                            json={"status": "running"})
        assert resp.status_code == 404


# ── service-token-only enforcement(/v1 寫入面)───────────────────────────────


class TestServiceTokenOnly:
    def test_job_register_user_jwt_403(self, client: TestClient, db: Session):
        user = make_user(db, username="jwt_job")
        resp = client.post("/v1/artifact-jobs", headers=_bearer(user),
                           json=_job_body("job-jwt", requester_user_id=user.id))
        assert resp.status_code == 403

    def test_artifact_register_user_jwt_403(self, client: TestClient, db: Session):
        user = make_user(db, username="jwt_art")
        task = _make_task(db, user)
        resp = client.post("/v1/artifacts", headers=_bearer(user),
                           json=_artifact_body(task_id=task.id))
        assert resp.status_code == 403

    def test_anonymous_401(self, client: TestClient, db: Session):
        resp = client.post("/v1/artifact-jobs",
                           json=_job_body("job-anon", requester_user_id=1))
        assert resp.status_code == 401

    def test_invalid_service_token_401(self, client: TestClient, db: Session):
        resp = client.post(
            "/v1/artifact-jobs",
            headers={"X-CSP-Service-Token": "wrong-token"},
            json=_job_body("job-inv", requester_user_id=1),
        )
        assert resp.status_code == 401


# ── binding 規則(constitution §6)─────────────────────────────────────────────


class TestBindingRule:
    def test_artifact_requires_task_or_snapshot_422(self, client: TestClient, db: Session):
        make_user(db, username="bind_none")
        resp = _register_artifact(client)  # 兩者皆缺
        assert resp.status_code == 422
        assert "task" in resp.json()["detail"]

    def test_artifact_unknown_task_404(self, client: TestClient, db: Session):
        make_user(db, username="bind_404")
        resp = _register_artifact(client, task_id=99999)
        assert resp.status_code == 404


# ── 分類繼承(effective = max、單向)──────────────────────────────────────────


class TestClassificationInheritance:
    def test_task_level_wins_when_explicit_lower(self, client: TestClient, db: Session):
        user = make_user(db, username="inh_task")
        task = _make_task(db, user, level="機密")
        # explicit 省略(無機密)< task 機密 → effective 機密。
        resp = _register_artifact(client, task_id=task.id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification_level"] == "機密"
        artifact_id = resp.json()["artifact_id"]
        art = db.get(Artifact, artifact_id)
        db.refresh(art)
        assert art.classification_level == "機密"
        # 寫了一筆 ClassificationEvent(無機密 → 機密)。
        ev = (db.query(ClassificationEvent)
              .filter(ClassificationEvent.resource_type == "artifact",
                      ClassificationEvent.resource_id == str(artifact_id))
              .one())
        assert ev.previous_level == "無機密" and ev.new_level == "機密"
        assert ev.reason == "source_selected"

    def test_explicit_higher_is_one_way(self, client: TestClient, db: Session):
        user = make_user(db, username="inh_oneway")
        task = _make_task(db, user, level="機密")
        # explicit 極機密 > task 機密 → effective 極機密(不因 task 降級)。
        resp = _register_artifact(client, task_id=task.id, level="極機密")
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification_level"] == "極機密"
        # 首版分類同步為 effective。
        art_id = resp.json()["artifact_id"]
        ver = (db.query(ArtifactVersion)
               .filter(ArtifactVersion.artifact_id == art_id).one())
        assert ver.classification_level == "極機密"

    def test_snapshot_only_binding_inherits(self, client: TestClient, db: Session):
        user = make_user(db, username="inh_snap")
        task = _make_task(db, user, level="無機密")
        snap = SourceSnapshot(task_id=task.id, origin="collection",
                              classification_level="機密")
        db.add(snap)
        db.commit()
        db.refresh(snap)
        resp = _register_artifact(client, snapshot_id=snap.id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification_level"] == "機密"


# ── 版本 ──────────────────────────────────────────────────────────────────────


class TestVersioning:
    def test_add_version_increments(self, client: TestClient, db: Session):
        user = make_user(db, username="ver_inc")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, task_id=task.id).json()["artifact_id"]
        resp = client.post(
            f"/v1/artifacts/{art_id}/versions", headers=_SVC,
            json={"storage_ref": "store://artifacts/r-v2.pdf"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["version"] == 2
        art = db.get(Artifact, art_id)
        db.refresh(art)
        assert art.current_version == 2
        assert db.query(ArtifactVersion).filter(
            ArtifactVersion.artifact_id == art_id).count() == 2

    def test_version_reinherits_higher_explicit(self, client: TestClient, db: Session):
        user = make_user(db, username="ver_reinh")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, task_id=task.id).json()["artifact_id"]
        # 新版帶更高 explicit → artifact 單向升到 極機密。
        resp = client.post(
            f"/v1/artifacts/{art_id}/versions", headers=_SVC,
            json={"storage_ref": "store://x/v2.pdf",
                  "classification_level": "極機密"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification_level"] == "極機密"
        art = db.get(Artifact, art_id)
        db.refresh(art)
        assert art.classification_level == "極機密"


# ── 匯出 policy gate ──────────────────────────────────────────────────────────


class TestExportGate:
    def test_export_allow_records_decision_and_row(self, client: TestClient, db: Session):
        user = make_user(db, username="exp_ok")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, task_id=task.id).json()["artifact_id"]
        # target_floor 機密 >= artifact 機密 → allow。
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports", headers=_SVC,
            json={"target_classification_floor": "機密",
                  "target_space": "project-x", "export_format": "pdf"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["decision"] == "allow"
        assert db.query(ExportRecord).filter(
            ExportRecord.artifact_id == art_id).count() == 1
        pd = (db.query(PolicyDecision)
              .filter(PolicyDecision.action == "artifact.export",
                      PolicyDecision.resource_id == str(art_id)).one())
        assert pd.decision == "allow"

    def test_export_deny_403_no_row(self, client: TestClient, db: Session):
        user = make_user(db, username="exp_deny")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, task_id=task.id).json()["artifact_id"]
        # target_floor 營業秘密 < artifact 機密 → deny(403)。
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports", headers=_SVC,
            json={"target_classification_floor": "營業秘密"},
        )
        assert resp.status_code == 403
        # deny 只寫 PolicyDecision,不落 allow 匯出列。
        assert db.query(ExportRecord).filter(
            ExportRecord.artifact_id == art_id).count() == 0
        pd = (db.query(PolicyDecision)
              .filter(PolicyDecision.action == "artifact.export",
                      PolicyDecision.resource_id == str(art_id)).one())
        assert pd.decision == "deny"
        assert pd.reason  # deny 必附可解釋原因

    def test_export_via_user_jwt_allow(self, client: TestClient, db: Session):
        user = make_user(db, username="exp_jwt")
        task = _make_task(db, user, level="無機密")
        art_id = _register_artifact(client, task_id=task.id).json()["artifact_id"]
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports", headers=_bearer(user),
            json={"target_classification_floor": "機密"},
        )
        assert resp.status_code == 201, resp.text
        row = db.query(ExportRecord).filter(
            ExportRecord.artifact_id == art_id).one()
        assert row.exporter_user_id == user.id


# ── 治理讀面(owner-scope)────────────────────────────────────────────────────


class TestGovernanceReads:
    def _seed_artifact(self, client, db, owner):
        task = _make_task(db, owner, level="機密")
        return _register_artifact(client, task_id=task.id).json()["artifact_id"]

    def test_owner_lists_and_reads_detail(self, client: TestClient, db: Session):
        owner = make_user(db, username="gov_owner")
        art_id = self._seed_artifact(client, db, owner)
        lst = client.get("/api/artifacts", headers=_bearer(owner))
        assert lst.status_code == 200
        assert art_id in [a["id"] for a in lst.json()]
        detail = client.get(f"/api/artifacts/{art_id}", headers=_bearer(owner))
        assert detail.status_code == 200
        assert detail.json()["classification_level"] == "機密"
        assert len(detail.json()["versions"]) == 1

    def test_foreign_user_403_and_not_listed(self, client: TestClient, db: Session):
        owner = make_user(db, username="gov_owner2")
        other = make_user(db, username="gov_other")
        art_id = self._seed_artifact(client, db, owner)
        detail = client.get(f"/api/artifacts/{art_id}", headers=_bearer(other))
        assert detail.status_code == 403
        lst = client.get("/api/artifacts", headers=_bearer(other))
        assert art_id not in [a["id"] for a in lst.json()]

    def test_admin_sees_all(self, client: TestClient, db: Session):
        owner = make_user(db, username="gov_owner3")
        admin = make_user(db, username="gov_admin", role="admin")
        art_id = self._seed_artifact(client, db, owner)
        lst = client.get("/api/artifacts", headers=_bearer(admin))
        assert art_id in [a["id"] for a in lst.json()]
        assert client.get(f"/api/artifacts/{art_id}",
                          headers=_bearer(admin)).status_code == 200

    def test_filter_by_artifact_type(self, client: TestClient, db: Session):
        owner = make_user(db, username="gov_filter")
        task = _make_task(db, owner, level="無機密")
        _register_artifact(client, task_id=task.id, atype="report")
        _register_artifact(client, task_id=task.id, atype="slides")
        resp = client.get("/api/artifacts?artifact_type=slides",
                          headers=_bearer(owner))
        assert resp.status_code == 200
        assert {a["artifact_type"] for a in resp.json()} == {"slides"}
