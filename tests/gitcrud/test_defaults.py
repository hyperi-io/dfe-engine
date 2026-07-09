#  Project:      dfe-engine
#  File:         tests/gitcrud/test_defaults.py
#  Purpose:      Tests for chart-default diff (changed-from-default view)
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Chart-default diff."""

from __future__ import annotations

from dfe_engine.gitcrud.defaults import diff_against_defaults


def test_marks_changed_and_unchanged():
    current = {"replicaCount": 3, "config.kafka.brokers": "b:9092"}
    defaults = {"replicaCount": 1, "config": {"kafka": {"brokers": "b:9092"}}}
    rows = {r["path"]: r for r in diff_against_defaults(current, defaults)}
    assert rows["replicaCount"]["changed"] is True
    assert rows["replicaCount"]["default"] == 1
    assert rows["config.kafka.brokers"]["changed"] is False


def test_no_default_is_changed():
    rows = diff_against_defaults({"new.var": 5}, {})
    assert rows[0]["changed"] is True
    assert rows[0]["default"] is None
