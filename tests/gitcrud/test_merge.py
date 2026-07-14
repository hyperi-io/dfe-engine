#  Project:      dfe-engine
#  File:         tests/gitcrud/test_merge.py
#  Purpose:      Structured 3-way merge (drift reconciliation) - all cases
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""three_way_merge: per-field structured merge for clone-amend-then-pull-upstream."""

from __future__ import annotations

from dfe_engine.gitcrud.merge import three_way_merge


def test_no_changes() -> None:
    r = three_way_merge({"a": 1}, {"a": 1}, {"a": 1})
    assert r.clean and r.merged == {"a": 1}


def test_ours_only_change_kept() -> None:
    r = three_way_merge({"a": 1}, {"a": 2}, {"a": 1})
    assert r.clean and r.merged == {"a": 2}


def test_theirs_only_change_applied() -> None:
    r = three_way_merge({"a": 1}, {"a": 1}, {"a": 9})
    assert r.clean and r.merged == {"a": 9}


def test_both_same_change_no_conflict() -> None:
    r = three_way_merge({"a": 1}, {"a": 5}, {"a": 5})
    assert r.clean and r.merged == {"a": 5}


def test_divergent_scalar_change_conflicts_keeping_ours() -> None:
    r = three_way_merge({"a": 1}, {"a": 2}, {"a": 3})
    assert not r.clean
    assert r.conflicts == ["a"]
    assert r.merged == {"a": 2}  # ours kept


def test_ours_added_key_kept() -> None:
    r = three_way_merge({}, {"new": 1}, {})
    assert r.clean and r.merged == {"new": 1}


def test_theirs_added_key_applied() -> None:
    r = three_way_merge({}, {}, {"up": 1})
    assert r.clean and r.merged == {"up": 1}


def test_both_added_different_value_conflicts() -> None:
    r = three_way_merge({}, {"k": 1}, {"k": 2})
    assert r.conflicts == ["k"]
    assert r.merged == {"k": 1}


def test_theirs_deleted_unchanged_key_is_removed() -> None:
    r = three_way_merge({"a": 1, "b": 2}, {"a": 1, "b": 2}, {"a": 1})
    assert r.clean and r.merged == {"a": 1}  # b removed


def test_we_deleted_unchanged_key_is_removed() -> None:
    r = three_way_merge({"a": 1, "b": 2}, {"a": 1}, {"a": 1, "b": 2})
    assert r.clean and r.merged == {"a": 1}


def test_nested_dict_merges_per_field() -> None:
    base = {"cfg": {"x": 1, "y": 2}}
    ours = {"cfg": {"x": 1, "y": 20}}  # we changed y
    theirs = {"cfg": {"x": 10, "y": 2}}  # upstream changed x
    r = three_way_merge(base, ours, theirs)
    assert r.clean
    assert r.merged == {"cfg": {"x": 10, "y": 20}}  # both non-conflicting changes


def test_list_divergent_change_conflicts() -> None:
    r = three_way_merge({"t": ["a"]}, {"t": ["a", "b"]}, {"t": ["a", "c"]})
    assert r.conflicts == ["t"]
    assert r.merged == {"t": ["a", "b"]}  # ours kept; lists are atomic


def test_drift_scenario_customer_amend_plus_new_upstream() -> None:
    """base = hyperi v1; ours = customer's amended clone; theirs = hyperi v2."""
    base = {
        "match": {"field": "_json.event.module", "value": "aws"},
        "schema": {"columns": ["a", "b"]},
        "transform": {"engine": "vector"},
    }
    ours = {  # customer added a label + changed the transform engine
        "match": {"field": "_json.event.module", "value": "aws"},
        "schema": {"columns": ["a", "b"]},
        "transform": {"engine": "vrl"},
        "labels": {"team": "soc"},
    }
    theirs = {  # hyperi v2 refined the schema + match, and ALSO changed the transform
        "match": {"field": "_json.event.dataset", "value": "aws"},
        "schema": {"columns": ["a", "b", "c"]},
        "transform": {"engine": "wasm"},
    }
    r = three_way_merge(base, ours, theirs)
    # non-conflicting: upstream's match.field + new schema flow in; customer's label kept
    assert r.merged["match"]["field"] == "_json.event.dataset"
    assert r.merged["schema"]["columns"] == ["a", "b", "c"]
    assert r.merged["labels"] == {"team": "soc"}
    # transform.engine: both changed from base differently -> conflict, ours kept
    assert "transform.engine" in r.conflicts
    assert r.merged["transform"]["engine"] == "vrl"
