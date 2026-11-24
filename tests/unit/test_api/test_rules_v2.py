"""The v2 rule contract: a description, and either a CEL filter or a WHERE fragment."""

from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError
from scalo.api import ProblemError

from dfe_engine.api.deps import AuthContext
from dfe_engine.api.v1.rulesV2 import (
    RuleV2CreateRequest,
    RuleV2FromHyperdxRequest,
    RuleV2Response,
    RuleV2Summary,
    SqlValidationRequest,
    _refuse_detection,
    _rule_from_write,
    _rule_to_response,
    _with_source_table,
    validate_rule_sql,
)
from dfe_engine.hunts.rule_creation_service import RuleCreationService
from dfe_engine.hunts.rule_model import Rule
from dfe_engine.hunts.rule_registry import RuleRegistry
from dfe_engine.settings import DFESettings
from dfe_engine.yaml_utils import yaml_load


class _Hunts:
    lookup_databases: list[str] = []


class _Settings:
    hunts = _Hunts()


def _settings() -> DFESettings:
    """A stand-in carrying only the hunt fields these paths read."""
    return cast(DFESettings, _Settings())


def _nobody() -> AuthContext:
    """These paths never read the caller."""
    return cast(AuthContext, None)


def test_validate_fills_the_source_placeholder_with_the_landing_table():
    sql = "SELECT * FROM {{source}} WHERE severity = 'high'"
    deployment = cast(
        DFESettings,
        SimpleNamespace(clickhouse=SimpleNamespace(effective_data_database="tenant")),
    )
    assert (
        _with_source_table(sql, deployment)
        == "SELECT * FROM `tenant`.`main` WHERE severity = 'high'"
    )
    assert "{{source}}" in _with_source_table(
        "SELECT * FROM dfe.main WHERE msg = '{{source}}'", _settings()
    )

    result = validate_rule_sql(SqlValidationRequest(sql=sql), user=_nobody(), settings=_settings())

    assert result.valid is True
    assert result.errors == []


def test_a_source_placeholder_is_not_planned_against_the_landing_table(monkeypatch):
    """A column the landing table lacks is the hunt's source's column, not invalid SQL."""

    class _ClickHouse:
        def command(self, sql, **_kwargs):
            if sql.startswith("EXPLAIN PLAN"):
                raise RuntimeError(
                    "Code: 47. DB::Exception: Unknown expression or function identifier `test` "
                    "in scope SELECT 1 FROM dfe.main WHERE test = 'test'."
                )

        def query(self, _sql, **_kwargs):
            return type("Result", (), {"result_rows": [(0, 0)]})()

    monkeypatch.setattr(RuleCreationService, "_query_client", lambda _self: _ClickHouse())
    sql = "SELECT * FROM {{source}} WHERE test = 'test'"

    checked = validate_rule_sql(SqlValidationRequest(sql=sql), user=_nobody(), settings=_settings())
    rule, errors = _rule_from_write(
        _settings(),
        RuleV2CreateRequest(name="placeholder", user_sql=sql),
        rule_id="placeholder",
        display_name="Placeholder",
        description=None,
    )

    assert checked.valid is True
    assert errors == []
    assert rule.where_clause == "test = 'test'"

    named = validate_rule_sql(
        SqlValidationRequest(sql="SELECT * FROM dfe.main WHERE test = 'test'"),
        user=_nobody(),
        settings=_settings(),
    )
    assert named.valid is False
    assert any("cannot plan" in error.message for error in named.errors)


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
        _settings(),
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
        _settings(),
        body,
        rule_id="high_value",
        display_name="High Value",
        description=None,
    )
    assert errors == []
    assert rule.cel_filter == 'severity == "critical"'
    assert rule.source_type is None
    assert _rule_to_response(rule).source_type is None
    assert rule.where_clause
    assert "SELECT" not in rule.where_clause.upper()
    assert rule.original_sql == ""
    assert rule.hunt_name is None
    assert rule.source is None


def test_create_requires_exactly_one_detection():
    with pytest.raises(ValidationError, match="one of them"):
        RuleV2CreateRequest(name="bare")
    with pytest.raises(ValidationError, match="one of them"):
        RuleV2CreateRequest(
            name="both",
            where_clause="severity = 'high'",
            cel_filter='severity == "critical"',
        )
    with pytest.raises(ValidationError, match="one of them"):
        RuleV2CreateRequest(
            name="sql_and_match",
            user_sql="SELECT * FROM dfe.main WHERE severity = 'high'",
            match={"field": "severity", "value": "high"},
        )


def test_create_refuses_a_full_query_and_the_removed_fields():
    with pytest.raises(ValidationError, match="not a full SQL statement"):
        RuleV2CreateRequest(
            name="raw",
            where_clause="SELECT * FROM dfe.main WHERE severity = 'high'",
        )
    with pytest.raises(ValidationError):
        RuleV2CreateRequest(name="sourced", where_clause="severity = 'high'", source="windows")  # ty: ignore[unknown-argument]  # the removed field is the point of the test
    with pytest.raises(ValidationError):
        RuleV2CreateRequest(name="hunted", where_clause="severity = 'high'", hunt_name="windows")  # ty: ignore[unknown-argument]  # the removed field is the point of the test


def test_create_accepts_the_source_placeholder_validate_accepts():
    sql = "SELECT *\nFROM {{source}} WHERE severity = 'high'"
    body = RuleV2CreateRequest(name="high", user_sql=sql)
    checked = validate_rule_sql(SqlValidationRequest(sql=sql), user=_nobody(), settings=_settings())
    rule, errors = _rule_from_write(
        _settings(), body, rule_id="high", display_name="High", description=None
    )

    assert checked.valid is True
    assert errors == []
    assert rule.where_clause == "severity = 'high'"
    assert rule.source_table is None
    assert rule.original_sql == sql
    assert _rule_to_response(rule).original_sql == sql


def test_user_sql_keeps_the_where_and_drops_the_table():
    body = RuleV2CreateRequest(
        name="high",
        user_sql="SELECT * FROM dfe.main WHERE severity = 'high'",
    )
    assert body.source_type == "raw"
    rule, errors = _rule_from_write(
        _settings(), body, rule_id="high", display_name="High", description=None
    )
    assert errors == []
    assert rule.where_clause == "severity = 'high'"
    assert rule.source is None
    assert rule.source_db is None
    assert rule.source_table is None
    assert rule.original_sql == "SELECT * FROM dfe.main WHERE severity = 'high'"
    assert rule.condition is None
    response = _rule_to_response(rule)
    assert response.match is None
    assert response.source_type == "raw"
    assert response.original_sql == rule.original_sql


def test_hyperdx_user_sql_is_sanitized_to_the_where():
    rendered = (
        "SELECT _timestamp,_json FROM dfe.main "
        "WHERE (toString(_json.event_type) = 'login_failure') "
        "ORDER BY _timestamp DESC LIMIT 100"
    )
    body = RuleV2CreateRequest(name="search", source_type="hyperdx", user_sql=rendered)
    rule, errors = _rule_from_write(
        _settings(), body, rule_id="search", display_name="Search", description="From Search"
    )
    assert errors == []
    assert "login_failure" in rule.where_clause
    assert "ORDER" not in rule.where_clause.upper()
    assert "LIMIT" not in rule.where_clause.upper()
    assert rule.source_table is None
    assert rule.description == "From Search"
    assert rule.original_sql == rendered
    detail = _rule_to_response(rule)
    assert detail.source_type == "hyperdx"
    assert detail.original_sql == rendered


def test_hyperdx_source_type_applies_only_to_user_sql():
    with pytest.raises(ValidationError, match="user_sql"):
        RuleV2CreateRequest(name="cel", source_type="hyperdx", cel_filter='severity == "high"')


def test_hyperdx_create_accepts_user_sql_or_raw_sql():
    by_user_sql = RuleV2FromHyperdxRequest(
        user_sql="SELECT * FROM dfe.main WHERE severity = 'high'"
    )
    by_raw_sql = RuleV2FromHyperdxRequest(raw_sql="SELECT * FROM dfe.main WHERE severity = 'high'")
    assert by_user_sql.sql == by_raw_sql.sql
    with pytest.raises(ValidationError, match="raw_sql' or 'user_sql"):
        RuleV2FromHyperdxRequest()
    with pytest.raises(ValidationError, match="raw_sql' or 'user_sql"):
        RuleV2FromHyperdxRequest(
            user_sql="SELECT * FROM dfe.main WHERE x = 1",
            raw_sql="SELECT * FROM dfe.main WHERE y = 1",
        )


def test_match_compiles_like_a_source_match():
    body = RuleV2CreateRequest(
        name="certutil",
        description="Certutil abuse",
        match={
            "all": [
                {"field": "process_name", "value": "certutil.exe"},
                {"field": "event.category", "operator": "exists"},
            ]
        },
    )
    rule, errors = _rule_from_write(
        _settings(),
        body,
        rule_id="certutil",
        display_name="Certutil",
        description=body.description,
    )
    assert errors == []
    assert rule.condition is not None
    assert rule.condition.all[0].field == "process_name"
    assert rule.condition.all[0].operator == "equals"
    assert "_json.`process_name`.:String = 'certutil.exe'" in rule.where_clause
    assert "event`.`category" in rule.where_clause
    assert rule.source is None
    assert rule.original_sql == ""
    response = _rule_to_response(rule)
    assert response.match == rule.condition
    assert response.source_type is None
    assert response.description == "Certutil abuse"


def test_a_tautology_is_refused_before_save():
    body = RuleV2CreateRequest(name="all", where_clause="1 = 1")
    rule, errors = _rule_from_write(
        _settings(), body, rule_id="all", display_name="All", description=None
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
        assert loaded.source_type is None
        assert _rule_to_response(loaded).source_type is None
        reg.save(
            Rule(
                rule_id="from_search",
                name="From Search",
                severity="medium",
                where_clause="severity = 'high'",
                source_type="hyperdx",
            )
        )
        detail = _rule_to_response(reg.get("from_search"))
        assert detail.source_type == "hyperdx"
        assert detail.original_sql == ""
        reg.save(
            Rule(
                rule_id="from_sql",
                name="From SQL",
                severity="high",
                where_clause="`aws-guardduty`.severity >= 7",
                source_type="raw",
                original_sql="SELECT * FROM dfe.`aws-guardduty` WHERE `aws-guardduty`.severity >= 7",
            )
        )
        sql_detail = _rule_to_response(reg.get("from_sql"))
        assert sql_detail.original_sql.startswith("SELECT * FROM dfe.`aws-guardduty`")
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
    assert "source_db" not in RuleV2Response.model_fields
