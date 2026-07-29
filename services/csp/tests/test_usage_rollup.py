"""P1.2 — usage rollup across the department tree (院 → 所 → 組).

Read-time aggregation: filtering by a node includes that node's own
TokenUsage rows plus all descendants. Write-path attribution stays
direct (unchanged).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.token_usage import TokenUsage
from app.services.usage_service import (
    export_usage_csv,
    get_top_departments,
    get_top_models,
    get_top_users,
    get_usage_summary,
)
from tests.conftest import make_model, make_user


TOKENS_A = 100  # 院
TOKENS_B = 200  # 所
TOKENS_C = 400  # 組
TOKENS_NONE = 50
TOKENS_D = 800  # unrelated


def _seed_tree_and_usage(db: Session):
    """院(A)→所(B)→組(C) plus unrelated D; one usage row per attribution."""
    dept_a = Department(name="rollup院A", parent_id=None, is_active=True)
    db.add(dept_a)
    db.flush()
    dept_b = Department(name="rollup所B", parent_id=dept_a.id, is_active=True)
    db.add(dept_b)
    db.flush()
    dept_c = Department(name="rollup組C", parent_id=dept_b.id, is_active=True)
    dept_d = Department(name="rollup無關D", parent_id=None, is_active=True)
    db.add_all([dept_c, dept_d])
    db.flush()

    user_a = make_user(db, username="rollup_user_a")
    user_b = make_user(db, username="rollup_user_b")
    user_c = make_user(db, username="rollup_user_c")
    user_none = make_user(db, username="rollup_user_none")
    user_d = make_user(db, username="rollup_user_d")
    model = make_model(db, name="rollup-model")

    now = datetime.now(timezone.utc)
    rows = [
        (user_a, dept_a.id, TOKENS_A),
        (user_b, dept_b.id, TOKENS_B),
        (user_c, dept_c.id, TOKENS_C),
        (user_none, None, TOKENS_NONE),
        (user_d, dept_d.id, TOKENS_D),
    ]
    for user, dept_id, tokens in rows:
        db.add(
            TokenUsage(
                api_key_id=None,
                user_id=user.id,
                department_id=dept_id,
                model_id=model.id,
                prompt_tokens=tokens // 2,
                completion_tokens=tokens - tokens // 2,
                total_tokens=tokens,
                request_timestamp=now,
            )
        )
    db.commit()

    return dept_a, dept_b, dept_c, dept_d


def test_department_filter_rolls_up_descendants(db: Session):
    dept_a, dept_b, dept_c, dept_d = _seed_tree_and_usage(db)

    summary_c = get_usage_summary(db, range_key="24h", department_id=dept_c.id)
    summary_b = get_usage_summary(db, range_key="24h", department_id=dept_b.id)
    summary_a = get_usage_summary(db, range_key="24h", department_id=dept_a.id)
    summary_all = get_usage_summary(db, range_key="24h", department_id=None)
    summary_d = get_usage_summary(db, range_key="24h", department_id=dept_d.id)

    assert summary_c["total_tokens"] == TOKENS_C
    assert summary_b["total_tokens"] == TOKENS_B + TOKENS_C
    assert summary_a["total_tokens"] == TOKENS_A + TOKENS_B + TOKENS_C

    # PLAN acceptance: 組的和 = 所的一部分 = 院的一部分 (strict nesting)
    assert summary_c["total_tokens"] < summary_b["total_tokens"]
    assert summary_b["total_tokens"] < summary_a["total_tokens"]

    # Global filter unchanged: includes None + D + A/B/C
    assert summary_all["total_tokens"] == (
        TOKENS_A + TOKENS_B + TOKENS_C + TOKENS_NONE + TOKENS_D
    )
    assert summary_all["total_requests"] == 5

    # Unrelated D never appears in A/B/C scopes (exact equality above is the proof)
    assert summary_d["total_tokens"] == TOKENS_D


def test_get_top_departments_keeps_direct_attribution(db: Session):
    """Under a subtree filter, ranking stays direct-attribution per department."""
    dept_a, dept_b, dept_c, _dept_d = _seed_tree_and_usage(db)

    # Filter by 院A: rows for A, B, C each keep their own direct tokens (not rolled up)
    rows_a = get_top_departments(db, limit=10, department_id=dept_a.id)
    by_id_a = {r["department_id"]: r for r in rows_a}
    assert set(by_id_a) == {dept_a.id, dept_b.id, dept_c.id}
    assert by_id_a[dept_a.id]["total_tokens"] == TOKENS_A
    assert by_id_a[dept_b.id]["total_tokens"] == TOKENS_B
    assert by_id_a[dept_c.id]["total_tokens"] == TOKENS_C

    # Filter by 所B: only B and C rows, still direct values
    rows_b = get_top_departments(db, limit=10, department_id=dept_b.id)
    by_id_b = {r["department_id"]: r for r in rows_b}
    assert set(by_id_b) == {dept_b.id, dept_c.id}
    assert by_id_b[dept_b.id]["total_tokens"] == TOKENS_B
    assert by_id_b[dept_c.id]["total_tokens"] == TOKENS_C


def test_nonexistent_department_returns_empty(db: Session):
    """Nonexistent department_id → empty result (no exception, no 404)."""
    _seed_tree_and_usage(db)

    summary = get_usage_summary(db, range_key="24h", department_id=999999)
    assert summary["total_tokens"] == 0
    assert summary["total_requests"] == 0

    assert get_top_departments(db, limit=10, department_id=999999) == []


def test_top_models_and_users_respect_department_filter(db: Session):
    """Other entry points also expand department_id via _department_scope_ids.

    get_chart_data uses Postgres-only SQL and cannot run on SQLite — skipped here.
    """
    dept_a, dept_b, _dept_c, _dept_d = _seed_tree_and_usage(db)

    models = get_top_models(db, department_id=dept_a.id)
    assert sum(r["total_tokens"] for r in models) == TOKENS_A + TOKENS_B + TOKENS_C

    users = get_top_users(db, department_id=dept_b.id)
    assert sum(r["total_tokens"] for r in users) == TOKENS_B + TOKENS_C


def test_export_usage_csv_respects_subtree_expansion(db: Session):
    _dept_a, dept_b, dept_c, _dept_d = _seed_tree_and_usage(db)

    csv_text = export_usage_csv(db, range_key="24h", department_id=dept_b.id)
    # BOM + header + B row + C row (subtree); not A, D, or None
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    # header + 2 data rows
    assert len(lines) == 3
    assert "rollup所B" in csv_text
    assert "rollup組C" in csv_text
    assert "rollup院A" not in csv_text
    assert "rollup無關D" not in csv_text
