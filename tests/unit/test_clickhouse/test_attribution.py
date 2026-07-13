#  Project:      dfe-engine
#  File:         tests/unit/test_clickhouse/test_attribution.py
#  Purpose:      Query attribution - context-var scoping + log_comment payload
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""Attribution tags: context-var scoping + log_comment serialisation."""

from __future__ import annotations

import json

import pytest

from dfe_engine.clickhouse.attribution import (
    DfeQueryTags,
    current_tags,
    merge_log_comment,
    tags_context,
)


def test_base_tags_are_service_only():
    assert current_tags() == DfeQueryTags()
    assert json.loads(current_tags().to_json()) == {"service": "dfe-engine"}


def test_tags_context_scopes_and_restores():
    before = current_tags()
    with tags_context(tenant_id="acme", feature="hunts", kind="read"):
        tags = current_tags()
        assert tags.tenant_id == "acme"
        assert tags.feature == "hunts"
        payload = json.loads(tags.to_json())
        assert payload["tenant_id"] == "acme"
        assert payload["service"] == "dfe-engine"
    assert current_tags() == before  # restored on exit


def test_tags_context_nests_and_inherits():
    with tags_context(tenant_id="a", feature="f1"):
        with tags_context(kind="ddl"):
            tags = current_tags()
            assert tags.tenant_id == "a"  # inherited from the outer scope
            assert tags.feature == "f1"
            assert tags.kind == "ddl"
        assert current_tags().kind is None  # inner scope restored


def test_tags_context_restores_on_exception():
    before = current_tags()
    with pytest.raises(ValueError, match="boom"):
        with tags_context(user="bob"):
            raise ValueError("boom")
    assert current_tags() == before


def test_merge_log_comment_keeps_caller_settings():
    with tags_context(tenant_id="acme"):
        merged = merge_log_comment({"max_execution_time": 25})
        assert merged["max_execution_time"] == 25
        assert json.loads(merged["log_comment"])["tenant_id"] == "acme"


def test_merge_log_comment_preserves_explicit_log_comment():
    with tags_context(tenant_id="acme"):
        merged = merge_log_comment({"log_comment": "custom"})
        assert merged["log_comment"] == "custom"  # a caller-set log_comment wins


def test_merge_log_comment_returns_fresh_dict():
    base = {"max_execution_time": 25}
    merged = merge_log_comment(base)
    assert "log_comment" not in base  # caller's dict is never mutated
    assert "log_comment" in merged


def test_to_json_omits_none_and_sorts():
    tags = DfeQueryTags(tenant_id="acme", user="bob")
    payload = tags.to_json()
    assert "null" not in payload  # None fields omitted
    assert json.loads(payload) == {"service": "dfe-engine", "tenant_id": "acme", "user": "bob"}
