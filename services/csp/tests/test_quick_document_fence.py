"""Synthetic independent-Session cancellation between real Artifact commits."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models.artifact import Artifact, ArtifactVersion, ExportRecord
from app.models.quick_builder import QuickRun
from app.models.user import User
from app.services import quick_document as documents
from app.services import quick_execution as execution
from app.services import quick_test_lifecycle as lifecycle
from tests.test_quick_builder_review import _assistant, review_rows
from tests.test_quick_document_review import _rendered


@pytest.mark.parametrize("outcome", ["cancel", "expire"])
@pytest.mark.parametrize("commit_window", range(1, 7))
def test_document_cannot_write_past_cancel_or_expiry_commit_window(
    tmp_path, outcome, commit_window
):
    engine = create_engine("sqlite:///" + str(tmp_path / "document-fence.db"))
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as producer:
            rows = review_rows.__wrapped__(producer)
            owner = rows[0]
            assistant, version, _ = _assistant(producer, rows, published=False)
            owner_id, assistant_id = owner.id, assistant.id
            row = QuickRun(
                user_id=owner_id, version_id=version.id, status="running", result={}
            )
            producer.add(row)
            lease = lifecycle.claim_new(
                producer,
                row,
                request_id=str(uuid4()),
                message="合成文件",
                actor_department_id=owner.department_id,
            )
            producer.commit()
            commits = []
            interrupted = []

            def interrupt_after_commit(session):
                commits.append(len(commits) + 1)
                if len(commits) != commit_window:
                    return
                # The producer has committed its phase. A distinct connection
                # now revokes the authority and invalidates all visible files.
                with Session(engine, expire_on_commit=False) as canceller:
                    actor = canceller.get(User, owner_id)
                    if outcome == "cancel":
                        assert lifecycle.cancel_run_in_session(
                            canceller, user_id=owner_id, run_id=lease.run_id
                        )
                        execution.invalidate_test_artifacts(
                            canceller,
                            actor,
                            assistant_id,
                            lease.run_id,
                            "cancelled",
                            "CANCELLED",
                        )
                    else:
                        stored = canceller.get(QuickRun, lease.run_id)
                        stored.lease_expires_at = datetime.now(
                            timezone.utc
                        ) - timedelta(seconds=1)
                        canceller.commit()
                        assert execution.recover_test_runs(
                            canceller, actor, assistant_id, run_id=lease.run_id
                        ) == [lease.run_id]
                    canceller.commit()
                    interrupted.append(True)

            def check_fence():
                if not lifecycle.check_in_session(producer, lease):
                    raise HTTPException(409, detail={"type": "TEST_RUN_INACTIVE"})

            event.listen(producer, "after_commit", interrupt_after_commit)
            try:
                if commit_window < 6:
                    with pytest.raises(HTTPException) as rejected:
                        documents.register_document_artifact(
                            producer,
                            owner,
                            run_id=lease.run_id,
                            rendered=_rendered(),
                            classification_level="無機密",
                            before_persist=check_fence,
                        )
                    assert rejected.value.detail["type"] == "TEST_RUN_INACTIVE"
                else:
                    documents.register_document_artifact(
                        producer,
                        owner,
                        run_id=lease.run_id,
                        rendered=_rendered(),
                        classification_level="無機密",
                        before_persist=check_fence,
                    )
            finally:
                event.remove(producer, "after_commit", interrupt_after_commit)
            assert interrupted and len(commits) == commit_window
            producer.rollback()
            # The scoped listener must not interfere with later compensation.
            assert not list(producer.dispatch.before_commit)
            producer.commit()
            with Session(engine) as verification:
                result = verification.get(QuickRun, lease.run_id)
                assert result.status == (
                    "cancelled" if outcome == "cancel" else "interrupted"
                )
                assert result.result.get("artifacts", []) == []
                assert (
                    verification.query(Artifact).filter_by(status="completed").count()
                    == 0
                )
                assert all(
                    item.storage_ref is None and item.file_refs == []
                    for item in verification.query(ArtifactVersion).all()
                )
                # No later export is created after revocation between the
                # policy decision and export-record commits.
                assert verification.query(ExportRecord).count() == (
                    1 if commit_window == 6 else 0
                )
    finally:
        engine.dispose()


def test_fence_is_acquired_before_pending_task_autoflush_and_listener_is_removed(
    db, review_rows
):
    owner = review_rows[0]
    _, version, _ = _assistant(db, review_rows, published=False)
    row = QuickRun(user_id=owner.id, version_id=version.id, status="running", result={})
    db.add(row)
    db.commit()
    pending_at_check = []

    def revoked():
        pending_at_check.append(tuple(db.new))
        raise HTTPException(409, detail={"type": "TEST_RUN_INACTIVE"})

    with pytest.raises(HTTPException):
        documents.register_document_artifact(
            db,
            owner,
            run_id=row.id,
            rendered=_rendered(),
            classification_level="無機密",
            before_persist=revoked,
        )
    assert pending_at_check == [()]
    assert db.query(Artifact).count() == 0
    # If the scoped hook leaked, this ordinary commit would raise again.
    db.commit()
    assert len(pending_at_check) == 1
