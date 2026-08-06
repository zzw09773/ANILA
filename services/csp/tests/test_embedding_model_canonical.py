"""#56 — a new collection must record the model name the registry uses.

The defect, stated as data rather than code: ``model_registry.name`` is
this platform's canonical name for a model, but
``ingestion_collections.embedding_model`` was written from somewhere
else — an API payload nobody normalised, or migration 0014's column
default ``'nvidia/NV-embed-V2'`` — while the same model registers as
``nvidia/nv-embed-v2``. Migration ``r1_0018`` then copied that column
onto chunk provenance, and retrieval filters chunks on provenance. One
wrong capital and a fully-indexed corpus answers nothing, with no error
and no log line.

2026-08-05 loosened the read-side comparisons to case-insensitive, which
stopped the bleeding but also removed every symptom — so the data defect
kept being committed once per new collection, silently. These tests pin
the write side: whatever spelling arrives, what lands in the row is the
registry's.

Production edits that turn these red are named per test.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

import app.api.ingestion.search as search_mod
from anila_core.storage.adapters.pgvector_store import SourceModelCoverage
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.services.platform_embedding import (
    LAST_RESORT_EMBEDDING_MODEL,
    canonical_embedding_model_name,
)

from tests.conftest import login, make_user

# Not invented for the test. Read out of the live database on 2026-08-05
# and recorded in FAKE-CONTROLS #56:
#   ingestion_collections.embedding_model DEFAULT = 'nvidia/NV-embed-V2'
#   model_registry.name                           = 'nvidia/nv-embed-v2'
REGISTERED = "nvidia/nv-embed-v2"
LIVE_MISCASED = "nvidia/NV-embed-V2"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _payload(name: str, *, embedding_model: str | None = None) -> dict:
    body: dict = {
        "name": name,
        "chunking_config": {"strategy": "fixed", "params": {"size": 256}},
        "embedding_dim": 4000,
    }
    if embedding_model is not None:
        body["embedding_model"] = embedding_model
    return body


def _register_model(
    db,
    name: str,
    *,
    model_type: str = "embedding",
    is_active: bool = True,
    designated: bool = False,
) -> ModelRegistry:
    row = ModelRegistry(
        name=name,
        display_name=name,
        model_type=model_type,
        endpoint_url="http://embed.test/v1",
        is_active=is_active,
        is_platform_embedding=designated,
        embedding_native_dim=4096 if model_type == "embedding" else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _register_embedding(db, name: str, *, designated: bool = True) -> ModelRegistry:
    return _register_model(db, name, designated=designated)


def _create(client, db, token: str, name: str, **kwargs) -> IngestionCollection:
    resp = client.post(
        "/api/ingestion/collections", headers=_auth(token), json=_payload(name, **kwargs)
    )
    assert resp.status_code == 201, resp.text
    row = db.get(IngestionCollection, resp.json()["id"])
    assert row is not None
    return row


# ── the write path ───────────────────────────────────────────────────────────


class TestCreateStoresTheRegistrySpelling:
    def test_miscased_payload_is_stored_as_the_registry_spells_it(self, client, db):
        """The exact live collision, arriving through the API.

        Mutant: drop the ``canonical_embedding_model_name`` call in
        ``create_collection`` and the row keeps the caller's capitals —
        this fails on the stored value.
        """
        _register_embedding(db, REGISTERED)
        user = make_user(db, username="canon_payload", role="developer")
        token = login(client, user.username)

        row = _create(client, db, token, "canon-kb", embedding_model=LIVE_MISCASED)

        assert row.embedding_model == REGISTERED
        assert row.embedding_model != LIVE_MISCASED

    def test_omitted_payload_stores_the_designated_model_name(self, client, db):
        """No payload → the designation, which is already a registry name."""
        _register_embedding(db, REGISTERED)
        user = make_user(db, username="canon_omit", role="developer")
        token = login(client, user.username)

        row = _create(client, db, token, "canon-omit-kb")

        assert row.embedding_model == REGISTERED

    def test_last_resort_default_is_spelled_the_way_the_model_registers(
        self, client, db
    ):
        """Nothing registered at all still must not seed the collision.

        This is the branch migration 0014's column default used to
        mirror: no designation, no registry row, and the platform writes
        a literal. It has to be the literal the model actually uses.

        Mutant: put ``"nvidia/NV-embed-V2"`` back as the last resort —
        this fails.
        """
        user = make_user(db, username="canon_lastresort", role="developer")
        token = login(client, user.username)

        row = _create(client, db, token, "canon-bare-kb")

        assert row.embedding_model == LAST_RESORT_EMBEDDING_MODEL
        assert row.embedding_model == REGISTERED
        assert row.embedding_model != LIVE_MISCASED

    def test_unregistered_name_is_stored_verbatim_not_guessed(self, client, db):
        """No registry match → no invention.

        Silently rewriting an unknown name to something registered would
        bind a corpus to a model the caller never asked for. Search
        already refuses an unregistered name loudly ("is not registered
        in model_registry"), and loud beats convenient.
        """
        _register_embedding(db, REGISTERED)
        user = make_user(db, username="canon_unknown", role="developer")
        token = login(client, user.username)

        row = _create(client, db, token, "canon-unknown-kb", embedding_model="acme/emb-1")

        assert row.embedding_model == "acme/emb-1"

    def test_an_unrelated_chat_model_does_not_make_the_embedder_ambiguous(
        self, client, db
    ):
        """A ``model_type='llm'`` row sharing the name is not a candidate.

        These columns record which model *produced a vector*; a chat
        model cannot have. If it were counted, one deactivated
        ``NVIDIA/NV-Embed-V2`` chat registration would push the
        distinct-name count for that case-folded key to 2 and the real
        embedder's rows would be skipped for ever — the fix would be
        disarmed by an unrelated inventory entry, and silently.

        Mutant: drop ``ModelRegistry.model_type == "embedding"`` from
        ``canonical_embedding_model_name`` — this fails.
        """
        _register_model(db, REGISTERED, designated=True)
        _register_model(db, LIVE_MISCASED, model_type="llm", is_active=False)
        user = make_user(db, username="canon_decoy", role="developer")
        token = login(client, user.username)

        row = _create(client, db, token, "canon-decoy-kb", embedding_model=LIVE_MISCASED)

        assert row.embedding_model == REGISTERED

    def test_a_deactivated_embedder_is_still_a_canonical_spelling(self, client, db):
        """``is_active`` is deliberately NOT filtered.

        A deactivated embedder is still the right name for the vectors it
        already produced — #56 item 9 and the 409 message both tell the
        operator to re-designate (and if necessary reactivate) exactly
        that model. Filtering it out would strand the corpus it built.
        """
        _register_model(db, REGISTERED, is_active=False)
        user = make_user(db, username="canon_inactive", role="developer")
        token = login(client, user.username)

        row = _create(
            client, db, token, "canon-inactive-kb", embedding_model=LIVE_MISCASED
        )

        assert row.embedding_model == REGISTERED

    def test_two_registry_spellings_leave_the_request_alone(self, client, db):
        """#56 item 8: ``model_registry.name`` has no case-insensitive key.

        Two rows differing only in case is the original defect's own
        shape, and picking one of them here would be a coin toss the
        caller cannot see. Left untouched, and said so in the log.
        """
        _register_embedding(db, REGISTERED)
        _register_embedding(db, LIVE_MISCASED, designated=False)
        user = make_user(db, username="canon_ambig", role="developer")
        token = login(client, user.username)

        row = _create(client, db, token, "canon-ambig-kb", embedding_model="NVIDIA/NV-EMBED-V2")

        assert row.embedding_model == "NVIDIA/NV-EMBED-V2"


class TestCanonicaliser:
    def test_blank_and_missing_mean_no_opinion(self, db):
        assert canonical_embedding_model_name(db, None) is None
        assert canonical_embedding_model_name(db, "   ") is None

    def test_surrounding_whitespace_still_finds_the_registry_row(self, db):
        _register_embedding(db, REGISTERED)
        assert canonical_embedding_model_name(db, f"  {LIVE_MISCASED} ") == REGISTERED

    def test_exact_match_is_returned_unchanged(self, db):
        _register_embedding(db, REGISTERED)
        assert canonical_embedding_model_name(db, REGISTERED) == REGISTERED


# ── the consequence at the search path ───────────────────────────────────────


class _StubChunk:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubHit:
    def __init__(self, *, chunk, score, parent_content=None):
        self.chunk = chunk
        self.score = score
        self.parent_content = parent_content


class _CaseSensitiveStore:
    """A store whose rows carry one provenance string, matched exactly.

    The production filter is case-INsensitive today, and deliberately so
    (it still carries rows written before this package). Matching
    case-sensitively here is the point: it asserts the *data* no longer
    depends on that leniency, which is the only way a test can tell the
    difference between "the collision is gone" and "the collision is
    being tolerated".
    """

    def __init__(self, row_source_model: str, *, doc_id: int) -> None:
        self._row = row_source_model
        self._doc_id = doc_id

    def _hits(self):
        return [
            _StubHit(
                chunk=_StubChunk(
                    id=9001,
                    document_id=self._doc_id,
                    chunk_key=f"chunk:{self._doc_id}:9001",
                    content="第三條 承辦單位應於七日內完成審查。",
                    metadata={},
                    token_count=18,
                    parent_chunk_id=None,
                    chunk_type="leaf",
                    chunk_level=0,
                ),
                score=0.42,
            )
        ]

    async def similarity_search(
        self, *, query_embedding, top_k, min_score, source_model=None, **_kwargs
    ):
        if source_model is not None and source_model != self._row:
            return []
        return self._hits()

    async def source_model_coverage(self, source_model: str) -> SourceModelCoverage:
        matching = source_model == self._row
        return SourceModelCoverage(
            has_matching=matching,
            has_other=not matching,
            sample_other_model=None if matching else self._row,
        )


@pytest.fixture()
def _stub_embed(monkeypatch):
    async def fake_embed_query(db, user, model_name, dim, query):
        return [0.1] * dim

    monkeypatch.setattr(search_mod, "_embed_query", fake_embed_query)


def test_collection_created_from_a_miscased_payload_retrieves_its_chunks(
    client, db, monkeypatch, _stub_embed
):
    """End of the chain: create miscased, index, search, get the chunk back.

    The chunk's provenance is set to whatever the collection row ended up
    holding — that is precisely what migration r1_0018 did
    (``SET embedding_source_model = ic.embedding_model``), so it models
    the live corpus rather than a convenient fixture.

    Mutant: drop the ``canonical_embedding_model_name`` call in
    ``create_collection``. The row stores ``nvidia/NV-embed-V2``, the
    designation is ``nvidia/nv-embed-v2``, the exact match fails and the
    endpoint answers 409 with an empty corpus instead of the passage.
    """
    _register_embedding(db, REGISTERED)
    user = make_user(db, username="canon_search", role="developer")
    token = login(client, user.username)
    coll = _create(client, db, token, "canon-search-kb", embedding_model=LIVE_MISCASED)

    doc = IngestionDocument(
        collection_id=coll.id,
        filename="regulation.pdf",
        sha256="c" * 64,
        mime_type="application/pdf",
        status="indexed",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    store = _CaseSensitiveStore(coll.embedding_model, doc_id=doc.id)
    monkeypatch.setattr(
        search_mod, "CollectionScopedPgVectorStore", lambda pool, collection_id: store
    )
    monkeypatch.setattr(search_mod, "get_pool", lambda: object())

    resp = client.post(
        f"/api/ingestion/collections/{coll.id}/search",
        headers=_auth(token),
        json={"query": "審查期限是幾天"},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["results"]) == 1
    assert body["embedding_model"] == REGISTERED
