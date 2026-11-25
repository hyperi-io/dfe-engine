"""A rule written as a source and a WHERE clause: the compile, the checks, and the stored shape."""

import types

import pytest

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_creation_service import (
    RuleCreateRequest,
    RuleCreationService,
)
from dfe_engine.hunts.rule_rewriter import SELECT_STAR_WARNING
from dfe_engine.hunts.rule_sources import resolved_source, unsourced_rules
from dfe_engine.settings import DetectionGuardSettings, HuntsSettings

_SOURCES = frozenset({"windows-audit", "events"})


def _service(**kwargs) -> RuleCreationService:
    kwargs.setdefault("hunts", HuntsSettings(detection_guard=DetectionGuardSettings(enabled=False)))
    return RuleCreationService(data_database="dfe", is_source=_SOURCES.__contains__, **kwargs)


def _create(service: RuleCreationService, **fields):
    request = RuleCreateRequest(name="Rule", source="windows-audit", **fields)
    return service.create_rule(request, "rule")


class TestTheStoredShape:
    def test_a_where_on_a_source_stores_the_sources_table_and_the_where(self):
        result = _create(_service(), where_clause="severity = 'high'")

        rule = result.rule
        assert result.sql_errors == []
        assert (rule.source, rule.source_db, rule.source_table) == (
            "windows-audit",
            "dfe",
            "windows-audit",
        )
        assert rule.where_clause == "severity = 'high'"
        assert rule.original_sql == "SELECT * FROM `dfe`.`windows-audit` WHERE severity = 'high'"
        assert rule.condition is None

    def test_a_cel_filter_alone_is_the_where(self):
        result = _create(_service(), cel_filter='severity == "high"')

        assert result.sql_errors == []
        assert "severity" in result.rule.where_clause
        assert result.rule.source_table == "windows-audit"

    def test_a_cel_filter_is_anded_into_the_where(self):
        result = _create(_service(), where_clause="a = 1", cel_filter="b > 2")

        assert result.rule.where_clause == "(a = 1) AND (b > 2)"

    @pytest.mark.parametrize(
        ("fields", "warned"),
        [
            ({"where_clause": "severity = 'high'"}, False),
            ({"cel_filter": 'severity == "high"'}, False),
            ({"user_sql": "SELECT * FROM dfe.`windows-audit` WHERE severity = 'high'"}, True),
        ],
        ids=["where", "cel-only", "written-select-star"],
    )
    def test_select_star_is_warned_only_when_the_author_wrote_it(self, fields, warned):
        rule = _create(_service(), **fields).rule

        assert (SELECT_STAR_WARNING in rule.warnings) is warned
        assert rule.had_select_star is warned

    def test_the_where_is_kept_as_written_bar_its_edges(self):
        result = _create(_service(), where_clause="  toString(_json.`user`.name) IN ('A', 'b')  ")

        assert result.rule.where_clause == "toString(_json.`user`.name) IN ('A', 'b')"


class TestWhatASaveRefuses:
    @pytest.mark.parametrize(
        ("fields", "fragment"),
        [
            pytest.param({"where_clause": ""}, "WHERE clause is empty", id="empty"),
            pytest.param({"where_clause": " \n "}, "WHERE clause is empty", id="blank"),
            pytest.param(
                {"where_clause": "a = 1 AND {timestamp_condition}"},
                "time window comes from the hunt schedule",
                id="time placeholder",
            ),
            pytest.param({"where_clause": "a = {window}"}, "{window}", id="window placeholder"),
            pytest.param(
                {"where_clause": "a = {org:String}"}, "{org}", id="typed parameter placeholder"
            ),
            pytest.param(
                {"where_clause": "_timestamp >= now() - INTERVAL 1 HOUR"},
                "hunt window bounds time",
                id="time bound",
            ),
            pytest.param(
                {"where_clause": "a = 1 ORDER BY a"}, "more than a row filter", id="past the filter"
            ),
            pytest.param(
                {"where_clause": "a = 1; DROP TABLE t"}, "more than a row filter", id="statement"
            ),
            pytest.param(
                {"where_clause": "url('http://x') = 1"},
                "may not call",
                id="call outside the row",
            ),
        ],
    )
    def test_a_where_that_cannot_be_a_rule_comes_back_as_an_error(self, fields, fragment):
        result = _create(_service(), **fields)

        assert any(fragment in error.message for error in result.sql_errors)

    @pytest.mark.parametrize(
        ("source", "fragment"),
        [
            pytest.param("no-such-source", "not a registered source", id="unregistered"),
            pytest.param("dfe.events", "a label, not a <db>.<table>", id="qualified"),
            pytest.param("a b", "not a table label", id="no label"),
        ],
    )
    def test_a_source_that_is_not_a_registered_label_comes_back_as_an_error(self, source, fragment):
        request = RuleCreateRequest(name="Rule", source=source, where_clause="a = 1")

        result = _service().create_rule(request, "rule")

        assert any(fragment in error.message for error in result.sql_errors)

    def test_a_source_given_with_no_registry_is_taken_as_named(self):
        service = RuleCreationService(data_database="dfe")

        result = service.create_rule(
            RuleCreateRequest(name="Rule", source="anything", where_clause="a = 1"), "rule"
        )

        assert result.sql_errors == []

    @pytest.mark.parametrize(
        "fields",
        [
            {"where_clause": "a = 1", "user_sql": "SELECT * FROM dfe.events WHERE a = 1"},
            {"where_clause": "a = 1", "condition": {"field": "a", "value": "x"}},
        ],
        ids=["where and sql", "where and condition"],
    )
    def test_two_ways_of_writing_it_are_refused(self, fields):
        with pytest.raises(ValueError, match="not both"):
            RuleCreateRequest(name="Rule", source="events", **fields)


class _Clickhouse:
    """Answers the plan, the column read and the JSON path read."""

    def __init__(self, *, paths=None, columns=None, plan_error=None) -> None:
        self.paths = paths if paths is not None else []
        self.columns = columns or {}
        self.plan_error = plan_error
        self.queries: list[tuple[str, dict]] = []

    def command(self, cmd: str, *_args, **_kwargs) -> str:
        if cmd.startswith("EXPLAIN PLAN") and self.plan_error is not None:
            raise self.plan_error
        return ""

    def query(self, sql: str, parameters=None, settings=None, **_kwargs):
        self.queries.append((sql, settings or {}))
        if "system.columns" in sql:
            named = parameters or {}
            names = self.columns.get((named["db"], named["tbl"]), [])
            return types.SimpleNamespace(result_rows=[(n, "String") for n in names])
        if "distinctJSONPaths" in sql:
            return types.SimpleNamespace(result_rows=[(self.paths,)])
        return types.SimpleNamespace(result_rows=[(10, 1)])


class TestUnseenJsonPaths:
    def test_a_path_recent_rows_never_carried_is_a_warning(self):
        client = _Clickhouse(paths=["user.name"])

        result = _create(_service(ch_client=client), where_clause="_json.user.nmae = 'x'")

        assert result.sql_errors == []
        assert len(result.json_path_warnings) == 1
        assert "_json.user.nmae did not appear" in result.json_path_warnings[0]

    @pytest.mark.parametrize(
        ("where", "paths"),
        [
            pytest.param("_json.user.name = 'x'", ["user.name"], id="path seen"),
            pytest.param("_json.user = 'x'", ["user.name"], id="object above a seen path"),
            pytest.param("severity = 'x'", ["user.name"], id="no json path read"),
            pytest.param("_json.user.nmae = 'x'", [], id="no recent rows"),
        ],
    )
    def test_nothing_is_warned_when_nothing_can_be_said(self, where, paths):
        result = _create(_service(ch_client=_Clickhouse(paths=paths)), where_clause=where)

        assert result.json_path_warnings == []

    def test_the_read_is_bounded_read_only_and_breaks_rather_than_fails(self):
        client = _Clickhouse(paths=["a"])

        _create(_service(ch_client=client), where_clause="_json.a = 'x'")

        [(sql, settings)] = [q for q in client.queries if "distinctJSONPaths" in q[0]]
        assert "LIMIT 10000" in sql
        assert settings["readonly"] == 1
        assert settings["read_overflow_mode"] == "break"
        assert settings["max_rows_to_read"] > 0

    def test_a_read_held_to_no_org_never_runs(self):
        client = _Clickhouse(paths=["a"])

        request = RuleCreateRequest(name="Rule", source="events", where_clause="_json.b = 'x'")
        result = _service(ch_client=client).create_rule(request, "rule", tenant_ids=[])

        assert result.json_path_warnings == []
        assert not [q for q in client.queries if "distinctJSONPaths" in q[0]]

    def test_a_rule_refused_for_its_sql_is_not_read(self):
        client = _Clickhouse(paths=["a"])

        _create(_service(ch_client=client), where_clause="_json.b = 'x' LIMIT 1")

        assert not [q for q in client.queries if "distinctJSONPaths" in q[0]]


class TestMeasureVolume:
    def test_off_it_never_counts_the_source(self):
        client = _Clickhouse()
        service = _service(
            ch_client=client,
            hunts=HuntsSettings(detection_guard=DetectionGuardSettings(enabled=True)),
        )

        result = service.create_rule(
            RuleCreateRequest(
                name="Rule", source="events", where_clause="a = 1", measure_volume=False
            ),
            "rule",
        )

        assert result.cost_estimate is None
        assert result.volume_warnings == []
        assert not [q for q in client.queries if "dfe_total" in q[0]]


class TestAStoredRulesQueryCheck:
    def test_a_rule_that_plans_has_no_errors(self):
        assert _service(ch_client=_Clickhouse()).check_stored("dfe", "events", "a = 1") == []

    def test_a_rule_clickhouse_cannot_plan_is_an_error(self):
        client = _Clickhouse(plan_error=Exception("Code: 47. UNKNOWN_IDENTIFIER nope"))

        [error] = _service(ch_client=client).check_stored("dfe", "events", "nope = 1")

        assert "cannot plan this rule against dfe.events" in error.message

    @pytest.mark.parametrize(
        ("where", "fragment"),
        [
            ("", "empty detection logic"),
            ("url('http://x') = 1", "may not call"),
            ("a IN (SELECT b FROM other.t)", "SELECT"),
        ],
    )
    def test_a_rule_the_save_would_refuse_is_an_error_without_clickhouse(self, where, fragment):
        [error] = RuleCreationService().check_stored("dfe", "events", where)

        assert fragment in error.message


class TestTheTargetCheck:
    def test_a_table_with_every_column_is_fine(self):
        client = _Clickhouse(columns={("dfe", "detection"): HuntResultSchema().insert_columns()})

        assert _service(ch_client=client).target_error("dfe", "detection") is None

    def test_the_missing_columns_are_named_in_insert_order(self):
        columns = HuntResultSchema().insert_columns()
        client = _Clickhouse(columns={("dfe", "detection"): columns[:-2]})

        error = _service(ch_client=client).target_error("dfe", "detection")

        assert (
            error == f"dfe.detection lacks the columns the runner writes: {', '.join(columns[-2:])}"
        )

    def test_an_absent_table_is_named(self):
        error = _service(ch_client=_Clickhouse()).target_error("dfe", "gone")

        assert error == "dfe.gone does not exist, so the runner has nowhere to write detections"

    def test_no_clickhouse_leaves_it_unchecked(self):
        assert RuleCreationService().target_error("dfe", "detection") is None


class TestRulesStoredBeforeSourcesWereRegistered:
    @pytest.mark.parametrize(
        ("source", "table", "expected"),
        [
            ("events", None, "events"),
            (None, "events", "events"),
            ("stale", "events", "events"),
            ("events", "windows-audit", "events"),
            ("stale", "stranger", None),
            (None, None, None),
            ("", "", None),
        ],
    )
    def test_the_registered_source_a_stored_rule_reads(self, source, table, expected):
        assert resolved_source(source, table, _SOURCES.__contains__) == expected

    def test_the_rules_naming_no_registered_source_are_listed_sorted(self):
        rows = [
            {"name": "b", "source": None, "source_table": "stranger"},
            {"name": "a", "source": "stale", "source_table": "nothing"},
            {"name": "ok", "source": None, "source_table": "events"},
        ]

        assert unsourced_rules(rows, _SOURCES.__contains__) == ["a", "b"]
