"""Unit tests for the Router sentinel model auto-seed (auto_seed.py).

The sentinel is the NON-secret piece of the "ANILA Router auto" chat chain:
a create-if-missing ModelRegistry ``anila-router`` llm row. Discipline mirrors
the admin-user block — never overwrite an existing row, write no api_key, and
leave classification_ceiling at the least-privilege column default.
"""
from __future__ import annotations

from app.models.model_registry import ModelRegistry
from app.services.auto_seed import (
    ROUTER_SENTINEL_MODEL_NAME,
    seed_router_sentinel_model,
)


class TestRouterSentinelSeed:
    def test_seeds_when_url_set_and_row_absent(self, db):
        added = seed_router_sentinel_model(
            db, "http://router:9000", formal=False
        )
        db.flush()

        assert added is True
        row = (
            db.query(ModelRegistry)
            .filter(ModelRegistry.name == ROUTER_SENTINEL_MODEL_NAME)
            .one()
        )
        assert row.model_type == "llm"
        assert row.endpoint_url == "http://router:9000"
        # No secret written by the seed.
        assert row.api_key_secret_ref is None
        # Least-privilege column default, never raised by the seed.
        assert row.classification_ceiling == "無機密"
        # Non-formal dev deployment leaves the row routable.
        assert row.is_active is True

    def test_formal_governance_seeds_quarantined_row(self, db):
        added = seed_router_sentinel_model(
            db, "http://router:9000", formal=True
        )
        db.flush()

        assert added is True
        row = (
            db.query(ModelRegistry)
            .filter(ModelRegistry.name == ROUTER_SENTINEL_MODEL_NAME)
            .one()
        )
        # Formal governance keeps it a non-routable quarantine until an
        # operator supplies signed authority via the API boundary.
        assert row.is_active is False

    def test_skips_and_never_overwrites_existing_row(self, db):
        # An operator-edited row: different endpoint + a gateway api_key set.
        db.add(ModelRegistry(
            name=ROUTER_SENTINEL_MODEL_NAME,
            display_name="operator edited",
            model_type="llm",
            endpoint_url="http://router-edited:9000",
            api_version="v1",
            api_key_secret_ref="enc::v1::sentinel",
        ))
        db.flush()

        added = seed_router_sentinel_model(
            db, "http://router:9000", formal=False
        )
        db.flush()

        assert added is False
        rows = (
            db.query(ModelRegistry)
            .filter(ModelRegistry.name == ROUTER_SENTINEL_MODEL_NAME)
            .all()
        )
        assert len(rows) == 1
        # Untouched: endpoint, display name, and the operator's api_key survive.
        assert rows[0].endpoint_url == "http://router-edited:9000"
        assert rows[0].display_name == "operator edited"
        assert rows[0].api_key_secret_ref == "enc::v1::sentinel"

    def test_skips_when_url_empty(self, db):
        added = seed_router_sentinel_model(db, "", formal=False)
        db.flush()

        assert added is False
        assert (
            db.query(ModelRegistry)
            .filter(ModelRegistry.name == ROUTER_SENTINEL_MODEL_NAME)
            .first()
            is None
        )

    def test_skips_when_url_whitespace_only(self, db):
        added = seed_router_sentinel_model(db, "   ", formal=False)
        db.flush()

        assert added is False
        assert (
            db.query(ModelRegistry)
            .filter(ModelRegistry.name == ROUTER_SENTINEL_MODEL_NAME)
            .first()
            is None
        )
