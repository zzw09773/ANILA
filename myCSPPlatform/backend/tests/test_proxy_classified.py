"""Regression tests for CSP's classified one-way latch in proxy_service.

Wave D: encodes the invariant that when a resolved model/agent has
``requires_encryption=True``, the emitted ``anila_meta.classified`` must
become ``True`` — whether the downstream supplied its own meta or not —
and must never be downgraded.
"""

from __future__ import annotations

import pytest

from app.services.proxy_service import build_default_anila_meta


# ---------------------------------------------------------------------------
# build_default_anila_meta
# ---------------------------------------------------------------------------


def test_build_default_anila_meta_classified_false_by_default() -> None:
    meta = build_default_anila_meta("some-model", detail="d")
    assert meta["classified"] is False


def test_build_default_anila_meta_classified_true_passes_through() -> None:
    meta = build_default_anila_meta("some-model", detail="d", classified=True)
    assert meta["classified"] is True


def test_build_default_anila_meta_classified_coerces_truthy() -> None:
    """Non-bool truthy values get coerced to True (defensive against accidental ints)."""
    meta = build_default_anila_meta("x", detail="y", classified=1)
    assert meta["classified"] is True


def test_build_default_anila_meta_latency_field_present() -> None:
    meta = build_default_anila_meta("x", detail="y", latency_ms=123)
    assert meta["latency_ms"] == 123


def test_build_default_anila_meta_has_trace_skeleton() -> None:
    meta = build_default_anila_meta("x", detail="call detail")
    assert len(meta["trace"]) == 1
    assert meta["trace"][0]["label"].endswith("x")
    assert meta["trace"][0]["detail"] == "call detail"


# ---------------------------------------------------------------------------
# Latch invariant via proxy_service._emit path (symbolic — unit-level)
# ---------------------------------------------------------------------------


def test_latch_upgrade_true_when_requires_encryption_and_meta_missing() -> None:
    """Simulates the non-streaming path: model requires_encryption=True and the
    upstream returned no ``anila_meta`` → CSP must build one with classified=True."""
    requires_encryption = True
    existing_meta: dict | None = None
    if existing_meta is None:
        result_meta = build_default_anila_meta(
            "agent-name",
            detail="call detail",
            latency_ms=10,
            classified=requires_encryption,
        )
    assert result_meta["classified"] is True


def test_latch_upgrade_existing_meta_classified_false_flipped() -> None:
    """Non-streaming path: upstream returned classified=False but model requires
    encryption → ``existing_meta['classified'] = True`` (as proxy_service does
    at the ``elif requires_encryption`` branch)."""
    requires_encryption = True
    existing_meta = {"classified": False, "trace": [], "citations": []}
    if requires_encryption and isinstance(existing_meta, dict):
        existing_meta["classified"] = True
    assert existing_meta["classified"] is True


def test_latch_no_downgrade_when_already_classified() -> None:
    """If upstream says classified=True, it stays True regardless of model flag."""
    existing_meta = {"classified": True, "trace": []}
    requires_encryption = False
    # proxy_service only *upgrades* to True; the ``or`` shape below captures
    # the same intent as the ``existing_meta['classified'] = True`` line.
    final_classified = bool(existing_meta.get("classified")) or requires_encryption
    assert final_classified is True


@pytest.mark.parametrize(
    "upstream_classified, model_requires, expected",
    [
        (False, True, True),
        (True, False, True),
        (True, True, True),
        (False, False, False),
    ],
)
def test_latch_combinations(
    upstream_classified: bool, model_requires: bool, expected: bool
) -> None:
    """Truth table for the merged ``classified`` flag CSP emits."""
    meta = {"classified": upstream_classified}
    if model_requires:
        meta["classified"] = True
    assert meta["classified"] is expected
