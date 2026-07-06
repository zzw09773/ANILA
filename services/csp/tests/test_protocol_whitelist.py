import pytest
from datetime import datetime
from pydantic import ValidationError
from app.schemas.model_registry import ModelCreate, ModelResponse

_BASE = dict(name="m", display_name="M", model_type="llm", endpoint_url="http://x:8000")

def test_create_accepts_whitelisted():
    assert ModelCreate(**_BASE, protocol="llamacpp").protocol == "llamacpp"
    assert ModelCreate(**_BASE).protocol == "openai_compatible"  # 預設不變

def test_create_rejects_unimplemented_backend():
    with pytest.raises(ValidationError):
        ModelCreate(**_BASE, protocol="triton")  # Phase 1+2 未實作 → 422

def test_response_tolerates_db_residue():
    # DB 殘值（舊 custom_adapter）不得炸 response
    now = datetime.now()
    r = ModelResponse(
        id=1, name="m", display_name="M", model_type="llm",
        endpoint_url="http://x:8000", api_version="v1", is_active=True,
        health_status="healthy", health_checked_at=None, description=None,
        context_window=None, protocol="custom_adapter",
        created_at=now, updated_at=now,
    )
    assert r.protocol == "custom_adapter"
