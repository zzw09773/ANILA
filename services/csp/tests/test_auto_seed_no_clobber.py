"""OE-2 B3 — env seeds a model once; after that the row belongs to the admin.

auto_seed used to reassign endpoint_url on every startup for any model whose
name appears in AUTO_REGISTER_MODELS. An admin who changed an endpoint in the
console saw it save, and saw it silently revert on the next deploy — the same
silent-revert family as the rest of docs/FAKE-CONTROLS.md, and harder to catch
because the revert happens hours later during an unrelated restart.
"""
import inspect

from app.services import auto_seed


def test_seed_does_not_reassign_endpoint_url_for_existing_models():
    src = inspect.getsource(auto_seed)
    # The assignment that caused the revert must not come back.
    assert 'existing.endpoint_url = m["endpoint_url"]' not in src, (
        "auto_seed must not overwrite an existing model's endpoint_url — "
        "env creates the row, the admin owns it afterwards"
    )


def test_seed_still_creates_missing_models():
    """The other half: env must still be able to seed a model that is absent."""
    src = inspect.getsource(auto_seed)
    assert 'endpoint_url=m["endpoint_url"]' in src, (
        "creation must still take the endpoint from the seed config"
    )
