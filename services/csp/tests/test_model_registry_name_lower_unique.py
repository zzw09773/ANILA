"""model_registry.name is unique case-insensitively (r1_0043)."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.model_registry import ModelRegistry


def _row(name: str) -> ModelRegistry:
    return ModelRegistry(
        name=name,
        display_name=name,
        model_type="embedding",
        endpoint_url="http://embed:8000/v1",
        is_active=True,
    )


def test_model_registry_rejects_case_variant_name(db):
    db.add(_row("nvidia/nv-embed-v2"))
    db.commit()

    db.add(_row("nvidia/NV-embed-V2"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    names = {row.name for row in db.query(ModelRegistry).all()}
    assert names == {"nvidia/nv-embed-v2"}


def test_model_registry_still_accepts_distinct_names(db):
    db.add(_row("nvidia/nv-embed-v2"))
    db.add(_row("bge-m3"))
    db.commit()
    names = {row.name for row in db.query(ModelRegistry).all()}
    assert names == {"nvidia/nv-embed-v2", "bge-m3"}
