"""``POST /api/models/{id}/set-platform-embedding`` — the real endpoint.

Replaces the Invariant-3 test that used to live at the bottom of
``test_platform_embedding.py``. That one never called the endpoint: it
re-implemented the designation UPDATE by hand, hand-rolled the warning
f-string, and then asserted on its own literal. Replacing the production
``set_platform_embedding`` with a function that only raises left it — and
five neighbours — green, so it was not testing anything that could break.

Everything here goes through ``TestClient`` and asserts on the response
the operator's console actually receives, so deleting either behaviour
this endpoint owns turns it red:

  * ``truncation_warning`` when the probed native dim exceeds the
    halfvec(4000) column width, and
  * ``index_mismatch_warning`` / ``previous_embedding_model`` when the
    designation *moves* — the moment every chunk indexed under the old
    model stops being retrievable.

The endpoint's outbound probe (``_probe_embedding_native_dim``) is the
only thing stubbed; it is an HTTP call to a model gateway that does not
exist in the test environment.

SQLite vs PostgreSQL: this endpoint touches ``model_registry`` only —
plain columns, no RLS, no pgvector — so the SQLite fixture exercises the
same code path production does. The *vector* side of the same defect is
covered in ``test_chunk_search_index_mismatch.py``, which documents its
own PostgreSQL caveat.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

import app.api.models as models_api
from app.models.model_registry import ModelRegistry
from app.services.auth_service import create_tokens

from tests.conftest import make_user


PROBED_NATIVE_DIM = 4096


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


@pytest.fixture
def admin(db):
    return make_user(db, username="embed-admin", role="admin")


@pytest.fixture(autouse=True)
def _stub_probe(monkeypatch):
    """The designation path probes the live embedding endpoint once."""

    async def fake_probe(model):
        return PROBED_NATIVE_DIM

    monkeypatch.setattr(models_api, "_probe_embedding_native_dim", fake_probe)


def _add_embedding(db, *, name: str, designated: bool = False) -> ModelRegistry:
    row = ModelRegistry(
        name=name,
        display_name=name,
        model_type="embedding",
        endpoint_url="http://embed.test/v1",
        is_active=True,
        is_platform_embedding=designated,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _add_collection(db, owner, *, name: str, embedding_model: str, status: str = "active"):
    """A knowledge base whose vectors were produced by ``embedding_model``.

    ``ingestion_collections.embedding_model`` is the platform's own record
    of index provenance and carries no RLS, which is what makes the
    designation check able to read it from an unscoped admin session.
    """
    from app.models.ingestion import IngestionCollection

    coll = IngestionCollection(
        name=name,
        chunking_config={"strategy": "semantic"},
        embedding_model=embedding_model,
        embedding_dim=4000,
        status=status,
        created_by=owner.id,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


# ── truncation warning ───────────────────────────────────────────────────────


def test_designation_endpoint_surfaces_truncation_warning(
    client: TestClient, db, admin,
):
    """A >4000-d model must come back with the truncation warning attached.

    Mutant: drop the ``truncation_warning`` assignment from the endpoint —
    the key goes None and this fails. (The old version of this test built
    the warning string itself, so the same mutation left it green.)
    """
    row = _add_embedding(db, name="nvidia/nv-embed-v2")

    resp = client.post(
        f"/api/models/{row.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["measured_native_dim"] == PROBED_NATIVE_DIM
    warning = body["truncation_warning"]
    assert warning is not None
    assert "截斷" in warning
    assert str(PROBED_NATIVE_DIM) in warning

    db.expire_all()
    stored = db.query(ModelRegistry).filter(ModelRegistry.id == row.id).one()
    assert stored.is_platform_embedding is True
    assert stored.embedding_native_dim == PROBED_NATIVE_DIM


# ── corpus state → index mismatch warning ───────────────────────────────────


def test_stranded_collection_is_reported_by_name(
    client: TestClient, db, admin,
):
    """A collection indexed under another model must be named to the operator.

    Retrieval filters chunks by the model that produced them, so the
    instant this call returns, that knowledge base answers nothing. The
    operator has to hear it here — they are the only one who can reindex.

    Mutant: delete the ``index_mismatch_warning`` assignment — the key is
    None and this fails.
    """
    _add_embedding(db, name="old/embedder", designated=True)
    new = _add_embedding(db, name="new/embedder")
    _add_collection(db, admin, name="法規知識庫", embedding_model="old/embedder")

    resp = client.post(
        f"/api/models/{new.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    warning = body["index_mismatch_warning"]
    assert warning is not None, "a corpus was stranded and the operator was told nothing"
    # Naming the collection is the actionable part — "something changed"
    # leaves the operator with nothing to reindex.
    assert "法規知識庫" in warning
    assert "new/embedder" in warning
    # The remedy that actually works, named where the operator sees it.
    assert "old/embedder" in warning, "the operator is not told what to set back to"
    assert "改回" in warning
    # And the honest cost of the alternative — there is no reindex button.
    assert "沒有重新索引的功能" in warning
    assert "重新上傳" in warning


def test_warning_survives_deactivating_the_old_model_first(
    client: TestClient, db, admin,
):
    """The order-independence fix, as its own test.

    Deactivating the old model before designating the new one is an
    ordinary operator sequence, and ``deactivate_model`` clears
    ``is_platform_embedding`` (models.py). The previous version compared
    the incoming model against ``resolve_platform_embedding``'s soft
    fallback, which by then resolved to the *new* model itself — equal,
    so no warning, while the corpus was just as stranded.

    Mutant: revert the ``stranded = _collections_not_indexed_under(...)``
    line to the old transition comparison — this test fails while
    ``test_stranded_collection_is_reported_by_name`` stays green, which
    is exactly how the gap hid the first time.
    """
    old = _add_embedding(db, name="old/embedder", designated=True)
    new = _add_embedding(db, name="new/embedder")
    _add_collection(db, admin, name="法規知識庫", embedding_model="old/embedder")

    # Step 1 — the operator deactivates the old model. This is what
    # clears the designation flag out from under the comparison.
    old.is_active = False
    old.is_platform_embedding = False
    db.commit()

    # Sanity: the "previous" name the old check relied on is now useless.
    from app.services.platform_embedding import resolve_platform_embedding

    resolved = resolve_platform_embedding(db)
    assert resolved is not None and resolved.name == "new/embedder", (
        "premise of this test: the soft fallback now resolves to the model "
        "about to be designated, so a before/after comparison sees no change"
    )

    # Step 2 — and now designates the new one.
    resp = client.post(
        f"/api/models/{new.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["index_mismatch_warning"] is not None, (
        "deactivating the old model first silently suppressed the warning"
    )
    assert "法規知識庫" in body["index_mismatch_warning"]


def test_casing_variant_of_the_same_model_is_not_called_stranded(
    client: TestClient, db, admin,
):
    """The live casing collision, guarded where it always runs.

    ``ingestion_collections.embedding_model`` DEFAULTs to
    ``nvidia/NV-embed-V2``; the same model registers as
    ``nvidia/nv-embed-v2``. A case-sensitive ``!=`` therefore reports the
    platform's own default configuration as stranded — a warning that is
    wrong on the most common setup, which teaches the operator to click
    through every warning including the true ones.

    Mutant: drop ``func.lower()`` from ``_collections_not_indexed_under``
    — this fails, in a default run, with no environment variables set.
    """
    row = _add_embedding(db, name="nvidia/nv-embed-v2")
    _add_collection(
        db, admin, name="法規知識庫", embedding_model="nvidia/NV-embed-V2",
    )

    resp = client.post(
        f"/api/models/{row.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["index_mismatch_warning"] is None, (
        "a collection carrying the column DEFAULT spelling of the very model "
        "being designated was reported as stranded"
    )


def test_archived_collections_do_not_raise_a_warning(
    client: TestClient, db, admin,
):
    """An archived collection cannot be searched, so it cannot be stranded.

    Warning about it would train the operator to dismiss the warning.
    """
    _add_embedding(db, name="old/embedder", designated=True)
    new = _add_embedding(db, name="new/embedder")
    _add_collection(
        db, admin, name="舊專案", embedding_model="old/embedder", status="archived",
    )

    resp = client.post(
        f"/api/models/{new.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["index_mismatch_warning"] is None


def test_many_stranded_collections_are_summarised_not_dumped(
    client: TestClient, db, admin,
):
    """The message must stay readable; the full list stays machine-readable."""
    _add_embedding(db, name="old/embedder", designated=True)
    new = _add_embedding(db, name="new/embedder")
    for i in range(8):
        _add_collection(db, admin, name=f"知識庫{i}", embedding_model="old/embedder")

    resp = client.post(
        f"/api/models/{new.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    warning = body = resp.json()["index_mismatch_warning"]
    assert "8" in warning
    assert "知識庫0" in warning
    # Not every name inlined — the last one is behind the summary.
    assert "知識庫7" not in warning


def test_no_collections_at_all_means_nothing_to_warn_about(
    client: TestClient, db, admin,
):
    """A fresh platform with no knowledge bases strands nothing."""
    _add_embedding(db, name="old/embedder", designated=True)
    new = _add_embedding(db, name="new/embedder")

    resp = client.post(
        f"/api/models/{new.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["index_mismatch_warning"] is None


def test_redesignating_the_same_model_does_not_cry_wolf(
    client: TestClient, db, admin,
):
    """Re-confirming the model the corpus is already indexed under strands
    nothing. A warning on every designation call would be ignored within a
    week, which is the same as not having one.
    """
    row = _add_embedding(db, name="steady/embedder", designated=True)
    _add_collection(db, admin, name="法規知識庫", embedding_model="steady/embedder")

    resp = client.post(
        f"/api/models/{row.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["index_mismatch_warning"] is None


def test_designation_move_is_recorded_in_the_audit_trail(
    client: TestClient, db, admin,
):
    """The previous model name must survive in audit, not just in a response
    body the console may or may not render."""
    import json

    from app.models.audit_log import AuditLog

    _add_embedding(db, name="old/embedder", designated=True)
    new = _add_embedding(db, name="new/embedder")
    _add_collection(db, admin, name="法規知識庫", embedding_model="old/embedder")

    resp = client.post(
        f"/api/models/{new.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 200, resp.text

    db.expire_all()
    entry = (
        db.query(AuditLog)
        .filter(AuditLog.action == "set_platform_embedding")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert entry is not None, "designation was not audited at all"
    meta = json.loads(entry.metadata_json or "{}")
    assert meta.get("previous_embedding_model") == "old/embedder"
    assert meta.get("has_stranded_collections") is True
    assert meta.get("stranded_collections") == ["法規知識庫"]
    assert meta.get("stranded_collections") == ["法規知識庫"]
    assert "old/embedder" in (entry.detail or "")


# ── route wiring / guards ────────────────────────────────────────────────────


def test_designation_rejects_non_embedding_model(client: TestClient, db, admin):
    row = ModelRegistry(
        name="chat/model",
        display_name="chat/model",
        model_type="chat",
        endpoint_url="http://chat.test/v1",
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    resp = client.post(
        f"/api/models/{row.id}/set-platform-embedding", headers=_bearer(admin),
    )
    assert resp.status_code == 400
    assert "embedding" in resp.json()["detail"]


def test_designation_requires_admin(client: TestClient, db):
    user = make_user(db, username="plain-user", role="user")
    row = _add_embedding(db, name="nvidia/nv-embed-v2")

    resp = client.post(
        f"/api/models/{row.id}/set-platform-embedding", headers=_bearer(user),
    )
    assert resp.status_code == 403

    db.expire_all()
    assert db.query(ModelRegistry).filter(
        ModelRegistry.id == row.id
    ).one().is_platform_embedding is False
