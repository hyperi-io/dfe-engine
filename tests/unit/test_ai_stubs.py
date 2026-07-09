#  Project:      dfe-engine
#  File:         tests/unit/test_ai_stubs.py
#  Purpose:      Default AI stub modules - the four authoring touch-points
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The stub AI modules round-trip and are clearly marked as stubs."""

from __future__ import annotations

import pytest

from dfe_engine.ai import (
    AIModuleStatus,
    AIModuleType,
    LogParser,
    QueryGenerator,
    QueryOptimiser,
    SchemaOptimiser,
    default_ai_registry,
)


def test_default_registry_has_the_four_touch_points():
    reg = default_ai_registry()
    types = {m.module_type for m in reg.list_modules()}
    assert types == {
        AIModuleType.QUERY_OPTIMISER,
        AIModuleType.QUERY_GENERATOR,
        AIModuleType.SCHEMA_OPTIMISER,
        AIModuleType.LOG_PARSER,
    }


def test_query_review_stub_round_trips():
    mod = default_ai_registry().list_modules(AIModuleType.QUERY_OPTIMISER)[0]
    res = mod.get_result(mod.submit(QueryOptimiser.Input(query="SELECT 1")))
    assert res.status == AIModuleStatus.COMPLETED
    assert res.output["stub"] is True
    assert res.output["proposed_query"] == "SELECT 1"


def test_query_create_stub_echoes_prompt():
    mod = default_ai_registry().list_modules(AIModuleType.QUERY_GENERATOR)[0]
    res = mod.get_result(mod.submit(QueryGenerator.Input(prompt="failed logins by ip")))
    assert res.output["stub"] is True
    assert "failed logins by ip" in res.output["rationale"]


def test_vrl_log_parser_stub_counts_samples():
    mod = default_ai_registry().list_modules(AIModuleType.LOG_PARSER)[0]
    res = mod.get_result(mod.submit(LogParser.Input(samples=["a", "b", "c"])))
    assert res.output["stub"] is True
    assert res.output["sample_count"] == 3


def test_schema_promotion_stub_returns_no_proposals():
    mod = default_ai_registry().list_modules(AIModuleType.SCHEMA_OPTIMISER)[0]
    res = mod.get_result(mod.submit(SchemaOptimiser.Input(source_name="aws/ct", source_schema={})))
    assert res.output["stub"] is True
    assert res.output["proposed_columns"] == []


def test_unknown_task_raises():
    mod = default_ai_registry().list_modules(AIModuleType.SCHEMA_OPTIMISER)[0]
    with pytest.raises(KeyError):
        mod.get_result("nope")
