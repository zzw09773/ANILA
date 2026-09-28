"""Internal model calls keep max_concurrent on the snapshot."""

from app.models.model_registry import ModelRegistry
from app.services.model_gate import MemoryBoard, ModelAdmission
from app.services.proxy.snapshot import snapshot_model


def test_snapshot_copies_max_concurrent():
    row = ModelRegistry(
        id=7,
        name="embed",
        model_type="embedding",
        endpoint_url="http://embed/v1",
        max_concurrent=2,
    )
    snap = snapshot_model(row)
    admission = ModelAdmission.maybe(snap, 1, board=MemoryBoard())
    assert admission is not None
    assert admission.limit == 2


def test_snapshot_of_a_namespace_keeps_a_later_edit_off_the_original():
    from types import SimpleNamespace

    original = SimpleNamespace(id=1, name="m", max_concurrent=3)
    snap = snapshot_model(original)
    snap.max_concurrent = 9
    assert original.max_concurrent == 3
