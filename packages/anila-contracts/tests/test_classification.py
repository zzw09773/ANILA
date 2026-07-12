from __future__ import annotations

import pytest

from anila_contracts import Classification
from anila_contracts.classification import ClassificationLevel

ORDERED_VALUES = ["無機密", "營業秘密", "機密", "極機密", "絕對機密"]


def test_internal_class_name_and_public_contract_share_one_authoritative_type() -> None:
    assert Classification is ClassificationLevel
    assert [level.value for level in Classification] == ORDERED_VALUES
    assert [level.rank for level in Classification] == list(range(5))


def test_classification_order_latch_and_storage_round_trip() -> None:
    levels = [
        Classification.from_storage("無機密"),
        Classification.from_storage("機密"),
        Classification.from_storage("營業秘密"),
    ]
    assert Classification.max_of(levels) is Classification.CONFIDENTIAL
    assert Classification.from_storage(Classification.SECRET.to_storage()) is Classification.SECRET


def test_legacy_floor_matches_existing_csp_contract() -> None:
    assert Classification.from_legacy_classified(False) is Classification.UNCLASSIFIED
    assert Classification.from_legacy_classified(True) is Classification.CONFIDENTIAL


def test_classification_fails_closed_for_empty_or_unknown_input() -> None:
    with pytest.raises(ValueError, match="至少一個"):
        Classification.max_of([])
    with pytest.raises(ValueError, match="未知的分類等級"):
        Classification.from_storage("公開")
