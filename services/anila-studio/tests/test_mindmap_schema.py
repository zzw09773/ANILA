"""Schema-level tests for the Mindmap pipeline contract.

Covers:
  * Recursive ``MindmapNode`` validation (the model_rebuild call must
    have resolved the forward reference).
  * ``MindmapSpec`` accepts a 5-level-deep tree (the schema's depth
    limit is enforced in the pipeline runner, not the schema — the
    schema is intentionally generous so the runner has room to clip).
  * ``GenerateMindmapRequest`` honours max_depth bounds (1 ≤ d ≤ 5)
    and top_k bounds (1 ≤ k ≤ 20).
  * Preset enum round-trips through JSON.

These are pure pydantic-validator tests; no I/O, no LLM, no subprocess.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.mindmap import (
    GenerateMindmapRequest,
    MindmapJobStatus,
    MindmapNode,
    MindmapPreset,
    MindmapSpec,
)


# ── MindmapNode ──────────────────────────────────────────────────────────


def test_mindmapnode_leaf_has_empty_children_by_default():
    node = MindmapNode(id="n0", label="root")
    assert node.children == []
    assert node.note is None


def test_mindmapnode_accepts_nested_children():
    """The recursive self-reference must resolve so we can build a
    multi-level tree without dict<->model gymnastics."""
    leaf = MindmapNode(id="leaf", label="leaf")
    mid = MindmapNode(id="mid", label="mid", children=[leaf])
    root = MindmapNode(id="root", label="root", children=[mid])
    assert root.children[0].children[0].label == "leaf"


def test_mindmapnode_rejects_empty_label():
    with pytest.raises(ValidationError):
        MindmapNode(id="x", label="")


def test_mindmapnode_rejects_too_long_label():
    with pytest.raises(ValidationError):
        MindmapNode(id="x", label="a" * 201)


def test_mindmapnode_rejects_empty_id():
    with pytest.raises(ValidationError):
        MindmapNode(id="", label="ok")


def test_mindmapnode_note_caps_at_500_chars():
    with pytest.raises(ValidationError):
        MindmapNode(id="x", label="ok", note="x" * 501)


# ── MindmapSpec ──────────────────────────────────────────────────────────


def _five_deep() -> MindmapNode:
    """Helper: build a chain root → c1 → c2 → c3 → c4 (5 levels)."""
    c4 = MindmapNode(id="c4", label="lvl4")
    c3 = MindmapNode(id="c3", label="lvl3", children=[c4])
    c2 = MindmapNode(id="c2", label="lvl2", children=[c3])
    c1 = MindmapNode(id="c1", label="lvl1", children=[c2])
    return MindmapNode(id="root", label="lvl0", children=[c1])


def test_mindmapspec_accepts_deep_tree():
    """The schema itself does NOT bound depth — the pipeline runner
    truncates at request.max_depth. Confirm the validator doesn't
    spuriously reject deep trees."""
    spec = MindmapSpec(
        title="Deep tree",
        preset=MindmapPreset.CONCEPT_TREE,
        root=_five_deep(),
    )
    assert spec.layout == "LR"  # default
    # Walk verifies the recursive shape survived validation.
    cursor = spec.root
    depth = 0
    while cursor.children:
        cursor = cursor.children[0]
        depth += 1
    assert depth == 4  # 5 levels total → 4 hops from root


def test_mindmapspec_defaults_layout_to_LR():
    spec = MindmapSpec(
        title="t",
        preset=MindmapPreset.CONCEPT_TREE,
        root=MindmapNode(id="r", label="r"),
    )
    assert spec.layout == "LR"


@pytest.mark.parametrize("layout", ["TB", "LR", "BT", "RL"])
def test_mindmapspec_accepts_all_four_layouts(layout: str):
    spec = MindmapSpec(
        title="t",
        preset=MindmapPreset.CONCEPT_TREE,
        root=MindmapNode(id="r", label="r"),
        layout=layout,  # type: ignore[arg-type]
    )
    assert spec.layout == layout


def test_mindmapspec_rejects_unknown_layout():
    with pytest.raises(ValidationError):
        MindmapSpec(
            title="t",
            preset=MindmapPreset.CONCEPT_TREE,
            root=MindmapNode(id="r", label="r"),
            layout="SIDEWAYS",  # type: ignore[arg-type]
        )


def test_mindmapspec_preset_enum_roundtrip():
    payload = {
        "title": "demo",
        "preset": "sop_flow",  # string form, as the LLM emits
        "root": {"id": "r", "label": "r", "children": []},
        "layout": "TB",
    }
    spec = MindmapSpec.model_validate(payload)
    assert spec.preset is MindmapPreset.SOP_FLOW
    # Dump back to dict — enum becomes its string value.
    dumped = spec.model_dump(mode="json")
    assert dumped["preset"] == "sop_flow"


# ── GenerateMindmapRequest ──────────────────────────────────────────────


def test_request_defaults_match_brief():
    req = GenerateMindmapRequest(
        collection_id=1, preset=MindmapPreset.CONCEPT_TREE,
    )
    assert req.max_depth == 3
    assert req.top_k == 8
    assert req.seed_query is None
    assert req.document_ids is None


@pytest.mark.parametrize("d", [0, -1, 6, 100])
def test_request_rejects_out_of_range_max_depth(d: int):
    with pytest.raises(ValidationError):
        GenerateMindmapRequest(
            collection_id=1, preset=MindmapPreset.CONCEPT_TREE, max_depth=d,
        )


@pytest.mark.parametrize("k", [0, -1, 21, 1000])
def test_request_rejects_out_of_range_top_k(k: int):
    with pytest.raises(ValidationError):
        GenerateMindmapRequest(
            collection_id=1, preset=MindmapPreset.CONCEPT_TREE, top_k=k,
        )


# ── MindmapJobStatus ────────────────────────────────────────────────────


def test_jobstatus_serialises_datetimes_isoformat():
    now = datetime.now(timezone.utc)
    status_obj = MindmapJobStatus(
        job_id="m_abc",
        state="done",
        preset=MindmapPreset.CONCEPT_TREE,
        title="demo",
        node_count=4,
        download_urls={"svg": "/api/mindmaps/jobs/m_abc/download/svg"},
        created_at=now,
        updated_at=now,
    )
    payload = status_obj.model_dump(mode="json")
    assert payload["state"] == "done"
    assert payload["preset"] == "concept_tree"
    assert payload["download_urls"]["svg"].endswith("/download/svg")
    # ISO 8601 string round trips back to a datetime.
    assert isinstance(payload["created_at"], str)
    assert "T" in payload["created_at"]
