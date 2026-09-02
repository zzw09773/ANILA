"""The health probe must present the model's own gateway key.

2026-09-02, live: the owner registered a model behind a LiteLLM proxy
(``http://…:4000/v1``) with a valid ``sk-`` virtual key. Bulk import and chat
worked (they resolve the per-model key), but 「測試連線」 and the background
loop stayed ``unknown`` forever, because the probe deliberately sends no
credentials and LiteLLM answers 401 even on ``/health``. To the owner that
reads as "the key I just saved did not take".

Invariant: when a model has a gateway key, the probe sends it as a Bearer on
the real probe paths; a 2xx then means ``healthy``. Without a key the probe
stays anonymous and a 401 still means ``unknown`` (never green on auth
failure — the 2026-07-31 rotated-key false green must not come back).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api import models as models_api
from app.services import health_checker
from app.services.health_checker import HEALTH_HEALTHY, HEALTH_UNKNOWN
from app.services.service_token_envelope import encode_service_token_envelope

from tests.conftest import make_model, make_user

KEY = "sk-test-virtual-key"


def _gateway_client(monkeypatch, record: list):
    """A LiteLLM-shaped fake: every path is 401 unless the right Bearer is sent."""

    class _Resp:
        def __init__(self, status_code: int):
            self.status_code = status_code

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            headers = kwargs.get("headers") or {}
            record.append((url, headers.get("Authorization")))
            ok = headers.get("Authorization") == f"Bearer {KEY}"
            return _Resp(200 if ok else 401)

    monkeypatch.setattr(health_checker.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")


def test_probe_with_key_sends_bearer_and_reports_healthy(monkeypatch):
    record: list = []
    _gateway_client(monkeypatch, record)
    status, _ = asyncio.run(
        health_checker.probe_model_health_detailed(
            "http://mock-llm:4000/v1", skip_validate=True, api_key=KEY
        )
    )
    assert status == HEALTH_HEALTHY
    # Kill: drop the header → every probe is 401 → unknown.
    assert record and all(auth == f"Bearer {KEY}" for _, auth in record)


def test_probe_without_key_stays_anonymous_and_unknown_on_401(monkeypatch):
    record: list = []
    _gateway_client(monkeypatch, record)
    status, _ = asyncio.run(
        health_checker.probe_model_health_detailed(
            "http://mock-llm:4000/v1", skip_validate=True
        )
    )
    assert status == HEALTH_UNKNOWN
    assert record and all(auth is None for _, auth in record)


def test_test_connection_endpoint_passes_the_models_own_key(db, monkeypatch):
    admin = make_user(db, "admin_pk", role="admin")
    m = make_model(db, name="behind-litellm")
    m.api_key_secret_ref = encode_service_token_envelope(KEY)
    db.commit()
    seen: dict = {}

    async def fake_probe(url, **kwargs):
        seen.update(kwargs)
        return (HEALTH_HEALTHY, 7)

    monkeypatch.setattr(models_api, "probe_model_health_detailed", fake_probe)
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    asyncio.run(models_api.test_model(m.id, req, admin, db))
    assert seen.get("api_key") == KEY


def test_background_loop_targets_carry_the_key(db):
    """The loop's target rows must include the resolved key (kill: drop it)."""
    m = make_model(db, name="loop-litellm")
    m.api_key_secret_ref = encode_service_token_envelope(KEY)
    m.is_active = True
    db.commit()
    targets = health_checker._model_probe_targets(db)
    row = next(t for t in targets if t["model_id"] == m.id)
    assert row["api_key"] == KEY


# ── 2026-09-02 16:44, live: the third 「更新」 stored a screenshot file name
# (「螢幕快照 2026-…」) as the key — the clipboard held the file, not the key.
# Two guards: the write path refuses a key that cannot be a bearer token, and
# the probe never crashes on one that slipped in (it probes anonymously).

import pytest
from pydantic import ValidationError

from app.schemas.model_registry import ModelCreate, ModelUpdate


@pytest.mark.parametrize("bad", ["螢幕快照 2026-09-02 16.43.12.png", "sk-abc def", "sk-abc\ndef"])
def test_update_and_create_refuse_a_key_that_cannot_be_a_bearer_token(bad):
    with pytest.raises(ValidationError) as exc:
        ModelUpdate(api_key=bad)
    assert "金鑰" in str(exc.value)
    with pytest.raises(ValidationError):
        ModelCreate(name="m", display_name="m", model_type="llm", endpoint_url="http://x/v1", api_key=bad)


def test_update_still_accepts_a_real_key_and_empty_means_keep():
    assert ModelUpdate(api_key="sk-example-virtual-key-000000").api_key == "sk-example-virtual-key-000000"
    assert ModelUpdate(api_key="  ").api_key == ""
    assert ModelUpdate().api_key is None


def test_probe_with_a_non_ascii_key_probes_anonymously_instead_of_crashing(monkeypatch):
    record: list = []
    _gateway_client(monkeypatch, record)
    status, _ = asyncio.run(
        health_checker.probe_model_health_detailed(
            "http://mock-llm:4000/v1", skip_validate=True, api_key="螢幕快照 2026-09-02.png"
        )
    )
    assert status == HEALTH_UNKNOWN
    assert record and all(auth is None for _, auth in record)
