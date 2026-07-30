# -*- coding: utf-8 -*-
"""P4.6 — bulk import from upstream ``/v1/models`` (SYSTEM-MAP §6 / OE-2 G5).

Network is never required: ``_fetch_upstream_model_listing`` is stubbed.
Covers happy path, endpoint-scoped inheritance, caps, audit sentinel,
upstream error hygiene, admin-tier grouping key (P4.6b; probe blocked at
registration), streamed body cap, missing-from-listing, activate-created,
review round-2 address leaks, and round-3 residues (import guard echo,
create-cap progress, listing-name length parity).
"""
from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.params import Depends

from app.api import models as models_api
from app.models.model_registry import ModelRegistry
from app.schemas.model_registry import (
    ModelBulkActivateCreatedRequest,
    ModelBulkImportRequest,
    ModelCreate,
)
from app.services.auth_service import get_current_user, require_admin
from tests.conftest import make_model, make_user


def _run(coro):
    return asyncio.run(coro)


def _stub_listing(monkeypatch, entries: list):
    async def _fake(endpoint_url, api_key):
        return entries

    monkeypatch.setattr(models_api, "_fetch_upstream_model_listing", _fake)


def _allow_https_endpoint(monkeypatch):
    """Permit the mock HTTPS endpoint used by make_model overrides / fixtures."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm,gateway.example.com,gemma4,other-gateway")


# ── happy path + inheritance ──────────────────────────────────────────────────


def test_bulk_import_happy_path_creates_from_stubbed_listing(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_bi", role="admin")
    source = make_model(db, name="seed-gateway")
    source.endpoint_url = "http://mock-llm:8080/v1"
    source.is_internal = True
    source.classification_ceiling = "營業秘密"
    source.context_window = 32768
    source.supports_streaming = True
    source.supports_json_schema = True
    source.supports_tools = False
    source.protocol = "openai_compatible"
    db.commit()

    _stub_listing(
        monkeypatch,
        [
            {"id": "gpt-oss-20b", "object": "model", "owned_by": "ncsist"},
            {"id": "nv-embed-v2", "object": "model"},
        ],
    )
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="10.0.0.1"))
    body = ModelBulkImportRequest(source_model_id=source.id)
    result = _run(models_api.import_models_from_endpoint(body, req, admin, db))

    assert result.created == 2
    assert result.already_existed == 0
    assert result.skipped == 0
    assert sorted(result.created_names) == ["gpt-oss-20b", "nv-embed-v2"]

    oss = db.query(ModelRegistry).filter(ModelRegistry.name == "gpt-oss-20b").one()
    assert oss.endpoint_url == "http://mock-llm:8080/v1"
    assert oss.model_type == "llm"
    assert oss.display_name == "gpt-oss-20b"
    assert oss.is_internal is True
    assert oss.classification_ceiling == "營業秘密"
    # Per-model facts are NOT copied from the sibling source row.
    assert oss.context_window is None
    assert oss.supports_streaming is True  # schema default
    assert oss.supports_json_schema is False  # schema default
    assert oss.supports_tools is False  # schema default
    assert oss.is_active is False  # pending admin review
    assert oss.description == "[上游來源 owned_by] ncsist"

    emb = db.query(ModelRegistry).filter(ModelRegistry.name == "nv-embed-v2").one()
    assert emb.model_type == "embedding"
    assert emb.classification_ceiling == "營業秘密"
    assert emb.is_active is False
    assert emb.supports_tools is False  # not inherited from source

    # Guessed fields enumerated per created row.
    by_name = {e.name: e.guessed_fields for e in result.created_entries}
    assert "context_window" in by_name["gpt-oss-20b"]
    assert "supports_tools" in by_name["nv-embed-v2"]


def test_bulk_import_inherits_endpoint_scoped_only_and_stays_inactive(db, monkeypatch):
    """Ceiling travels with the gateway; window/flags stay at schema defaults."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_ceil", role="admin")
    source = make_model(db, name="seed-ceil")
    source.endpoint_url = "http://mock-llm:8080/v1"
    source.classification_ceiling = "密"
    source.context_window = 8192
    source.supports_streaming = False
    source.supports_json_schema = False
    source.supports_tools = True
    db.commit()

    _stub_listing(monkeypatch, [{"id": "capped-model"}])
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert result.created == 1
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "capped-model").one()
    assert row.classification_ceiling == "密"
    assert row.context_window is None
    assert row.supports_streaming is True
    assert row.supports_json_schema is False
    assert row.supports_tools is False
    assert row.is_active is False
    assert "context_window" in result.created_entries[0].guessed_fields


def test_bulk_import_inheritance_driven_by_tuple():
    """``_IMPORT_INHERITED_FROM_SOURCE`` is the single source — no per-model flags."""
    assert "context_window" not in models_api._IMPORT_INHERITED_FROM_SOURCE
    assert "supports_streaming" not in models_api._IMPORT_INHERITED_FROM_SOURCE
    assert "supports_json_schema" not in models_api._IMPORT_INHERITED_FROM_SOURCE
    assert "supports_tools" not in models_api._IMPORT_INHERITED_FROM_SOURCE
    assert "endpoint_url" in models_api._IMPORT_INHERITED_FROM_SOURCE
    assert "classification_ceiling" in models_api._IMPORT_INHERITED_FROM_SOURCE
    assert models_api._IMPORT_COPY_SECRET_REF == "api_key_secret_ref"


# ── idempotence ───────────────────────────────────────────────────────────────


def test_bulk_import_second_run_is_idempotent(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_idemp", role="admin")
    source = make_model(db, name="seed-idemp")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    listing = [{"id": "model-a"}, {"id": "model-b"}]
    _stub_listing(monkeypatch, listing)
    req = SimpleNamespace(headers={}, client=None)
    body = ModelBulkImportRequest(source_model_id=source.id)

    first = _run(models_api.import_models_from_endpoint(body, req, admin, db))
    assert first.created == 2
    assert first.already_existed == 0

    second = _run(models_api.import_models_from_endpoint(body, req, admin, db))
    assert second.created == 0
    assert second.already_existed == 2
    assert second.skipped == 0
    assert {u.name for u in second.unchanged} == {"model-a", "model-b"}
    assert all("已登錄" in u.reason for u in second.unchanged)
    assert db.query(ModelRegistry).filter(
        ModelRegistry.name.in_(["model-a", "model-b"])
    ).count() == 2


# ── malformed / non-object skip ───────────────────────────────────────────────


def test_bulk_import_skips_malformed_without_aborting(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_mal", role="admin")
    source = make_model(db, name="seed-mal")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    _stub_listing(
        monkeypatch,
        [
            {"id": ""},  # empty
            {"object": "model"},  # missing id
            {"id": "good-model"},
            {"id": "x" * 201},  # too long for name column
            "not-an-object",  # non-object entry
            42,
        ],
    )
    req = SimpleNamespace(headers={}, client=None)
    body = ModelBulkImportRequest(source_model_id=source.id)
    result = _run(models_api.import_models_from_endpoint(body, req, admin, db))

    assert result.created == 1
    assert result.created_names == ["good-model"]
    assert result.skipped == 5
    reasons = [s.reason for s in result.skipped_entries]
    assert any("缺少或空白" in r for r in reasons)
    assert any("200 字元" in r for r in reasons)
    assert reasons.count("清單項目不是物件") == 2
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "good-model").one()


# ── preserve local edits / different endpoint ─────────────────────────────────


def test_bulk_import_preserves_existing_local_edits(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_keep", role="admin")
    source = make_model(db, name="seed-keep")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    existing = ModelRegistry(
        name="edited-model",
        display_name="Admin Custom Display",
        model_type="vlm",
        endpoint_url="http://mock-llm:8080/v1",
        description="admin wrote this",
        context_window=64000,
        is_internal=False,
        is_active=True,
    )
    db.add(existing)
    db.commit()

    _stub_listing(
        monkeypatch,
        [
            {
                "id": "edited-model",
                "owned_by": "should-not-overwrite-description",
            }
        ],
    )
    req = SimpleNamespace(headers={}, client=None)
    body = ModelBulkImportRequest(source_model_id=source.id)
    result = _run(models_api.import_models_from_endpoint(body, req, admin, db))

    assert result.created == 0
    assert result.already_existed == 1
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "edited-model").one()
    assert row.display_name == "Admin Custom Display"
    assert row.model_type == "vlm"
    assert row.description == "admin wrote this"
    assert row.context_window == 64000
    assert row.is_internal is False
    assert "本機設定已保留" in result.unchanged[0].reason
    assert "其他端點" not in result.unchanged[0].reason


def test_bulk_import_reports_different_endpoint_plainly(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_diffep", role="admin")
    source = make_model(db, name="seed-diffep")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    existing = ModelRegistry(
        name="elsewhere-model",
        display_name="Elsewhere",
        model_type="llm",
        endpoint_url="http://other-gateway:9090/v1",
        is_active=True,
    )
    db.add(existing)
    db.commit()

    _stub_listing(monkeypatch, [{"id": "elsewhere-model"}])
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert result.created == 0
    assert result.already_existed == 1
    assert "其他端點" in result.unchanged[0].reason
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "elsewhere-model").one()
    assert row.endpoint_url == "http://other-gateway:9090/v1"


def test_bulk_import_fills_only_absent_description(db, monkeypatch):
    """Absent (NULL) description may be filled; non-empty local text is left alone."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_fill", role="admin")
    source = make_model(db, name="seed-fill")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    bare = ModelRegistry(
        name="bare-model",
        display_name="Bare",
        model_type="llm",
        endpoint_url="http://mock-llm:8080/v1",
        description=None,
        is_active=True,
    )
    db.add(bare)
    db.commit()

    _stub_listing(
        monkeypatch,
        [{"id": "bare-model", "owned_by": "gateway-team"}],
    )
    req = SimpleNamespace(headers={}, client=None)
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id), req, admin, db
        )
    )
    assert result.already_existed == 1
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "bare-model").one()
    assert row.description == "[上游來源 owned_by] gateway-team"
    assert row.display_name == "Bare"  # local display_name untouched
    assert "已補齊空白欄位" in result.unchanged[0].reason
    assert "description" in result.unchanged[0].reason


def test_bulk_import_does_not_fill_description_on_other_endpoint(db, monkeypatch):
    """Description fill requires same endpoint as the source."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_nofill", role="admin")
    source = make_model(db, name="seed-nofill")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    other = ModelRegistry(
        name="cross-gw",
        display_name="Cross",
        model_type="llm",
        endpoint_url="http://other-gateway:9090/v1",
        description=None,
        is_active=True,
    )
    db.add(other)
    db.commit()

    _stub_listing(
        monkeypatch,
        [{"id": "cross-gw", "owned_by": "must-not-write"}],
    )
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert result.already_existed == 1
    row = db.query(ModelRegistry).filter(ModelRegistry.name == "cross-gw").one()
    assert row.description is None
    assert "已補齊" not in result.unchanged[0].reason
    assert "其他端點" in result.unchanged[0].reason


# ── unrecognised upstream payload ────────────────────────────────────────────


def test_parse_upstream_rejects_dict_without_data():
    with pytest.raises(HTTPException) as exc:
        models_api._parse_upstream_models_payload({"error": {"message": "nope"}})
    assert exc.value.status_code == 502
    assert "無法辨識" in str(exc.value.detail)


def test_parse_upstream_rejects_wrong_collection_key():
    with pytest.raises(HTTPException) as exc:
        models_api._parse_upstream_models_payload({"models": [{"id": "x"}]})
    assert exc.value.status_code == 502


def test_parse_upstream_accepts_openai_shape_and_bare_list():
    assert models_api._parse_upstream_models_payload(
        {"object": "list", "data": [{"id": "a"}, "skip-me"]}
    ) == [{"id": "a"}, "skip-me"]
    assert models_api._parse_upstream_models_payload([{"id": "b"}]) == [{"id": "b"}]


def test_bulk_import_unrecognised_payload_fails_loudly(db, monkeypatch):
    """Error body / wrong collection under success status must not look empty."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_badshape", role="admin")
    source = make_model(db, name="seed-badshape")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    async def _fake_error_body(endpoint_url, api_key):
        # Simulate what _fetch would raise after parsing an error-shaped body.
        return models_api._parse_upstream_models_payload(
            {"error": {"message": "upstream denied"}}
        )

    monkeypatch.setattr(models_api, "_fetch_upstream_model_listing", _fake_error_body)
    with pytest.raises(HTTPException) as exc:
        _run(
            models_api.import_models_from_endpoint(
                ModelBulkImportRequest(source_model_id=source.id),
                SimpleNamespace(headers={}, client=None),
                admin,
                db,
            )
        )
    assert exc.value.status_code == 502
    assert "無法辨識" in str(exc.value.detail)


# ── concurrent name collision ─────────────────────────────────────────────────


def test_bulk_import_concurrent_name_collision_reports(db, monkeypatch):
    """A race that inserts the same name after the snapshot must report, not abort."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_race", role="admin")
    source = make_model(db, name="seed-race")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    raced = ModelRegistry(
        name="race-model",
        display_name="Race",
        model_type="llm",
        endpoint_url="http://other-gateway:9090/v1",
        is_active=True,
    )
    db.add(raced)
    db.commit()

    # Blind the pre-batch snapshot so apply believes the name is free — then
    # UNIQUE on flush surfaces the concurrent registration.
    monkeypatch.setattr(models_api, "_registry_by_name", lambda _db: {})
    _stub_listing(monkeypatch, [{"id": "race-model"}, {"id": "after-race"}])

    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert result.created == 1
    assert result.created_names == ["after-race"]
    assert result.already_existed == 1
    assert result.unchanged[0].name == "race-model"
    assert "其他端點" in result.unchanged[0].reason
    # Batch did not abort; both outcomes recorded.
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "after-race").one()
    assert (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == "race-model")
        .one()
        .endpoint_url
        == "http://other-gateway:9090/v1"
    )


# ── audit: detail never carries address (even when owner acts) ────────────────


def test_bulk_import_audit_detail_never_carries_address_for_owner(db, monkeypatch):
    """BLOCKING: owner-acted import must still sentinel the audit detail text."""
    _allow_https_endpoint(monkeypatch)
    owner = make_user(db, "owner_audit", role="owner")
    source = make_model(db, name="seed-owner-audit")
    source.endpoint_url = "http://mock-llm:8080/v1"
    source.is_internal = True
    db.commit()

    _stub_listing(monkeypatch, [{"id": "audited-by-owner"}])
    captured: dict = {}

    def _fake_audit(db, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(models_api, "log_audit_event", _fake_audit)
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            owner,
            db,
        )
    )
    # Response may show the real URL to the owner …
    assert result.endpoint_url == "http://mock-llm:8080/v1"
    # … but detail must use the sentinel; real address only in owner-gated metadata.
    assert "mock-llm" not in captured["detail"]
    assert "8080" not in captured["detail"]
    assert models_api.ENDPOINT_INTERNAL in captured["detail"]
    assert captured["metadata"]["endpoint_url"] == "http://mock-llm:8080/v1"
    assert "created_names_sample" in captured["metadata"]
    assert "created_names" not in captured["metadata"]


def test_bulk_import_audit_redacts_endpoint_for_non_owner(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_audit", role="admin")
    source = make_model(db, name="seed-audit")
    source.endpoint_url = "http://mock-llm:8080/v1"
    source.is_internal = True
    db.commit()

    _stub_listing(monkeypatch, [{"id": "audited-model"}])
    captured: dict = {}

    def _fake_audit(db, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(models_api, "log_audit_event", _fake_audit)
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert result.endpoint_url == models_api.ENDPOINT_INTERNAL
    assert "mock-llm" not in captured["detail"]
    assert models_api.ENDPOINT_INTERNAL in captured["detail"]
    # Metadata keeps the real URL for owner forensics (listing owner-gates it).
    assert captured["metadata"]["endpoint_url"] == "http://mock-llm:8080/v1"


# ── upstream failure message must not carry address ───────────────────────────


def test_bulk_import_upstream_error_message_has_no_address(monkeypatch):
    """BLOCKING: client-facing 502 must not include the request URL."""
    address = "https://gateway.example.com:8443/v1"

    class _BoomStream:
        async def __aenter__(self):
            raise httpx.ConnectError(
                f"Connection refused to {address}/models",
                request=httpx.Request("GET", f"{address}/models"),
            )

        async def __aexit__(self, *args):
            return False

    def _boom_stream(self, method, url, **kwargs):
        return _BoomStream()

    monkeypatch.setattr(httpx.AsyncClient, "stream", _boom_stream)
    with pytest.raises(HTTPException) as exc:
        _run(models_api._fetch_upstream_model_listing(address, None))
    assert exc.value.status_code == 502
    detail = str(exc.value.detail)
    assert "gateway.example.com" not in detail
    assert "8443" not in detail
    assert "/v1/models" not in detail
    assert "無法從上游取得模型清單" in detail
    assert "ConnectError" in detail


# ── caps + truncated ──────────────────────────────────────────────────────────


def test_bulk_import_caps_entries_and_reports_truncated(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_cap", role="admin")
    source = make_model(db, name="seed-cap")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    cap = models_api._BULK_IMPORT_ENTRY_CAP
    listing = [{"id": f"m-{i}"} for i in range(cap + 7)]
    _stub_listing(monkeypatch, listing)
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert result.truncated == 7
    assert result.created == cap
    assert len(result.created_names) == cap


def test_bulk_import_second_run_creates_past_cap(db, monkeypatch):
    """Create-cap (not scan-cap): a second run reaches rows the first could not."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_cap2", role="admin")
    source = make_model(db, name="seed-cap2")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    cap = models_api._BULK_IMPORT_ENTRY_CAP
    listing = [{"id": f"prog-{i}"} for i in range(cap + 7)]
    _stub_listing(monkeypatch, listing)

    first = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert first.created == cap
    assert first.truncated == 7
    first_names = set(first.created_names)

    second = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert second.created == 7
    assert second.truncated == 0
    assert second.already_existed == cap
    second_names = set(second.created_names)
    assert first_names.isdisjoint(second_names)
    assert len(first_names | second_names) == cap + 7
    for name in second_names:
        assert (
            db.query(ModelRegistry).filter(ModelRegistry.name == name).one()
        )


def test_bulk_import_uses_to_thread_for_db_work(db, monkeypatch):
    """Database apply runs via asyncio.to_thread (off the event loop)."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_thread", role="admin")
    source = make_model(db, name="seed-thread")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()
    _stub_listing(monkeypatch, [{"id": "threaded-model"}])

    real_to_thread = asyncio.to_thread
    seen = {"called": False}

    async def _wrap(func, /, *args, **kwargs):
        seen["called"] = True
        assert func is models_api._apply_bulk_import_entries
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(models_api.asyncio, "to_thread", _wrap)
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert seen["called"] is True
    assert result.created == 1


def test_bulk_import_overlong_id_not_in_listed_names(db, monkeypatch):
    """Same length rule for listed_names and the create loop — no false retire."""
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_longid", role="admin")
    source = make_model(db, name="seed-longid")
    source.endpoint_url = "http://mock-llm:8080/v1"
    # Registry row whose name equals the truncated prefix of an over-long
    # upstream id — must still be reported missing (prefix must not enter
    # listed_names).
    prefix_row = ModelRegistry(
        name="x" * 200,
        display_name="Prefix",
        model_type="llm",
        endpoint_url="http://mock-llm:8080/v1",
        is_active=True,
    )
    db.add(prefix_row)
    db.commit()

    _stub_listing(
        monkeypatch,
        [
            {"id": "x" * 250},  # over-long → skipped, not listed
            {"id": "ok-model"},
        ],
    )
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert any(
        s.reason == "模型 id 超過 200 字元" for s in result.skipped_entries
    )
    missing_names = {m.name for m in result.missing_from_listing}
    assert ("x" * 200) in missing_names
    assert "seed-longid" in missing_names
    assert "ok-model" not in missing_names


# ── source ceiling validation ─────────────────────────────────────────────────


def test_bulk_import_rejects_stale_source_ceiling_once(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_stale", role="admin")
    source = make_model(db, name="seed-stale")
    source.endpoint_url = "http://mock-llm:8080/v1"
    source.classification_ceiling = "TOP_SECRET"  # retired vocabulary
    db.commit()

    called = {"fetch": False}

    async def _should_not_run(endpoint_url, api_key):
        called["fetch"] = True
        return [{"id": "x"}]

    monkeypatch.setattr(models_api, "_fetch_upstream_model_listing", _should_not_run)
    with pytest.raises(HTTPException) as exc:
        _run(
            models_api.import_models_from_endpoint(
                ModelBulkImportRequest(source_model_id=source.id),
                SimpleNamespace(headers={}, client=None),
                admin,
                db,
            )
        )
    assert exc.value.status_code == 400
    assert "分類上限" in str(exc.value.detail)
    assert "TOP_SECRET" in str(exc.value.detail)
    assert called["fetch"] is False


# ── missing from listing (report-only) ────────────────────────────────────────


def test_bulk_import_reports_missing_from_listing(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_miss", role="admin")
    source = make_model(db, name="seed-miss")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    retired = ModelRegistry(
        name="retired-upstream",
        display_name="Retired",
        model_type="llm",
        endpoint_url="http://mock-llm:8080/v1",
        is_active=True,
    )
    elsewhere = ModelRegistry(
        name="other-gw-only",
        display_name="Other",
        model_type="llm",
        endpoint_url="http://other-gateway:9090/v1",
        is_active=True,
    )
    db.add_all([retired, elsewhere])
    db.commit()

    _stub_listing(monkeypatch, [{"id": "still-there"}])
    result = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    missing_names = {m.name for m in result.missing_from_listing}
    assert "retired-upstream" in missing_names
    assert "seed-miss" in missing_names  # source row itself not in listing
    assert "other-gw-only" not in missing_names
    assert "still-there" not in missing_names
    # Nothing deleted.
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "retired-upstream").one()


# ── activate-created ──────────────────────────────────────────────────────────


def test_bulk_activate_created_scopes_to_source_endpoint(db, monkeypatch):
    _allow_https_endpoint(monkeypatch)
    admin = make_user(db, "admin_act", role="admin")
    source = make_model(db, name="seed-act")
    source.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    _stub_listing(monkeypatch, [{"id": "new-a"}, {"id": "new-b"}])
    imported = _run(
        models_api.import_models_from_endpoint(
            ModelBulkImportRequest(source_model_id=source.id),
            SimpleNamespace(headers={}, client=None),
            admin,
            db,
        )
    )
    assert imported.created == 2
    assert all(
        not db.query(ModelRegistry).filter(ModelRegistry.name == n).one().is_active
        for n in imported.created_names
    )

    # Plant a same-name on another gateway — must not be activated.
    other = ModelRegistry(
        name="foreign-name",
        display_name="Foreign",
        model_type="llm",
        endpoint_url="http://other-gateway:9090/v1",
        is_active=False,
    )
    db.add(other)
    db.commit()

    result = models_api.activate_created_from_import(
        ModelBulkActivateCreatedRequest(
            source_model_id=source.id,
            names=imported.created_names + ["foreign-name", "no-such"],
        ),
        SimpleNamespace(headers={}, client=None),
        admin,
        db,
    )
    assert result.activated == 2
    assert sorted(result.activated_names) == ["new-a", "new-b"]
    assert result.wrong_endpoint == 1
    assert result.not_found == 1
    assert db.query(ModelRegistry).filter(ModelRegistry.name == "new-a").one().is_active
    assert not db.query(ModelRegistry).filter(ModelRegistry.name == "foreign-name").one().is_active


# ── endpoint group key (admin-tier emission; helper still opaque) ─────────────


def _response_row(*, endpoint_url="http://mock-llm:8080/v1", is_internal=True):
    return SimpleNamespace(
        id=1,
        name="m",
        display_name="M",
        model_type="llm",
        endpoint_url=endpoint_url,
        api_version="v1",
        is_active=True,
        is_router_primary=False,
        health_status="healthy",
        health_checked_at=None,
        description=None,
        context_window=None,
        base_model_id=None,
        base_model=None,
        is_internal=is_internal,
        protocol="openai_compatible",
        classification_ceiling=None,
        owner_department_id=None,
        supports_streaming=True,
        supports_json_schema=False,
        supports_tools=False,
        api_key_secret_ref=None,
        created_at=None,
        updated_at=None,
    )


def test_endpoint_group_key_per_request_salt_and_opaque():
    """Helper: keys group within one salt; digest never carries the address."""
    salt_a = b"\x01" * 16
    salt_b = b"\x02" * 16
    url = "http://mock-llm:8080/v1"
    other = "http://other-gateway:9090/v1"

    a1 = models_api._endpoint_group_key(url, request_salt=salt_a)
    a2 = models_api._endpoint_group_key(url, request_salt=salt_a)
    a_other = models_api._endpoint_group_key(other, request_salt=salt_a)
    b1 = models_api._endpoint_group_key(url, request_salt=salt_b)

    assert a1 == a2  # same request salt → same key
    assert a1 != a_other  # different address → different key
    assert a1 != b1  # different request → different key
    assert "mock-llm" not in a1
    assert "8080" not in a1
    assert len(a1) == 64  # sha256 hex


def test_endpoint_group_key_default_salt_varies_across_calls():
    """Omitting request_salt draws a fresh random — not stable across calls."""
    url = "http://mock-llm:8080/v1"
    keys = {models_api._endpoint_group_key(url) for _ in range(5)}
    assert len(keys) == 5


def test_build_response_endpoint_group_key_admin_tier():
    """Admin-tier receives an opaque key; callers below admin receive none.

    Address remains owner-only even when the grouping key is present.
    """
    row = _response_row()
    salt = b"\xab" * 16
    owner_data = models_api._build_response(
        row, caller=SimpleNamespace(role="owner"), endpoint_group_salt=salt
    )
    admin_data = models_api._build_response(
        row, caller=SimpleNamespace(role="admin"), endpoint_group_salt=salt
    )
    user_data = models_api._build_response(
        row, caller=SimpleNamespace(role="user"), endpoint_group_salt=salt
    )
    expected = models_api._endpoint_group_key(
        row.endpoint_url, request_salt=salt
    )
    assert owner_data["endpoint_group_key"] == expected
    assert admin_data["endpoint_group_key"] == expected
    assert "mock-llm" not in admin_data["endpoint_group_key"]
    assert "8080" not in admin_data["endpoint_group_key"]
    assert admin_data["endpoint_url"] == models_api.ENDPOINT_INTERNAL
    assert user_data["endpoint_group_key"] == ""
    assert user_data["endpoint_url"] == models_api.ENDPOINT_INTERNAL


def test_list_models_group_key_oracle_fails_at_probe_registration(db, monkeypatch):
    """P4.6b: the former probe oracle dies at registration, not comparison.

    An undesignated administrator cannot create a throwaway row carrying a
    candidate address. Grouping keys are therefore safe to emit to admins
    for already-registered rows (same-endpoint merge in the import chooser).
    """
    # Public https — avoid ANILA_TRUSTED_HOSTS (TestClient lifespan would
    # backfill it into the process-local DB cache and pollute SSRF tests).
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    secret = "https://secret-gpu-box.example.com/v1"
    target = make_model(db, name="oracle-target")
    target.endpoint_url = secret
    target.is_internal = False
    db.commit()

    admin = make_user(db, "admin_oracle", role="admin")
    with pytest.raises(HTTPException) as exc:
        models_api.create_model(
            ModelCreate(
                name="oracle-probe",
                display_name="Oracle Probe",
                model_type="llm",
                endpoint_url=secret,
            ),
            admin,
            db,
        )
    assert exc.value.status_code == 403
    assert "端點位址設定權限" in str(exc.value.detail)
    assert (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == "oracle-probe")
        .first()
        is None
    )

    # Grouping restored for administrators on existing rows.
    rows = models_api.list_models(
        model_type=None, current_user=admin, db=db
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["oracle-target"]["endpoint_group_key"]
    assert by_name["oracle-target"]["endpoint_url"] == models_api.ENDPOINT_REDACTED

    # Same endpoint still groups for admins when both rows already exist
    # (planted without going through the gated create path).
    sibling = make_model(db, name="oracle-sibling")
    sibling.endpoint_url = secret
    sibling.is_internal = False
    db.commit()
    rows2 = models_api.list_models(
        model_type=None, current_user=admin, db=db
    )
    by2 = {r["name"]: r for r in rows2}
    assert (
        by2["oracle-target"]["endpoint_group_key"]
        == by2["oracle-sibling"]["endpoint_group_key"]
    )


# ── streamed upstream body cap ────────────────────────────────────────────────


def test_bulk_import_stream_body_cap_aborts_early(monkeypatch):
    """Oversized chunked body: existing message; stop near the cap, not whole body."""
    cap = models_api._BULK_IMPORT_BODY_MAX_BYTES
    chunk_size = 64 * 1024
    offered = {"n": 0}
    too_large = (
        f"上游模型清單回應過大（上限 {cap} 位元組）"
    )

    class _OversizeResp:
        status_code = 200
        headers: dict = {}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            # Offer far more than the cap; consumer must abort after crossing it.
            remaining = cap * 3
            while remaining > 0:
                n = min(chunk_size, remaining)
                offered["n"] += n
                remaining -= n
                yield b"x" * n

    class _StreamCM:
        def __init__(self):
            self._resp = _OversizeResp()

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *args):
            return False

    def _stream(self, method, url, **kwargs):
        return _StreamCM()

    monkeypatch.setattr(httpx.AsyncClient, "stream", _stream)
    with pytest.raises(HTTPException) as exc:
        _run(
            models_api._fetch_upstream_model_listing(
                "https://gateway.example.com/v1", None
            )
        )
    assert exc.value.status_code == 502
    assert exc.value.detail == too_large
    # One chunk past the cap is enough to trip the guard; never the full 3× body.
    assert offered["n"] <= cap + chunk_size
    assert offered["n"] < cap * 2


# ── SSRF guard ────────────────────────────────────────────────────────────────


def test_bulk_import_guard_refuses_disallowed_endpoint(db, monkeypatch):
    """Import must call the same ``_enforce_endpoint_url`` as create — loopback denied.

    Import path re-raises a fixed message (no hostname / resolved address).
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    admin = make_user(db, "admin_ssrf", role="admin")
    source = make_model(db, name="seed-ssrf")
    source.endpoint_url = "http://127.0.0.1:8000/v1"
    db.commit()

    called = {"fetch": False}

    async def _should_not_run(endpoint_url, api_key):
        called["fetch"] = True
        return []

    monkeypatch.setattr(models_api, "_fetch_upstream_model_listing", _should_not_run)
    req = SimpleNamespace(headers={}, client=None)
    with pytest.raises(HTTPException) as exc:
        _run(
            models_api.import_models_from_endpoint(
                ModelBulkImportRequest(source_model_id=source.id), req, admin, db
            )
        )
    assert exc.value.status_code == 400
    assert called["fetch"] is False
    detail = exc.value.detail
    assert isinstance(detail, str)
    assert detail == models_api._BULK_IMPORT_OUTBOUND_REJECTED
    assert "127.0.0.1" not in detail
    assert "8000" not in detail
    assert "localhost" not in detail.lower()
    assert "loopback" not in detail.lower()
    assert "擁有者" in detail


def test_bulk_import_guard_fixed_message_hides_private_resolved_address(
    db, monkeypatch
):
    """Hostname + resolved private address must not reach the import client."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    admin = make_user(db, "admin_ssrf2", role="admin")
    source = make_model(db, name="seed-ssrf2")
    # Single-label host that resolves (via guard) to a private IP — create
    # path would echo both; import must not.
    source.endpoint_url = "http://internal-gw:8080/v1"
    db.commit()

    from anila_core.security import UnsafeEndpointError

    def _boom(url, endpoint_kind="model"):
        raise UnsafeEndpointError(
            "endpoint_url host 'internal-gw' resolved to private "
            "(RFC 1918) address '10.53.100.12' "
            "(set ANILA_ALLOW_PRIVATE_ENDPOINT=1 in on-prem dev)",
            host="internal-gw",
            reason="private_ip",
        )

    monkeypatch.setattr(models_api, "validate_outbound_url", _boom)
    called = {"fetch": False}

    async def _should_not_run(endpoint_url, api_key):
        called["fetch"] = True
        return []

    monkeypatch.setattr(models_api, "_fetch_upstream_model_listing", _should_not_run)
    with pytest.raises(HTTPException) as exc:
        _run(
            models_api.import_models_from_endpoint(
                ModelBulkImportRequest(source_model_id=source.id),
                SimpleNamespace(headers={}, client=None),
                admin,
                db,
            )
        )
    assert exc.value.status_code == 400
    assert called["fetch"] is False
    detail = str(exc.value.detail)
    assert detail == models_api._BULK_IMPORT_OUTBOUND_REJECTED
    assert "internal-gw" not in detail
    assert "10.53.100.12" not in detail
    assert "8080" not in detail
    assert "RFC" not in detail
    assert "private" not in detail.lower()


# ── authorization parity ──────────────────────────────────────────────────────


def test_bulk_import_auth_dependency_stays_require_admin():
    """Import / activate-created stay admin-tier; create uses address-author gate.

    P4.6b narrowed create (address entry) to owner / designated developer.
    Batch import takes a registered row id — no address — so administrators
    keep ``require_admin``.
    """
    create_param = inspect.signature(models_api.create_model).parameters[
        "current_user"
    ]
    import_param = inspect.signature(
        models_api.import_models_from_endpoint
    ).parameters["admin"]
    assert isinstance(create_param.default, Depends)
    assert isinstance(import_param.default, Depends)
    assert create_param.default.dependency is get_current_user
    assert import_param.default.dependency is require_admin
    activate_param = inspect.signature(
        models_api.activate_created_from_import
    ).parameters["admin"]
    assert activate_param.default.dependency is require_admin


def test_bulk_import_require_admin_rejects_regular_user():
    """Import boundary: non-admin tier → 403 (unchanged from P4.6)."""
    user = SimpleNamespace(role="user", id=1, username="bob")
    with pytest.raises(HTTPException) as exc:
        require_admin(user)
    assert exc.value.status_code == 403
    assert "管理員" in str(exc.value.detail)
