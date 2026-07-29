# -*- coding: utf-8 -*-
"""ClassificationLevel 契約測試。

依 SYSTEM-MAP.md §8:四級分類(無機密 < 營業秘密 < 密 < 機密)、
單向閂鎖(effective level = max)、舊 boolean classified 的 floor
backfill 映射(true → 機密 / SECRET)。
"""

import pytest

from app.schemas.contracts import ClassificationLevel


# 由低到高的完整鏈,順序即契約(SYSTEM-MAP §8「排序不可變」)。
ORDERED_VALUES = ["無機密", "營業秘密", "密", "機密"]


class TestOrdering:
    def test_four_levels_exact_values(self):
        assert [level.value for level in ClassificationLevel] == ORDERED_VALUES

    def test_full_ordering_chain(self):
        levels = list(ClassificationLevel)
        for lower, higher in zip(levels, levels[1:]):
            assert lower < higher
            assert higher > lower
            assert lower <= higher
            assert higher >= lower
            assert lower != higher

    def test_rank_matches_system_map_numbering(self):
        assert [level.rank for level in ClassificationLevel] == [0, 1, 2, 3]

    def test_reflexive_comparison(self):
        for level in ClassificationLevel:
            assert level <= level
            assert level >= level
            assert not level < level

    def test_comparison_with_non_level_raises_type_error(self):
        with pytest.raises(TypeError):
            ClassificationLevel.from_storage("密") < "機密"


class TestMaxOf:
    def test_max_of_mixed_list(self):
        mixed = [
            ClassificationLevel.from_storage("營業秘密"),
            ClassificationLevel.from_storage("機密"),
            ClassificationLevel.from_storage("無機密"),
            ClassificationLevel.from_storage("密"),
        ]
        assert ClassificationLevel.max_of(mixed) == ClassificationLevel.from_storage(
            "機密"
        )

    def test_one_way_latch_property(self):
        # 單向閂鎖:觀測到較高分類後只能維持或升級,不得降回。
        latched = ClassificationLevel.max_of(
            [
                ClassificationLevel.from_storage("機密"),
                ClassificationLevel.from_storage("無機密"),
            ]
        )
        assert latched == ClassificationLevel.from_storage("機密")

    def test_max_of_single_element(self):
        only = ClassificationLevel.from_storage("密")
        assert ClassificationLevel.max_of([only]) == only

    def test_max_of_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            ClassificationLevel.max_of([])


class TestLegacyBackfill:
    def test_legacy_false_maps_to_unclassified(self):
        # SYSTEM-MAP §8:classified=false → 無機密。
        assert (
            ClassificationLevel.from_legacy_classified(False).value == "無機密"
        )

    def test_legacy_true_maps_to_secret_top(self):
        # SYSTEM-MAP §8 / OE-3:classified=true → 機密(SECRET,最高級),
        # 保守 floor,不是最終分類。
        assert ClassificationLevel.from_legacy_classified(True).value == "機密"


class TestStorageRoundTrip:
    @pytest.mark.parametrize("value", ORDERED_VALUES)
    def test_round_trip_all_levels(self, value):
        level = ClassificationLevel.from_storage(value)
        assert level.to_storage() == value
        assert ClassificationLevel.from_storage(level.to_storage()) is level

    @pytest.mark.parametrize(
        "bad",
        [
            "極機密",  # removed five-level value
            "絕對機密",
            "top-secret",
            "",
            "無 機密",
            "unclassified",
        ],
    )
    def test_unknown_storage_value_rejected(self, bad):
        with pytest.raises(ValueError):
            ClassificationLevel.from_storage(bad)
