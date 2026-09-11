"""Transport usage must not inflate inference token totals."""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.token_usage import TokenUsage
from app.services.usage_service import get_usage_summary
from tests.conftest import make_model, make_user


def test_router_transport_tokens_excluded_from_inference_totals(db: Session):
    user = make_user(db, username="usage-alice")
    glm = make_model(db, name="glm-usage")
    now = datetime.now(timezone.utc)
    db.add_all([
        TokenUsage(
            user_id=user.id,
            model_id=glm.id,
            prompt_tokens=100,
            completion_tokens=100,
            total_tokens=200,
            request_timestamp=now,
            usage_kind="inference",
            invocation_id="inv-1",
            token_source="reported",
            model_name_snapshot="glm-usage",
        ),
        TokenUsage(
            user_id=user.id,
            model_id=glm.id,
            prompt_tokens=100,
            completion_tokens=100,
            total_tokens=200,
            request_timestamp=now,
            usage_kind="inference",
            invocation_id="inv-2",
            token_source="reported",
            model_name_snapshot="glm-usage",
        ),
        TokenUsage(
            user_id=user.id,
            model_id=glm.id,
            prompt_tokens=200,
            completion_tokens=0,
            total_tokens=200,
            request_timestamp=now,
            usage_kind="router_transport",
            invocation_id="inv-transport",
            token_source="reported",
            model_name_snapshot="anila-router",
        ),
    ])
    db.commit()
    summary = get_usage_summary(db, range_key="24h", user_id=user.id)
    assert summary["total_tokens"] == 400
    assert summary["total_requests"] == 2


import asyncio
from app.services.usage_writer import _flush_batch


def test_duplicate_invocation_does_not_drop_batch(db, db_engine, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr("app.services.usage_writer.SessionLocal", sessionmaker(bind=db_engine, expire_on_commit=False))
    user = make_user(db, username="dup-user")
    glm = make_model(db, name="glm-dup")
    now = datetime.now(timezone.utc)
    row = dict(user_id=user.id, model_id=glm.id, prompt_tokens=10, completion_tokens=10, total_tokens=20, request_timestamp=now, usage_kind="inference", invocation_id="same-id", token_source="reported", model_name_snapshot="glm-dup", request_type="chat", legacy_runtime_call=False)
    other = dict(row); other["invocation_id"]="other-id"; other["total_tokens"]=30
    asyncio.run(_flush_batch([row]))
    asyncio.run(_flush_batch([row, other]))
    rows = db.query(TokenUsage).filter(TokenUsage.user_id==user.id).all()
    ids={r.invocation_id for r in rows}
    assert "same-id" in ids and "other-id" in ids
    assert sum(r.total_tokens for r in rows)==50
