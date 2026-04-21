"""Regression tests for the Router's classified one-way latch + manifest flow.

Wave B/D: encodes the security invariant that once any path in the Router
observes ``requires_encryption`` or downstream ``classified=true``, the merged
``anila_meta.classified`` is pinned to ``True`` and never downgraded.
"""

from __future__ import annotations

import pytest

from anila_core.api.router_server import _merge_anila_meta, _normalize_anila_meta
from anila_core.registry.remote_agent_manifest import RemoteAgentManifest


# ---------------------------------------------------------------------------
# _merge_anila_meta: one-way latch invariants
# ---------------------------------------------------------------------------


def test_merge_anila_meta_classified_override_true_upgrades() -> None:
    """Manifest requires encryption → merged meta latches to classified=True."""
    merged = _merge_anila_meta(
        base_trace=[{"kind": "thinking", "label": "x", "detail": "y", "status": "ok"}],
        downstream_meta={"classified": False, "trace": []},
        agent_id="agent-a",
        latency_ms=42,
        classified_override=True,
    )
    assert merged["classified"] is True


def test_merge_anila_meta_downstream_classified_preserved() -> None:
    """Downstream anila.meta classified=True is preserved even without override."""
    merged = _merge_anila_meta(
        base_trace=[],
        downstream_meta={"classified": True},
        agent_id="agent-b",
        latency_ms=10,
        classified_override=False,
    )
    assert merged["classified"] is True


def test_merge_anila_meta_no_signal_stays_unclassified() -> None:
    merged = _merge_anila_meta(
        base_trace=[],
        downstream_meta={"classified": False},
        agent_id=None,
        latency_ms=10,
        classified_override=False,
    )
    assert merged["classified"] is False


def test_merge_anila_meta_override_wins_over_false_downstream() -> None:
    """If downstream explicitly says classified=False but agent is classified,
    we MUST NOT downgrade — this is the core latch security guarantee."""
    merged = _merge_anila_meta(
        base_trace=[],
        downstream_meta={"classified": False, "trace": [], "citations": []},
        agent_id="classified-agent",
        latency_ms=1,
        classified_override=True,
    )
    assert merged["classified"] is True, (
        "Router must never downgrade classified — a misbehaving agent claiming "
        "classified=false on an encryption-required model is a security bug."
    )


def test_merge_anila_meta_override_false_with_downstream_true_still_classified() -> None:
    """Override=False + downstream classified=True must still latch."""
    merged = _merge_anila_meta(
        base_trace=[],
        downstream_meta={"classified": True},
        agent_id="ag",
        latency_ms=1,
        classified_override=False,
    )
    assert merged["classified"] is True


# ---------------------------------------------------------------------------
# _merge_anila_meta: trace + handoff_chain shape
# ---------------------------------------------------------------------------


def test_merge_anila_meta_prepends_router_handoff_entry_when_dispatched() -> None:
    merged = _merge_anila_meta(
        base_trace=[{"kind": "thinking", "label": "L", "detail": "D", "status": "ok"}],
        downstream_meta={"handoff_chain": [{"agent_id": "other", "label": "other"}]},
        agent_id="target-agent",
        latency_ms=99,
    )
    chain = merged["handoff_chain"]
    assert chain[0]["agent_id"] == "anila-router"
    assert chain[0]["output_summary"] == "dispatch to target-agent"
    assert chain[1]["agent_id"] == "other"


def test_merge_anila_meta_concats_base_trace_before_downstream() -> None:
    base = [{"kind": "a", "label": "a", "detail": "a", "status": "ok"}]
    downstream = {"trace": [{"kind": "b", "label": "b", "detail": "b", "status": "ok"}]}
    merged = _merge_anila_meta(base, downstream, agent_id=None, latency_ms=0)
    assert [step["kind"] for step in merged["trace"]] == ["a", "b"]


def test_merge_anila_meta_latency_populated() -> None:
    merged = _merge_anila_meta(
        base_trace=[],
        downstream_meta=None,
        agent_id=None,
        latency_ms=12345,
    )
    assert merged["latency_ms"] == 12345


# ---------------------------------------------------------------------------
# _normalize_anila_meta
# ---------------------------------------------------------------------------


def test_normalize_anila_meta_none_returns_default_skeleton() -> None:
    skeleton = _normalize_anila_meta(None)
    assert skeleton["classified"] is False
    assert skeleton["trace"] == []
    assert skeleton["citations"] == []
    assert skeleton["handoff_chain"] == []
    assert skeleton["follow_ups"] == []


def test_normalize_anila_meta_preserves_existing_fields() -> None:
    src = {
        "trace_id": "custom",
        "trace": [{"kind": "x", "label": "x", "detail": "x", "status": "ok"}],
        "citations": [{"source_uri": "s", "snippet": "sn"}],
        "classified": True,
    }
    out = _normalize_anila_meta(src)
    assert out["trace_id"] == "custom"
    assert out["classified"] is True
    assert len(out["trace"]) == 1
    assert len(out["citations"]) == 1


# ---------------------------------------------------------------------------
# RemoteAgentManifest.requires_encryption flow
# ---------------------------------------------------------------------------


def test_manifest_requires_encryption_defaults_false() -> None:
    m = RemoteAgentManifest(
        agent_id="a",
        name="A",
        description_for_router="desc",
        endpoint_url="http://a",
    )
    assert m.requires_encryption is False


def test_manifest_requires_encryption_parsed_from_bool() -> None:
    m = RemoteAgentManifest(
        agent_id="a",
        name="A",
        description_for_router="desc",
        endpoint_url="http://a",
        requires_encryption=True,
    )
    assert m.requires_encryption is True


@pytest.mark.parametrize(
    "manifest_requires, downstream_classified, expected",
    [
        (True, False, True),   # agent-required wins
        (True, True, True),    # both say yes
        (False, True, True),   # downstream-reported wins
        (False, False, False), # neither
    ],
)
def test_router_latch_combinations(
    manifest_requires: bool, downstream_classified: bool, expected: bool
) -> None:
    merged = _merge_anila_meta(
        base_trace=[],
        downstream_meta={"classified": downstream_classified},
        agent_id="test",
        latency_ms=1,
        classified_override=manifest_requires,
    )
    assert merged["classified"] is expected
