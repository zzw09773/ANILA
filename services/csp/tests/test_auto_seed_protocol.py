"""Task 4 (backend adapter): auto_seed 模型自動註冊路徑寫入/同步 protocol 欄位。

不寫入 protocol 會讓 llama.cpp 等非 openai_compatible 後端無法透過
AUTO_REGISTER_MODELS seed 標記正確 protocol。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.model_registry import ModelRegistry
from app.services import auto_seed


@pytest.fixture(autouse=True)
def _patch_session_local(db_engine, monkeypatch):
    """Point auto_seed.SessionLocal at the in-memory test engine so
    auto_seed() reads/writes the same tables as the ``db`` fixture —
    otherwise it opens sessions on the file-backed SQLite from the
    DATABASE_URL env var, which has no tables, and the write silently
    no-ops (swallowed by auto_seed()'s broad except + rollback).
    """
    SessionFactory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(auto_seed, "SessionLocal", SessionFactory)


def test_seed_new_model_writes_protocol(db, monkeypatch):
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_MODELS",
        '[{"name":"llama-x","endpoint_url":"http://172.16.120.35:18018","protocol":"llamacpp"}]',
    )
    auto_seed.auto_seed()
    row = db.query(ModelRegistry).filter_by(name="llama-x").first()
    assert row is not None
    assert row.protocol == "llamacpp"


def test_seed_update_syncs_protocol(db, monkeypatch):
    db.add(ModelRegistry(
        name="llama-y",
        display_name="Y",
        model_type="llm",
        endpoint_url="http://old:8000",
        protocol="openai_compatible",
    ))
    db.commit()
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_MODELS",
        '[{"name":"llama-y","endpoint_url":"http://172.16.120.35:18018","protocol":"llamacpp"}]',
    )
    auto_seed.auto_seed()
    row = db.query(ModelRegistry).filter_by(name="llama-y").first()
    assert row.protocol == "llamacpp"


def test_seed_new_model_defaults_protocol_when_absent(db, monkeypatch):
    """No ``protocol`` key in config → default stays openai_compatible."""
    monkeypatch.setattr(
        auto_seed.settings,
        "AUTO_REGISTER_MODELS",
        '[{"name":"llama-z","endpoint_url":"http://172.16.120.35:18019"}]',
    )
    auto_seed.auto_seed()
    row = db.query(ModelRegistry).filter_by(name="llama-z").first()
    assert row is not None
    assert row.protocol == "openai_compatible"
