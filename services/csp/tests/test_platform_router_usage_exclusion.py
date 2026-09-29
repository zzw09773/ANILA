"""治理中心的模型排行與依模型圖表不把平台對話入口算進去。

用量列留在 token_usage。入口列通常是有請求、0 token（token 已記在真正回答的模型）。
彙總、總計圖與 CSV 仍算這些列。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.token_usage import TokenUsage
from app.services.usage_service import (
    export_usage_csv,
    get_chart_data,
    get_top_models,
    get_usage_summary,
)
from tests.conftest import make_model, make_user


def _sqlite_chart(monkeypatch) -> None:
    import app.services.usage_service as usage_service

    real_lc = usage_service.literal_column

    def _sqlite_bucket_lc(clause, *args, **kwargs):
        if isinstance(clause, str) and "EXTRACT(EPOCH FROM request_timestamp)" in clause:
            clause = clause.replace(
                "CAST(EXTRACT(EPOCH FROM request_timestamp) AS INTEGER)",
                "CAST(strftime('%s', request_timestamp) AS INTEGER)",
            )
        return real_lc(clause, *args, **kwargs)

    monkeypatch.setattr(usage_service, "literal_column", _sqlite_bucket_lc)


def _seed(db: Session):
    user = make_user(db, username="entry-usage")
    entry = make_model(db, name="anila-router")
    entry.display_name = "ANILA"
    real = make_model(db, name="glm-real")
    real.display_name = "GLM"
    now = datetime.now(timezone.utc)
    db.add_all(
        [
            TokenUsage(
                user_id=user.id,
                model_id=entry.id,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                request_timestamp=now,
            ),
            TokenUsage(
                user_id=user.id,
                model_id=real.id,
                prompt_tokens=10,
                completion_tokens=30,
                total_tokens=40,
                request_timestamp=now,
            ),
        ]
    )
    db.commit()
    return entry, real


def test_top_models_omit_platform_chat_entry_even_when_it_has_requests(db: Session):
    _seed(db)

    ranked = get_top_models(db, range_key="24h")

    assert [row["model_name"] for row in ranked] == ["GLM"]
    assert ranked[0]["total_tokens"] == 40
    assert ranked[0]["total_requests"] == 1


def test_model_chart_omits_platform_chat_entry(db: Session, monkeypatch):
    _seed(db)
    _sqlite_chart(monkeypatch)

    chart = get_chart_data(db, "24h", group_by="model")

    assert {series["name"] for series in chart["series"]} == {"GLM"}


def test_summary_total_chart_and_csv_keep_platform_chat_entry_rows(
    db: Session, monkeypatch
):
    entry, _real = _seed(db)
    _sqlite_chart(monkeypatch)

    summary = get_usage_summary(db, range_key="24h")
    assert summary["total_requests"] == 2
    assert summary["total_tokens"] == 40

    total = get_chart_data(db, "24h", group_by="total")
    assert sum(total["series"][0]["data"]) == 40

    csv_text = export_usage_csv(db, range_key="24h")
    assert "GLM" in csv_text
    assert "ANILA" in csv_text
    assert db.query(TokenUsage).filter(TokenUsage.model_id == entry.id).count() == 1
