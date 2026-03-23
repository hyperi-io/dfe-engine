"""Tests for field map helpers — group_summaries."""

from dataclasses import dataclass

from dfe_engine.fieldmap.helpers import group_summaries

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


@dataclass
class _Summary:
    """Minimal summary satisfying _GroupableSummary protocol."""

    standard: str
    version: str | None = None


# ---------------------------------------------------------------
# group_summaries
# ---------------------------------------------------------------


def _key(g: dict) -> str:
    return g["key"]


def _items(g: dict) -> list:
    return g["items"]


class TestGroupSummaries:
    def test_empty_summaries_returns_empty_list(self):
        assert group_summaries([], "standard") == []
        assert group_summaries([], "version") == []

    def test_group_by_standard(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("ecs", "8.11"),
            _Summary("sigma", None),
        ]
        result = group_summaries(summaries, "standard")
        keys = [_key(g) for g in result]
        assert set(keys) == {"sigma", "ecs"}
        sigma = next(g for g in result if _key(g) == "sigma")
        ecs = next(g for g in result if _key(g) == "ecs")
        assert len(_items(sigma)) == 2
        assert len(_items(ecs)) == 1

    def test_group_by_version_none_maps_to_empty_string(self):
        summaries = [
            _Summary("sigma", None),
            _Summary("ecs", "8.11"),
        ]
        result = group_summaries(summaries, "version")
        keys = [_key(g) for g in result]
        assert "" in keys
        assert "8.11" in keys
        empty = next(g for g in result if _key(g) == "")
        v811 = next(g for g in result if _key(g) == "8.11")
        assert _items(empty) == [_Summary("sigma", None)]
        assert _items(v811) == [_Summary("ecs", "8.11")]

    def test_max_per_group_limits_items(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("sigma", "2.0"),
            _Summary("sigma", "3.0"),
        ]
        result = group_summaries(summaries, "standard", max_per_group=2)
        sigma = next(g for g in result if _key(g) == "sigma")
        assert len(_items(sigma)) == 2
        assert sigma["total"] == 3

    def test_max_per_group_minus_one_returns_all(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("sigma", "2.0"),
            _Summary("sigma", "3.0"),
        ]
        result = group_summaries(summaries, "standard", max_per_group=-1)
        sigma = next(g for g in result if _key(g) == "sigma")
        assert len(_items(sigma)) == 3

    def test_sort_order_asc_within_group(self):
        summaries = [
            _Summary("sigma", "3.0"),
            _Summary("sigma", "1.0"),
            _Summary("sigma", "2.0"),
        ]
        result = group_summaries(summaries, "standard", sort_order="asc", max_per_group=-1)
        sigma = next(g for g in result if _key(g) == "sigma")
        vers = [s.version for s in _items(sigma)]
        assert vers == ["1.0", "2.0", "3.0"]

    def test_sort_order_desc_within_group(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("sigma", "2.0"),
            _Summary("sigma", "3.0"),
        ]
        result = group_summaries(summaries, "standard", sort_order="desc", max_per_group=-1)
        sigma = next(g for g in result if _key(g) == "sigma")
        vers = [s.version for s in _items(sigma)]
        assert vers == ["3.0", "2.0", "1.0"]

    def test_sort_order_descend_alias(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("sigma", "2.0"),
        ]
        result = group_summaries(summaries, "standard", sort_order="descend", max_per_group=-1)
        sigma = next(g for g in result if _key(g) == "sigma")
        vers = [s.version for s in _items(sigma)]
        assert vers == ["2.0", "1.0"]

    def test_group_by_version_sorts_by_standard_within_group(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("ecs", "1.0"),
            _Summary("cim", "1.0"),
        ]
        result = group_summaries(summaries, "version", sort_order="asc", max_per_group=-1)
        v10 = next(g for g in result if _key(g) == "1.0")
        stds = [s.standard for s in _items(v10)]
        assert stds == ["cim", "ecs", "sigma"]

    def test_multiple_groups_preserves_all_keys(self):
        summaries = [
            _Summary("sigma", "1.0"),
            _Summary("ecs", "8.11"),
            _Summary("cim", "5.0"),
        ]
        result = group_summaries(summaries, "standard")
        keys = [_key(g) for g in result]
        assert set(keys) == {"sigma", "ecs", "cim"}
        sigma = next(g for g in result if _key(g) == "sigma")
        ecs = next(g for g in result if _key(g) == "ecs")
        cim = next(g for g in result if _key(g) == "cim")
        assert _items(sigma)[0].version == "1.0"
        assert _items(ecs)[0].version == "8.11"
        assert _items(cim)[0].version == "5.0"
