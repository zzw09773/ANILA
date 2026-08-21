"""Site 4 — usage CSV formula neutralization.

username / department_name / model_name / model_type are string cells.
Removing ``csv_formula_safe`` on any of those four turns the matching
assertion red independently of the other sites.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.model_registry import ModelRegistry
from app.models.token_usage import TokenUsage
from app.services.usage_service import export_usage_csv
from app.utils.csv_formula import FORMULA_TRIGGER_PREFIXES
from tests.conftest import make_user


_USER_COL = 1
_DEPT_COL = 2
_MODEL_COL = 3
_TYPE_COL = 4


def _csv_rows(text: str) -> list[list[str]]:
    assert text[:1] == "\ufeff"
    return list(csv.reader(io.StringIO(text[1:])))


def _seed_usage(
    db: Session,
    *,
    username: str,
    department_name: str,
    model_name: str,
    model_type: str,
    tag: str,
) -> None:
    dept = Department(name=department_name, parent_id=None, is_active=True)
    db.add(dept)
    db.flush()
    user = make_user(db, username=username, department_id=dept.id)
    model = ModelRegistry(
        name=f"usage-formula-{tag}",
        display_name=model_name,
        model_type=model_type,
        endpoint_url="http://mock-llm:8080",
        is_active=True,
    )
    db.add(model)
    db.flush()
    db.add(
        TokenUsage(
            api_key_id=None,
            user_id=user.id,
            department_id=dept.id,
            model_id=model.id,
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
            request_timestamp=datetime.now(timezone.utc),
        )
    )
    db.commit()


def _data_row(db: Session) -> list[str]:
    rows = _csv_rows(export_usage_csv(db, range_key="24h"))
    assert len(rows) >= 2, rows
    return rows[1]


@pytest.mark.parametrize("prefix", FORMULA_TRIGGER_PREFIXES)
def test_usage_csv_neutralizes_username_for_each_trigger(
    db: Session, prefix: str
):
    tag = prefix.encode("unicode_escape").decode("ascii")
    payload = f"{prefix}user"
    _seed_usage(
        db,
        username=payload,
        department_name=f"dept-{tag}",
        model_name=f"model-{tag}",
        model_type="llm",
        tag=f"user-{tag}",
    )
    cell = _data_row(db)[_USER_COL]
    assert cell[:1] == "'"
    assert cell[1:] == payload


def test_usage_csv_neutralizes_department_model_name_and_type(db: Session):
    """The four named string fields are independent injection points."""
    _seed_usage(
        db,
        username="=user",
        department_name="+dept",
        model_name="-model",
        model_type="@typ",
        tag="four-fields",
    )
    row = _data_row(db)
    assert row[_USER_COL][:1] == "'" and row[_USER_COL][1:] == "=user"
    assert row[_DEPT_COL][:1] == "'" and row[_DEPT_COL][1:] == "+dept"
    assert row[_MODEL_COL][:1] == "'" and row[_MODEL_COL][1:] == "-model"
    assert row[_TYPE_COL][:1] == "'" and row[_TYPE_COL][1:] == "@typ"


def test_usage_csv_leaves_numeric_token_columns_unprefixed(db: Session):
    _seed_usage(
        db,
        username="plain-user",
        department_name="plain-dept",
        model_name="plain-model",
        model_type="llm",
        tag="numeric",
    )
    row = _data_row(db)
    assert row[5] == "3"
    assert row[6] == "4"
    assert row[7] == "7"
