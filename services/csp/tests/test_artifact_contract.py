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
import json
import hashlib
import zipfile
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

# Same house pattern as test_trace_endpoints.py / test_proxy_task_wiring.py:
# startup_security blocks dev default secrets in production mode — allow in tests.
os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models.artifact import Artifact, ArtifactJob, ArtifactVersion, ExportRecord
from app.models.audit_log import AuditLog
from app.models.classification import ClassificationEvent
from app.models.clearance import (
    ClearanceGrant,
    ClearanceGrantCompartment,
    CollectionAccessGrant,
    CollectionRequiredCompartment,
    SecurityCompartment,
)
from app.models.ingestion import IngestionCollection
from app.models.policy_decision import PolicyDecision
from app.models.registered_service import RegisteredService
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.schemas.contracts.artifacts import (
    ArtifactJobIn,
    ArtifactJobPatch,
    ArtifactUploadMetadata,
)
from app.services import agent_credential_service
from app.services.agent_credential_service import CallerIdentity
from app.utils.security import create_access_token
from tests.conftest import make_user

SVC_TOKEN = "svc-slice8a-test-token"
_SVC = {"X-CSP-Service-Token": SVC_TOKEN}
_CONTRACT = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts/fixtures/artifact-control-plane-v1.json")
    .read_text(encoding="utf-8")
)


def _office_bytes(kind: str) -> bytes:
    roots = {
        "pptx": (
            "ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation.main+xml",
        ),
        "xlsx": (
            "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet.main+xml",
        ),
    }
    root, media_type = roots[kind]
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Override PartName="/{root}" ContentType="{media_type}"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        archive.writestr(root, "<root/>")
    return output.getvalue()


@pytest.fixture(autouse=True)
def _svc_token(monkeypatch, tmp_path, db):
    """本模組內把 legacy CSP_SERVICE_TOKEN 設成已知值,供 /v1 service 面測試
    (function-scoped,測完自動還原,不外洩到別模組)。"""
    monkeypatch.setattr(settings, "CSP_SERVICE_TOKEN", SVC_TOKEN)
    monkeypatch.setattr(
        settings,
        "ARTIFACT_BLOB_STORAGE_PATH",
        str(tmp_path / "artifact-blobs"),
    )
    service_client = ServiceClient(
        client_name="artifact-contract-studio",
        client_type="worker",
        service_token_envelope="enc::stub",
        service_token_lookup_hash="a" * 64,
        is_active=True,
        is_legacy=False,
    )
    db.add(service_client)
    db.flush()
    service = RegisteredService(
        name="Artifact Contract Studio",
        slug="artifact-contract-studio",
        service_type="artifact_tool",
        entry_url="https://studio.test.invalid",
        data_egress=["artifact"],
        service_client_id=service_client.id,
        is_active=True,
    )
    db.add(service)
    db.commit()

    def _verify(_db, *, token):
        if token != SVC_TOKEN:
            return None
        return CallerIdentity(
            kind="service_client",
            agent_id=None,
            service_client_id=service_client.id,
            credential_id=service_client.id,
            is_legacy=False,
            used_previous_token=False,
        )

    monkeypatch.setattr(agent_credential_service, "verify_service_token", _verify)
    yield service


def _bearer(user) -> dict:
    token = create_access_token({
        "sub": str(user.id), "username": user.username,
        "role": user.role, "tv": user.token_version,
    })
    return {"Authorization": f"Bearer {token}"}


def _make_task(db: Session, user, level: str = "無機密") -> Task:
    task = Task(title="測試任務", task_type="query", requester_user_id=user.id,
                status="running", classification_level=level,
                selected_service_id="artifact-contract-studio")
    db.add(task)
    db.flush()
    db.add(TaskRun(
        task_id=task.id,
        run_sequence=1,
        dispatch_target="studio",
        status="running",
        classification_level=level,
    ))
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


def _register_artifact(client, db, *, task_id=None, snapshot_id=None, level=None,
                       atype="report", job_id=None, headers=None,
                       attempt=1, lease_token="test-artifact-lease-token-123"):
    if task_id is None and snapshot_id is not None:
        snapshot = db.get(SourceSnapshot, snapshot_id)
        task_id = snapshot.task_id if snapshot is not None else None
    task = db.get(Task, task_id) if task_id is not None else None
    if task_id is not None and task is None and snapshot_id is None:
        snapshot_id = 99998
    if snapshot_id is None and task is not None:
        if task.source_snapshot_id is not None:
            snapshot_id = task.source_snapshot_id
        else:
            snapshot = SourceSnapshot(
                task_id=task.id,
                origin="none",
                source_scope="none",
                classification_level=task.classification_level,
            )
            db.add(snapshot)
            db.flush()
            snapshot_id = snapshot.id
            task.source_snapshot_id = snapshot.id
    elif task is not None and snapshot_id is not None and task.source_snapshot_id is None:
        task.source_snapshot_id = snapshot_id
    actual_job_id = job_id or f"immutable-{task_id}-{atype}"
    if task is not None and snapshot_id is not None and db.get(ArtifactJob, actual_job_id) is None:
        db.add(ArtifactJob(
            job_id=actual_job_id,
            owner_user_id=task.requester_user_id,
            task_id=task.id,
            source_snapshot_id=snapshot_id,
            artifact_type=atype,
            status="running",
            durable_attempt=attempt,
            durable_lease_digest=hashlib.sha256(lease_token.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ))
        db.commit()

    formats = {
        "report": (b"%PDF-1.7\nreport\n%%EOF", "report.pdf", "application/pdf"),
        "slides": (
            _office_bytes("pptx"),
            "slides.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        "mindmap": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "map.svg", "image/svg+xml"),
        "infographic": (
            b"%PDF-1.7\ninfographic\n%%EOF",
            "infographic.pdf",
            "application/pdf",
        ),
        "datatable": (
            _office_bytes("xlsx"),
            "table.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    }
    content, filename, media_type = formats[atype]
    body = {
        "artifact_type": atype,
        "title": "季報",
        "job_id": actual_job_id,
        "task_id": task_id,
        "source_snapshot_id": snapshot_id,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content_size": len(content),
        "media_type": media_type,
        "original_filename": filename,
    }
    if level is not None:
        body["classification_level"] = level
    request_headers = {
        **(headers or _SVC),
        "X-Studio-Attempt": str(attempt),
        "X-Studio-Lease-Token": lease_token,
    }
    return client.post(
        "/v1/artifacts/upload",
        headers=request_headers,
        data={"metadata_json": json.dumps(body, ensure_ascii=False)},
        files={"file": (filename, content, media_type)},
    )


# ── ArtifactJob:冪等 upsert + 狀態機 ─────────────────────────────────────────


class TestJobContract:
    def test_register_job_exact_idempotency_rejects_rebinding(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="job_owner")
        r1 = client.post("/v1/artifact-jobs", headers=_SVC,
                         json=_job_body("job-abc", requester_user_id=user.id))
        assert r1.status_code == 201, r1.text
        assert r1.json()["job_id"] == "job-abc"
        assert r1.json()["owner_user_id"] == user.id
        # Exact replay preserves authority and returns the existing row.
        replay = client.post(
            "/v1/artifact-jobs",
            headers=_SVC,
            json=_job_body("job-abc", requester_user_id=user.id),
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["artifact_type"] == "report"
        # Reusing the id with a different immutable binding is a conflict.
        r2 = client.post("/v1/artifact-jobs", headers=_SVC,
                         json=_job_body("job-abc", requester_user_id=user.id,
                                        artifact_type="slides"))
        assert r2.status_code == 409, r2.text
        assert db.get(ArtifactJob, "job-abc").artifact_type == "report"
        assert db.query(ArtifactJob).count() == 1

    def test_lease_refresh_is_fenced_and_registration_remains_idempotent(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="lease_owner")
        body = _job_body("job-lease", requester_user_id=user.id)
        assert client.post("/v1/artifact-jobs", headers=_SVC, json=body).status_code == 201
        lease = {
            "attempt": 1,
            "lease_token": "lease-token-123456789",
            "lease_seconds": 90,
        }
        assert client.put(
            "/v1/artifact-jobs/job-lease/lease", headers=_SVC, json=lease
        ).status_code == 200
        # Lease expiry is mutable and must not break exact registration replay.
        assert client.post("/v1/artifact-jobs", headers=_SVC, json=body).status_code == 201
        authority = client.get(
            "/v1/artifact-jobs/job-lease/authority", headers=_SVC
        )
        assert authority.status_code == 200
        assert authority.json()["artifact_id"] is None
        stolen = {**lease, "lease_token": "other-lease-token-987654"}
        assert client.put(
            "/v1/artifact-jobs/job-lease/lease", headers=_SVC, json=stolen
        ).status_code == 409

    def test_task_bound_job_rejects_non_selected_service_before_any_write(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="job_service_fence_owner")
        task = _make_task(db, user)
        body = _job_body(
            "job-service-fence",
            requester_user_id=user.id,
            task_id=task.id,
        )
        created = client.post("/v1/artifact-jobs", headers=_SVC, json=body)
        assert created.status_code == 201, created.text

        task.selected_service_id = "another-artifact-service"
        db.commit()

        lease = client.put(
            "/v1/artifact-jobs/job-service-fence/lease",
            headers=_SVC,
            json={
                "attempt": 9,
                "lease_token": "foreign-lease-token-123456",
                "lease_seconds": 90,
            },
        )
        patch = client.patch(
            "/v1/artifact-jobs/job-service-fence",
            headers=_SVC,
            json={"status": "running", "progress": 75},
        )
        authority = client.get(
            "/v1/artifact-jobs/job-service-fence/authority",
            headers=_SVC,
        )

        assert lease.status_code == 403
        assert patch.status_code == 403
        assert authority.status_code == 403
        db.expire_all()
        job = db.get(ArtifactJob, "job-service-fence")
        assert job.status == "queued"
        assert job.progress == 0
        assert job.durable_attempt is None
        assert job.durable_lease_digest is None

    def test_failed_job_patch_closes_outer_task_and_run_atomically(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="failed_job_owner")
        task = _make_task(db, user)
        snapshot = SourceSnapshot(
            task_id=task.id,
            origin="none",
            source_scope="none",
            classification_level=task.classification_level,
        )
        db.add(snapshot)
        db.flush()
        task.source_snapshot_id = snapshot.id
        db.commit()
        body = _job_body(
            "job-terminal-failed",
            requester_user_id=user.id,
            task_id=task.id,
            source_snapshot_id=snapshot.id,
            status="running",
        )
        assert client.post("/v1/artifact-jobs", headers=_SVC, json=body).status_code == 201

        response = client.patch(
            "/v1/artifact-jobs/job-terminal-failed",
            headers=_SVC,
            json={"status": "failed", "error": {"code": "retry_exhausted"}},
        )
        assert response.status_code == 200, response.text
        db.expire_all()
        assert db.get(Task, task.id).status == "failed"
        run = db.query(TaskRun).filter(TaskRun.task_id == task.id).one()
        assert run.status == "failed"
        # Response-loss replay is exact-idempotent and cannot reopen the run.
        replay = client.patch(
            "/v1/artifact-jobs/job-terminal-failed",
            headers=_SVC,
            json={"status": "failed", "error": {"code": "retry_exhausted"}},
        )
        assert replay.status_code == 200
        assert db.query(TaskRun).filter(TaskRun.task_id == task.id).count() == 1

    def test_failed_artifact_job_cannot_close_non_studio_run(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="nonstudio_run_owner")
        task = _make_task(db, user)
        run = db.query(TaskRun).filter(TaskRun.task_id == task.id).one()
        run.dispatch_target = "model"
        db.commit()
        body = _job_body(
            "job-must-not-close-model-run",
            requester_user_id=user.id,
            task_id=task.id,
            status="running",
        )
        assert client.post("/v1/artifact-jobs", headers=_SVC, json=body).status_code == 201
        response = client.patch(
            "/v1/artifact-jobs/job-must-not-close-model-run",
            headers=_SVC,
            json={"status": "failed"},
        )
        assert response.status_code == 409
        db.expire_all()
        assert db.get(TaskRun, run.id).status == "running"
        assert db.get(Task, task.id).status == "running"
        assert db.get(ArtifactJob, "job-must-not-close-model-run").status == "running"

    def test_terminal_patch_requires_task_selected_service(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="wrong_terminal_service_owner")
        task = _make_task(db, user)
        body = _job_body(
            "job-wrong-terminal-service",
            requester_user_id=user.id,
            task_id=task.id,
            status="running",
        )
        assert client.post("/v1/artifact-jobs", headers=_SVC, json=body).status_code == 201
        task.selected_service_id = "another-studio-service"
        db.commit()
        response = client.patch(
            "/v1/artifact-jobs/job-wrong-terminal-service",
            headers=_SVC,
            json={"status": "failed"},
        )
        assert response.status_code == 403
        db.expire_all()
        assert db.get(ArtifactJob, "job-wrong-terminal-service").status == "running"
        assert db.get(Task, task.id).status == "running"
        assert db.query(TaskRun).filter(TaskRun.task_id == task.id).one().status == "running"

    def test_register_job_resolves_employee_id(self, client: TestClient, db: Session):
        # 員編騎在 username;employee_id 可解析 owner。
        user = make_user(db, username="990000001")
        resp = client.post(
            "/v1/artifact-jobs", headers=_SVC,
            json={"job_id": "job-emp", "artifact_type": "mindmap",
                  "employee_id": "990000001"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_user_id"] == user.id
        assert resp.json()["requester_employee_id"] == "990000001"

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

    def test_cancelled_is_a_terminal_status(self, client: TestClient, db: Session):
        user = make_user(db, username="job_cancel")
        client.post(
            "/v1/artifact-jobs",
            headers=_SVC,
            json=_job_body("job-cancel", requester_user_id=user.id),
        )
        cancelled = client.patch(
            "/v1/artifact-jobs/job-cancel",
            headers=_SVC,
            json={"status": "cancelled"},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
        terminal = client.patch(
            "/v1/artifact-jobs/job-cancel",
            headers=_SVC,
            json={"status": "running"},
        )
        assert terminal.status_code == 409

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
    def test_named_writer_readiness_contract(self, client: TestClient):
        response = client.get("/v1/artifact-writer/ready", headers=_SVC)
        assert response.status_code == 200, response.text
        assert response.json()["service_type"] == "artifact_tool"
        assert response.json()["data_egress"] == ["artifact"]

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

    def test_agent_credential_cannot_write_artifact(
        self, client: TestClient, db: Session, monkeypatch
    ):
        user = make_user(db, username="agent_artifact_owner")
        task = _make_task(db, user)
        monkeypatch.setattr(
            agent_credential_service,
            "verify_service_token",
            lambda _db, *, token: CallerIdentity(
                kind="agent",
                agent_id=777,
                service_client_id=None,
                credential_id=888,
                is_legacy=False,
                used_previous_token=False,
            ),
        )
        resp = client.post(
            "/v1/artifacts",
            headers={"X-CSP-Service-Token": "csk-agent"},
            json=_artifact_body(task_id=task.id, generated_by_agent_id=777),
        )
        assert resp.status_code == 403

    def test_legacy_service_token_cannot_write_artifact(
        self, client: TestClient, monkeypatch
    ):
        monkeypatch.setattr(
            agent_credential_service,
            "verify_service_token",
            lambda _db, *, token: CallerIdentity(
                kind="service_client",
                agent_id=None,
                service_client_id=1,
                credential_id=1,
                is_legacy=True,
                used_previous_token=False,
            ),
        )
        response = client.post(
            "/v1/artifact-jobs",
            headers={"X-CSP-Service-Token": "legacy-fleet-token"},
            json=_job_body("legacy-job"),
        )
        assert response.status_code == 403

    def test_extra_egress_is_not_exact_artifact_capability(
        self, client: TestClient, db: Session, _svc_token
    ):
        _svc_token.data_egress = ["artifact", "trace"]
        db.commit()
        response = client.get("/v1/artifact-writer/ready", headers=_SVC)
        assert response.status_code == 403


    def test_service_without_artifact_capability_is_rejected(
        self, client: TestClient, db: Session, monkeypatch
    ):
        user = make_user(db, username="wrong_cap_owner")
        task = _make_task(db, user)
        other = ServiceClient(
            client_name="not-an-artifact-tool",
            client_type="worker",
            service_token_envelope="enc::stub",
            service_token_lookup_hash="b" * 64,
            is_active=True,
        )
        db.add(other)
        db.commit()
        monkeypatch.setattr(
            agent_credential_service,
            "verify_service_token",
            lambda _db, *, token: CallerIdentity(
                kind="service_client",
                agent_id=None,
                service_client_id=other.id,
                credential_id=other.id,
                is_legacy=False,
                used_previous_token=False,
            ),
        )
        resp = client.post(
            "/v1/artifacts",
            headers={"X-CSP-Service-Token": "csk-wrong-cap"},
            json=_artifact_body(task_id=task.id),
        )
        assert resp.status_code == 403

    def test_other_artifact_service_cannot_complete_unassigned_task(
        self, client: TestClient, db: Session, monkeypatch
    ):
        user = make_user(db, username="other_studio_task_owner")
        task = _make_task(db, user)
        other = ServiceClient(
            client_name="other-artifact-studio",
            client_type="worker",
            service_token_envelope="enc::stub",
            service_token_lookup_hash="c" * 64,
            is_active=True,
        )
        db.add(other)
        db.flush()
        other_service = RegisteredService(
            name="Other Artifact Studio",
            slug="other-artifact-studio",
            service_type="artifact_tool",
            entry_url="https://other-studio.test.invalid",
            data_egress=["artifact"],
            service_client_id=other.id,
            is_active=True,
        )
        db.add(other_service)
        db.commit()
        monkeypatch.setattr(
            agent_credential_service,
            "verify_service_token",
            lambda _db, *, token: CallerIdentity(
                kind="service_client",
                agent_id=None,
                service_client_id=other.id,
                credential_id=other.id,
                is_legacy=False,
                used_previous_token=False,
            ),
        )

        resp = _register_artifact(
            client,
            db,
            task_id=task.id,
            headers={"X-CSP-Service-Token": "csk-other-artifact-studio"},
        )

        assert resp.status_code == 403


def test_shared_wire_fixture_runs_through_csp_endpoints(
    client: TestClient, db: Session
):
    """CSP consumes the same exact wire bodies asserted by Studio tests."""
    user = make_user(db, username="contract_owner")
    task = _make_task(db, user)
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="none",
        source_scope="none",
        classification_level=task.classification_level,
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    db.commit()

    job_body = dict(_CONTRACT["job_created"])
    job_body["requester_user_id"] = user.id
    job_body["task_id"] = task.id
    job_body["source_snapshot_id"] = snapshot.id
    ArtifactJobIn.model_validate(job_body)
    created = client.post("/v1/artifact-jobs", headers=_SVC, json=job_body)
    assert created.status_code == 201, created.text
    lease_token = "contract-lease-token-123456"
    leased = client.put(
        f"/v1/artifact-jobs/{job_body['job_id']}/lease",
        headers=_SVC,
        json={"attempt": 1, "lease_token": lease_token, "lease_seconds": 90},
    )
    assert leased.status_code == 200, leased.text

    artifact_body = dict(_CONTRACT["artifact_registered"])
    artifact_body["task_id"] = task.id
    artifact_body["source_snapshot_id"] = snapshot.id
    ArtifactUploadMetadata.model_validate(artifact_body)
    content = _office_bytes("pptx")
    artifact_body["content_sha256"] = hashlib.sha256(content).hexdigest()
    artifact_body["content_size"] = len(content)
    registered = client.post(
        "/v1/artifacts/upload",
        headers={
            **_SVC,
            "X-Studio-Attempt": "1",
            "X-Studio-Lease-Token": lease_token,
        },
        data={"metadata_json": json.dumps(artifact_body, ensure_ascii=False)},
        files={"file": (artifact_body["original_filename"], content, artifact_body["media_type"])},
    )
    assert registered.status_code == 201, registered.text

    completed_body = dict(_CONTRACT["job_completed"])
    completed_body["artifact_id"] = registered.json()["artifact_id"]
    ArtifactJobPatch.model_validate(completed_body)
    completed = client.patch(
        f"/v1/artifact-jobs/{job_body['job_id']}",
        headers=_SVC,
        json=completed_body,
    )
    assert completed.status_code == 200, completed.text


# ── binding 規則(constitution §6)─────────────────────────────────────────────


class TestBindingRule:
    def test_stale_attempt_cannot_commit_artifact(self, client, db):
        user = make_user(db, username="artifact_fence_owner")
        task = _make_task(db, user)
        snapshot = SourceSnapshot(
            task_id=task.id,
            origin="none",
            source_scope="none",
            classification_level=task.classification_level,
        )
        db.add(snapshot)
        db.flush()
        task.source_snapshot_id = snapshot.id
        job = ArtifactJob(
            job_id="artifact-fence-job",
            owner_user_id=user.id,
            task_id=task.id,
            source_snapshot_id=snapshot.id,
            artifact_type="report",
            status="running",
        )
        db.add(job)
        db.commit()
        token_one = "artifact-fence-token-one-123"
        token_two = "artifact-fence-token-two-456"
        for attempt, token in ((1, token_one), (2, token_two)):
            response = client.put(
                "/v1/artifact-jobs/artifact-fence-job/lease",
                headers=_SVC,
                json={"attempt": attempt, "lease_token": token, "lease_seconds": 90},
            )
            assert response.status_code == 200, response.text

        stale = _register_artifact(
            client,
            db,
            task_id=task.id,
            snapshot_id=snapshot.id,
            job_id=job.job_id,
            attempt=1,
            lease_token=token_one,
        )
        assert stale.status_code == 409
        assert db.query(Artifact).count() == 0

        current = _register_artifact(
            client,
            db,
            task_id=task.id,
            snapshot_id=snapshot.id,
            job_id=job.job_id,
            attempt=2,
            lease_token=token_two,
        )
        assert current.status_code == 201, current.text
        assert db.query(Artifact).count() == 1

    def test_artifact_requires_task_or_snapshot_422(self, client: TestClient, db: Session):
        make_user(db, username="bind_none")
        resp = _register_artifact(client, db)  # 兩者皆缺
        assert resp.status_code == 422
        assert any("task_id" in item["loc"] for item in resp.json()["detail"])

    def test_artifact_unknown_task_404(self, client: TestClient, db: Session):
        make_user(db, username="bind_404")
        resp = _register_artifact(client, db, task_id=99999)
        assert resp.status_code == 404

    def test_job_cannot_cross_requester_boundary(self, client, db):
        task_owner = make_user(db, username="artifact_task_owner")
        other = make_user(db, username="artifact_other_requester")
        task = _make_task(db, task_owner)
        snapshot = SourceSnapshot(
            task_id=task.id,
            origin="none",
            source_scope="none",
            classification_level=task.classification_level,
        )
        db.add(snapshot)
        db.flush()
        task.source_snapshot_id = snapshot.id
        job = ArtifactJob(
            job_id="cross-requester-job",
            owner_user_id=other.id,
            task_id=task.id,
            source_snapshot_id=snapshot.id,
            artifact_type="report",
            status="running",
            durable_attempt=1,
            durable_lease_digest=hashlib.sha256(
                b"test-artifact-lease-token-123"
            ).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(job)
        db.commit()

        resp = _register_artifact(
            client,
            db,
            task_id=task.id,
            snapshot_id=snapshot.id,
            job_id=job.job_id,
        )
        assert resp.status_code == 403

    def test_snapshot_cannot_cross_task_requester_boundary(self, client, db):
        first = make_user(db, username="artifact_snapshot_first")
        second = make_user(db, username="artifact_snapshot_second")
        task = _make_task(db, first)
        other_task = _make_task(db, second)
        snapshot = SourceSnapshot(
            task_id=other_task.id,
            origin="collection",
            classification_level="機密",
        )
        db.add(snapshot)
        db.commit()

        resp = _register_artifact(
            client, db,
            task_id=task.id,
            snapshot_id=snapshot.id,
        )

        assert resp.status_code == 403

    @pytest.mark.parametrize("terminal", ["failed", "cancelled", "blocked_by_policy"])
    def test_terminal_task_cannot_be_revived_by_artifact_registration(
        self, client, db, terminal
    ):
        user = make_user(db, username=f"artifact_terminal_{terminal}")
        task = _make_task(db, user)
        run = db.query(TaskRun).filter_by(task_id=task.id).one()
        run.status = "failed" if terminal == "blocked_by_policy" else terminal
        task.status = terminal
        db.commit()

        resp = _register_artifact(client, db, task_id=task.id)

        assert resp.status_code == 409
        db.refresh(task)
        assert task.status == terminal


class TestUploadIdempotency:
    def test_crash_window_replay_returns_same_authority_without_new_blob(
        self, client, db, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "ARTIFACT_BLOB_STORAGE_PATH", str(tmp_path))
        owner = make_user(db, username="artifact_replay_owner")
        task = _make_task(db, owner)
        first = _register_artifact(client, db, task_id=task.id)
        assert first.status_code == 201, first.text
        db.refresh(task)
        second = _register_artifact(
            client, db, task_id=task.id, snapshot_id=task.source_snapshot_id
        )
        assert second.status_code == 201, second.text
        assert second.json() == first.json()
        assert db.query(Artifact).filter(Artifact.job_id == f"immutable-{task.id}-report").count() == 1
        assert db.query(ArtifactVersion).count() == 1
        assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1

    def test_replay_with_mismatched_metadata_is_409(
        self, client, db, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "ARTIFACT_BLOB_STORAGE_PATH", str(tmp_path))
        owner = make_user(db, username="artifact_replay_mismatch")
        task = _make_task(db, owner)
        first = _register_artifact(client, db, task_id=task.id)
        assert first.status_code == 201, first.text
        db.refresh(task)
        mismatch = _register_artifact(
            client, db, task_id=task.id, snapshot_id=task.source_snapshot_id,
            level="機密"
        )
        assert mismatch.status_code == 409
        assert db.query(Artifact).count() == 1
        assert db.query(ArtifactVersion).count() == 1


# ── 分類繼承(effective = max、單向)──────────────────────────────────────────


class TestClassificationInheritance:
    def test_registration_closes_canonical_run_and_audits_service_actor(
        self, client: TestClient, db: Session
    ):
        user = make_user(db, username="artifact_ledger_owner")
        task = _make_task(db, user)

        resp = _register_artifact(client, db, task_id=task.id)

        assert resp.status_code == 201, resp.text
        db.refresh(task)
        run = db.query(TaskRun).filter_by(task_id=task.id).one()
        event = db.query(AuditLog).filter_by(action="artifact.uploaded").one()
        assert task.status == "completed"
        assert run.status == "completed"
        assert event.actor_user_id is None
        assert event.actor_username.startswith("service_client:")
        assert str(user.id) != event.actor_username

    def test_task_level_wins_when_explicit_lower(self, client: TestClient, db: Session):
        user = make_user(db, username="inh_task")
        task = _make_task(db, user, level="機密")
        # explicit 省略(無機密)< task 機密 → effective 機密。
        resp = _register_artifact(client, db, task_id=task.id)
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
        resp = _register_artifact(client, db, task_id=task.id, level="極機密")
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
        resp = _register_artifact(client, db, snapshot_id=snap.id)
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification_level"] == "機密"


# ── 版本 ──────────────────────────────────────────────────────────────────────


class TestVersioning:
    def test_legacy_storage_ref_version_is_rejected(self, client: TestClient, db: Session):
        user = make_user(db, username="ver_inc")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, db, task_id=task.id).json()["artifact_id"]
        resp = client.post(
            f"/v1/artifacts/{art_id}/versions", headers=_SVC,
            json={"storage_ref": "store://artifacts/r-v2.pdf"},
        )
        assert resp.status_code == 410, resp.text
        art = db.get(Artifact, art_id)
        db.refresh(art)
        assert art.current_version == 1
        assert db.query(ArtifactVersion).filter(
            ArtifactVersion.artifact_id == art_id).count() == 1

    def test_legacy_version_cannot_raise_classification(self, client: TestClient, db: Session):
        user = make_user(db, username="ver_reinh")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, db, task_id=task.id).json()["artifact_id"]
        # 新版帶更高 explicit → artifact 單向升到 極機密。
        resp = client.post(
            f"/v1/artifacts/{art_id}/versions", headers=_SVC,
            json={"storage_ref": "store://x/v2.pdf",
                  "classification_level": "極機密"},
        )
        assert resp.status_code == 410, resp.text
        art = db.get(Artifact, art_id)
        db.refresh(art)
        assert art.classification_level == "機密"


# ── 匯出 policy gate ──────────────────────────────────────────────────────────


class TestExportGate:
    def test_export_allow_records_decision_and_row(self, client: TestClient, db: Session):
        user = make_user(db, username="exp_ok")
        task = _make_task(db, user, level="機密")
        art_id = _register_artifact(client, db, task_id=task.id).json()["artifact_id"]
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
        art_id = _register_artifact(client, db, task_id=task.id).json()["artifact_id"]
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
        art_id = _register_artifact(client, db, task_id=task.id).json()["artifact_id"]
        resp = client.post(
            f"/v1/artifacts/{art_id}/exports", headers=_bearer(user),
            json={"target_classification_floor": "機密"},
        )
        assert resp.status_code == 201, resp.text
        row = db.query(ExportRecord).filter(
            ExportRecord.artifact_id == art_id).one()
        assert row.exporter_user_id == user.id

    def test_foreign_user_cannot_export_or_mutate_source_task(
        self, client: TestClient, db: Session
    ):
        owner = make_user(db, username="exp_owner")
        foreign = make_user(db, username="exp_foreign")
        task = _make_task(db, owner, level="機密")
        art_id = _register_artifact(client, db, task_id=task.id).json()["artifact_id"]
        # Artifact registration legitimately completes the Task. Put it back
        # into a mutable state so the denial branch would expose a foreign
        # status mutation if authorization were checked too late.
        task.status = "running"
        task.policy_decision_id = None
        db.commit()

        response = client.post(
            f"/v1/artifacts/{art_id}/exports",
            headers=_bearer(foreign),
            json={"target_classification_floor": "營業秘密"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "無權匯出此 artifact"
        assert db.query(ExportRecord).filter(
            ExportRecord.artifact_id == art_id
        ).count() == 0
        assert db.query(PolicyDecision).filter(
            PolicyDecision.action == "artifact.export",
            PolicyDecision.resource_id == str(art_id),
        ).count() == 0
        db.refresh(task)
        assert task.status == "running"
        assert task.policy_decision_id is None


# ── 治理讀面(owner-scope)────────────────────────────────────────────────────


class TestGovernanceReads:
    def _seed_artifact(self, client, db, owner):
        task = _make_task(db, owner, level="機密")
        return _register_artifact(client, db, task_id=task.id).json()["artifact_id"]

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

    def test_admin_has_no_cross_owner_data_bypass(self, client: TestClient, db: Session):
        owner = make_user(db, username="gov_owner3")
        admin = make_user(db, username="gov_admin", role="admin")
        art_id = self._seed_artifact(client, db, owner)
        lst = client.get("/api/artifacts", headers=_bearer(admin))
        assert lst.status_code == 200
        assert art_id not in [a["id"] for a in lst.json()]
        assert client.get(f"/api/artifacts/{art_id}",
                          headers=_bearer(admin)).status_code == 403

    def test_filter_by_artifact_type(self, client: TestClient, db: Session):
        owner = make_user(db, username="gov_filter")
        report_task = _make_task(db, owner, level="無機密")
        slides_task = _make_task(db, owner, level="無機密")
        _register_artifact(client, db, task_id=report_task.id, atype="report")
        _register_artifact(client, db, task_id=slides_task.id, atype="slides")
        resp = client.get("/api/artifacts?artifact_type=slides",
                          headers=_bearer(owner))
        assert resp.status_code == 200
        assert {a["artifact_type"] for a in resp.json()} == {"slides"}


# ── immutable browser download authorization ────────────────────────────────


def _download_fixture(
    client,
    db,
    monkeypatch,
    tmp_path,
    *,
    task_level="機密",
    grant_level="機密",
    require_compartment=False,
):
    monkeypatch.setattr(settings, "ARTIFACT_BLOB_STORAGE_PATH", str(tmp_path))
    owner = make_user(db, username=f"download_owner_{task_level}_{grant_level}")
    collection = IngestionCollection(
        name="download collection",
        chunking_config={},
        embedding_model="test",
        embedding_fingerprint=f"sha256:{'0' * 64}",
        embedding_dim=3,
        created_by=owner.id,
        classification_level=task_level,
    )
    db.add(collection)
    db.flush()
    task = _make_task(db, owner, level=task_level)
    task.selected_collection_ids = [collection.id]
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="collection",
        source_scope="project",
        collection_ids=[collection.id],
        classification_level=task_level,
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    now = datetime.now(timezone.utc)
    grant = ClearanceGrant(
        subject_user_id=owner.id,
        max_classification_level=grant_level,
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        basis_ticket="TEST-DOWNLOAD",
        issued_by_user_id=owner.id,
    )
    db.add(grant)
    db.flush()
    db.add(CollectionAccessGrant(
        clearance_grant_id=grant.id,
        collection_id=collection.id,
        membership_granted=True,
        need_to_know=True,
        basis_ticket="TEST-DOWNLOAD",
        issued_by_user_id=owner.id,
    ))
    compartment = None
    if require_compartment:
        compartment = SecurityCompartment(
            code="PROJECT_X",
            name="Project X",
            created_by_user_id=owner.id,
        )
        db.add(compartment)
        db.flush()
        db.add(CollectionRequiredCompartment(
            collection_id=collection.id,
            compartment_id=compartment.id,
            basis_ticket="TEST-DOWNLOAD",
            assigned_by_user_id=owner.id,
        ))
    db.commit()
    response = _register_artifact(
        client,
        db,
        task_id=task.id,
        snapshot_id=snapshot.id,
        level=task_level,
    )
    assert response.status_code == 201, response.text
    return owner, task, snapshot, collection, grant, compartment, response.json()


class TestArtifactDownloads:
    def test_restart_readback_is_private_and_hash_verified(
        self, client, db, monkeypatch, tmp_path
    ):
        owner, *_rest, result = _download_fixture(
            client, db, monkeypatch, tmp_path
        )
        response = client.get(
            f"/api/artifacts/{result['artifact_id']}/download",
            headers=_bearer(owner),
        )
        assert response.status_code == 200, response.text
        assert response.content == b"%PDF-1.7\nreport\n%%EOF"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in response.headers["content-disposition"]

    def test_cross_user_and_admin_have_no_data_bypass(
        self, client, db, monkeypatch, tmp_path
    ):
        _owner, *_rest, result = _download_fixture(
            client, db, monkeypatch, tmp_path
        )
        other = make_user(db, username="download_other")
        admin = make_user(db, username="download_admin", role="admin")
        url = f"/api/artifacts/{result['artifact_id']}/download"
        assert client.get(url, headers=_bearer(other)).status_code == 403
        assert client.get(url, headers=_bearer(admin)).status_code == 403

    def test_cross_collection_and_low_classification_are_denied(
        self, client, db, monkeypatch, tmp_path
    ):
        owner, task, snapshot, _collection, _grant, _comp, result = _download_fixture(
            client, db, monkeypatch, tmp_path
        )
        other_collection = IngestionCollection(
            name="other",
            chunking_config={},
            embedding_model="test",
            embedding_fingerprint=f"sha256:{'1' * 64}",
            embedding_dim=3,
            created_by=owner.id,
            classification_level="機密",
        )
        db.add(other_collection)
        db.flush()
        snapshot.collection_ids = [*snapshot.collection_ids, other_collection.id]
        task.selected_collection_ids = [*task.selected_collection_ids, other_collection.id]
        db.commit()
        url = f"/api/artifacts/{result['artifact_id']}/download"
        assert client.get(url, headers=_bearer(owner)).status_code == 403

        low_dir = tmp_path / "low"
        low_owner, *_low_rest, low_result = _download_fixture(
            client,
            db,
            monkeypatch,
            low_dir,
            task_level="機密",
            grant_level="營業秘密",
        )
        low_url = f"/api/artifacts/{low_result['artifact_id']}/download"
        assert client.get(low_url, headers=_bearer(low_owner)).status_code == 403

    def test_compartment_and_revocation_are_enforced(
        self, client, db, monkeypatch, tmp_path
    ):
        owner, _task, _snapshot, _collection, grant, compartment, result = (
            _download_fixture(
                client,
                db,
                monkeypatch,
                tmp_path,
                require_compartment=True,
            )
        )
        url = f"/api/artifacts/{result['artifact_id']}/download"
        assert client.get(url, headers=_bearer(owner)).status_code == 403
        db.add(ClearanceGrantCompartment(
            clearance_grant_id=grant.id,
            compartment_id=compartment.id,
        ))
        db.commit()
        assert client.get(url, headers=_bearer(owner)).status_code == 200

        manager = make_user(db, username="artifact_revoker", role="admin")
        revoke = client.post(
            f"/api/artifacts/{result['artifact_id']}/versions/{result['version_id']}/revoke",
            headers=_bearer(manager),
            json={"reason": "TEST-REVOKE"},
        )
        assert revoke.status_code == 200, revoke.text
        assert client.get(url, headers=_bearer(owner)).status_code == 410
