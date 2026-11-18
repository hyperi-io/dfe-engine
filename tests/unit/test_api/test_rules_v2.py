"""The v2 rule contract: a description, and either a CEL filter or a WHERE fragment."""

import pytest
from pydantic import ValidationError
from scalo.api import ProblemError

from dfe_engine.api.v1.rulesV2 import (
    RuleV2CreateRequest,
    RuleV2Response,
    RuleV2Summary,
    _refuse_detection,
    _rule_from_write,
)
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.yaml_utils import yaml_load


class _Hunts:
    lookup_databases: list[str] = []


class _Settings:
    hunts = _Hunts()


def test_create_accepts_a_where_fragment_and_an_optional_description():
    body = RuleV2CreateRequest(
        name="certutil",
        display_name="Certutil",
        description="  Certutil abuse  ",
        where_clause="  WHERE process_name = 'certutil.exe'  ",
    )
    assert body.description == "Certutil abuse"
    assert body.where_clause == "WHERE process_name = 'certutil.exe'"
    assert body.cel_filter is None

    rule, errors = _rule_from_write(
        _Settings(),
        body,
        rule_id="certutil",
        display_name="Certutil",
        description=body.description,
    )
    assert errors == []
    assert rule.where_clause == "process_name = 'certutil.exe'"
    assert rule.cel_filter is None
    assert rule.description == "Certutil abuse"
    assert rule.source is None
    assert rule.source_db is None
    assert rule.source_table is None
    assert rule.hunt_name is None
    assert rule.original_sql == ""


def test_create_accepts_a_cel_filter_without_sql():
    body = RuleV2CreateRequest(name="high_value", cel_filter='severity == "critical"')
    rule, errors = _rule_from_write(
        _Settings(),
        body,
        rule_id="high_value",
        display_name="High Value",
        description=None,
    )
    assert errors == []
    assert rule.cel_filter == 'severity == "critical"'
    assert rule.where_clause
    assert "SELECT" not in rule.where_clause.upper()
    assert rule.original_sql == ""
    assert rule.hunt_name is None
    assert rule.source is None


def test_create_requires_exactly_one_detection():
    with pytest.raises(ValidationError, match="where_clause' or 'cel_filter"):
        RuleV2CreateRequest(name="bare")
    with pytest.raises(ValidationError, match="where_clause' or 'cel_filter"):
        RuleV2CreateRequest(
            name="both",
            where_clause="severity = 'high'",
            cel_filter='severity == "critical"',
        )


def test_create_refuses_a_full_query_and_the_removed_fields():
    with pytest.raises(ValidationError, match="not a full SQL statement"):
        RuleV2CreateRequest(
            name="raw",
            where_clause="SELECT * FROM dfe.main WHERE severity = 'high'",
        )
    with pytest.raises(ValidationError):
        RuleV2CreateRequest(name="sourced", where_clause="severity = 'high'", source="windows")
    with pytest.raises(ValidationError):
        RuleV2CreateRequest(name="hunted", where_clause="severity = 'high'", hunt_name="windows")


def test_a_tautology_is_refused_before_save():
    body = RuleV2CreateRequest(name="all", where_clause="1 = 1")
    rule, errors = _rule_from_write(
        _Settings(), body, rule_id="all", display_name="All", description=None
    )
    assert errors == []
    with pytest.raises(ProblemError, match="matches every event"):
        _refuse_detection(rule, errors)


def test_description_round_trips_and_the_list_has_no_source_or_hunt(tmp_path):
    reg = RuleRegistry(rules_directory=tmp_path, writable=True, refresh_interval=0)
    try:
        rule = Rule(
            rule_id="described",
            name="Described",
            description="Looks for certutil",
            severity="high",
            where_clause="process_name = 'certutil.exe'",
        )
        reg.save(rule)
        stored = yaml_load(tmp_path / "described.yaml")
        assert stored["description"] == "Looks for certutil"
        assert stored.get("hunt_name") is None
        loaded = reg.get("described")
        assert loaded.description == "Looks for certutil"
        listed = reg.list_rules()
        assert listed[0]["description"] == "Looks for certutil"
    finally:
        reg.close()

    summary = RuleV2Summary.model_validate(
        {
            "name": "described",
            "display_name": "Described",
            "description": "",
            "severity": "high",
            "source": "windows",
            "hunt_name": "windows",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    assert summary.description is None
    assert "source" not in RuleV2Summary.model_fields
    assert "hunt_name" not in RuleV2Summary.model_fields
    assert "original_sql" not in RuleV2Response.model_fields
    assert "source_db" not in RuleV2Response.model_fields
