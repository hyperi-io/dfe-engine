"""Tests for rule import: a SELECT read into a registered source and a WHERE clause."""

import pytest

from dfe_engine.clickhouse.statements import CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE
from dfe_engine.hunts import rule_import
from dfe_engine.hunts.rule_import import (
    ImportRefusedError,
    find_placeholders,
    import_select,
)
from dfe_engine.hunts.rule_json_paths import (
    referenced_json_paths,
    seen_paths_sql,
    unseen_paths,
    unseen_warning,
)

REGISTERED = frozenset({"windows-audit", "events"})


def _read(sql: str):
    return import_select(sql, is_source=REGISTERED.__contains__, data_database="dfe")


def _kinds(imported) -> list[str]:
    return [part.kind for part in imported.dropped]


class TestImportedFields:
    """The source and WHERE a SELECT becomes, and what is listed as dropped."""

    @pytest.mark.parametrize(
        ("sql", "source", "where", "dropped_kinds"),
        [
            pytest.param(
                "SELECT * FROM dfe.`windows-audit` WHERE _json.a = 'x'",
                "windows-audit",
                "_json.a = 'x'",
                [],
                id="qualified source",
            ),
            pytest.param(
                "SELECT * FROM events WHERE y = 2",
                "events",
                "y = 2",
                [],
                id="bare source",
            ),
            pytest.param(
                "SELECT a, b FROM events WHERE y = 2 ORDER BY a DESC LIMIT 5 OFFSET 2",
                "events",
                "y = 2",
                ["columns", "order_by", "limit", "offset"],
                id="column list, order and limit",
            ),
            pytest.param(
                "SELECT * FROM events WHERE y = 2 SETTINGS max_threads = 2",
                "events",
                "y = 2",
                ["settings"],
                id="settings",
            ),
            pytest.param(
                "SELECT DISTINCT a FROM events WHERE y = 2",
                "events",
                "y = 2",
                ["columns", "distinct"],
                id="distinct",
            ),
            pytest.param(
                "SELECT * FROM events WHERE _timestamp >= now() - INTERVAL 1 HOUR AND y = 2",
                "events",
                "y = 2",
                ["time_bound"],
                id="time bound on a timestamp column",
            ),
            pytest.param(
                "SELECT * FROM events WHERE {timestamp_condition} AND y = 2",
                "events",
                "y = 2",
                ["time_placeholder"],
                id="time placeholder",
            ),
            pytest.param(
                "SELECT _timestamp,_json FROM {{org_id}}.{{source_table_name}} "
                "WHERE ({{timestamp_condition}}) AND z = 3 ORDER BY _timestamp DESC",
                None,
                "z = 3",
                ["columns", "order_by", "time_placeholder"],
                id="legacy template always needs a pick",
            ),
            pytest.param(
                "SELECT * FROM other.events WHERE y = 2",
                None,
                "y = 2",
                [],
                id="table outside the data database",
            ),
            pytest.param(
                "SELECT * FROM stranger WHERE y = 2",
                None,
                "y = 2",
                [],
                id="table that is no registered source",
            ),
        ],
    )
    def test_reads_the_fields(self, sql, source, where, dropped_kinds):
        imported = _read(sql)
        assert imported.source == source
        assert imported.where_clause == where
        assert sorted(_kinds(imported)) == sorted(dropped_kinds)

    def test_lists_each_dropped_piece_as_written(self):
        imported = _read(
            "SELECT a FROM events WHERE _timestamp >= now() - INTERVAL 1 HOUR AND y = 2 "
            "ORDER BY _timestamp DESC LIMIT 5"
        )
        assert {part.kind: part.text for part in imported.dropped} == {
            "columns": "a",
            "order_by": "ORDER BY _timestamp DESC",
            "limit": "LIMIT 5",
            "time_bound": "_timestamp >= now() - INTERVAL 1 HOUR",
        }

    def test_keeps_the_predicates_as_written(self):
        where = "toString(_json.`user`.Name) IN ('A', 'b') AND NOT match(host, '^web[0-9]+$')"

        assert _read(f"SELECT * FROM events WHERE {where}").where_clause == where

    @pytest.mark.parametrize(
        ("sql", "fragment"),
        [
            pytest.param("SELECT * FROM events", "No filter is left", id="no where"),
            pytest.param(
                "SELECT * FROM events WHERE {timestamp_condition}",
                "No filter is left",
                id="only the placeholder",
            ),
            pytest.param(
                "SELECT * FROM events WHERE b = {org:String}",
                "{org}",
                id="parameter placeholder left in the where",
            ),
            pytest.param(
                "SELECT * FROM {{org_id}}.{{source_table_name}} WHERE a = 1",
                "pick the source",
                id="template from",
            ),
            pytest.param(
                "SELECT * FROM other.events WHERE a = 1", "pick the source", id="other database"
            ),
            pytest.param(
                "SELECT * FROM stranger WHERE a = 1", "not a registered source", id="unknown table"
            ),
            pytest.param(
                "SELECT * FROM events WHERE a = 1 OR _timestamp > now() - INTERVAL 1 HOUR",
                "time bound sits under OR",
                id="bound under or",
            ),
        ],
    )
    def test_warns_without_refusing(self, sql, fragment):
        imported = _read(sql)
        assert any(fragment in warning for warning in imported.warnings)


class TestRefusedShapes:
    """A shape a single-source row filter cannot express is refused, with the reason."""

    @pytest.mark.parametrize(
        ("sql", "reason"),
        [
            pytest.param("SELECT count() FROM t GROUP BY a", "GROUP BY", id="group by"),
            pytest.param(
                "SELECT count() FROM t GROUP BY a HAVING count() > 3", "GROUP BY", id="having"
            ),
            pytest.param("SELECT * FROM a JOIN b ON a.x = b.x", "joins", id="join"),
            pytest.param("SELECT * FROM a, b WHERE a.x = b.x", "joins", id="comma join"),
            pytest.param("SELECT * FROM a UNION ALL SELECT * FROM b", "unions", id="union"),
            pytest.param("SELECT * FROM a INTERSECT SELECT * FROM b", "intersects", id="intersect"),
            pytest.param("SELECT * FROM a EXCEPT SELECT * FROM b", "subtracts", id="except"),
            pytest.param("WITH x AS (SELECT 1) SELECT * FROM x", "WITH", id="with"),
            pytest.param(
                "SELECT * FROM (SELECT * FROM t) WHERE x = 1", "subquery", id="subquery in from"
            ),
            pytest.param("SELECT * FROM url('http://x') WHERE x = 1", "not a table", id="function"),
            pytest.param("SELECT 1", "no FROM", id="no from"),
            pytest.param("SELECT * FROM a; SELECT * FROM b", "one SELECT", id="two statements"),
            pytest.param("INSERT INTO t VALUES (1)", "not a SELECT", id="insert"),
            pytest.param("garbage ((", "does not parse", id="garbage"),
            pytest.param("   ", "empty", id="blank"),
        ],
    )
    def test_refused_with_a_reason(self, sql, reason):
        with pytest.raises(ImportRefusedError, match=reason):
            _read(sql)


_LONG = CLICKHOUSE_DEFAULT_MAX_QUERY_SIZE
_PATTERN_RUNS = {
    "template from": rule_import._TEMPLATE_FROM.search,
    "double brace time": rule_import._DOUBLE_BRACE_TIME.search,
    "bracketed time conjunct": rule_import._BRACKETED_TIME_CONJUNCT.search,
    "no filter": rule_import._NO_FILTER.fullmatch,
}
_HOSTILE_TEXT = {
    "whitespace": " \t\n" * (_LONG // 3),
    "split whitespace": ("x" + " " * 64) * (_LONG // 65),
    "unclosed brace": "{{" + " " * _LONG,
    "repeated brace": "{{ " * (_LONG // 3),
    "repeated conjunct": "( {timestamp_condition} ) AND" * (_LONG // 30),
    "unclosed paren": "(" * (_LONG // 2) + " " * (_LONG // 2),
}


class TestPatternsOnHostileText:
    """Near-miss text up to the request cap matches no pattern."""

    @pytest.mark.parametrize("text", _HOSTILE_TEXT.values(), ids=_HOSTILE_TEXT.keys())
    @pytest.mark.parametrize("run", _PATTERN_RUNS.values(), ids=_PATTERN_RUNS.keys())
    def test_does_not_match(self, run, text):
        assert run(text) is None


class TestPlaceholders:
    """``{name}`` placeholders are found outside string literals only."""

    @pytest.mark.parametrize(
        ("where", "found"),
        [
            ("a = {org_id}", ["{org_id}"]),
            ("a = {w:DateTime} AND b = {x}", ["{w}", "{x}"]),
            ("a = {{org_id}}", ["{org_id}"]),
            ("m = {'k': 1}", []),
            ("a = '{inside_a_literal}'", []),
            ("a = `{x}`", []),
            ("a = 1", []),
            ("a = ((", []),
        ],
    )
    def test_finds(self, where, found):
        assert find_placeholders(where) == found


class TestJsonPaths:
    """The ``_json`` paths a WHERE reads, and the ones recent rows lack."""

    @pytest.mark.parametrize(
        ("where", "paths"),
        [
            ("toString(_json.a.b) = 'x' AND _json.a.b.c.d > 3", ["a.b", "a.b.c.d"]),
            ("_json.`odd key`.`b.c` = 'x' OR other = 1", ["odd key.b.c"]),
            ("has(_json.tags.list, 'x')", ["tags.list"]),
            ("_json.a = 1 AND _json.a = 2", ["a"]),
            ("x = 1 AND y.z = 2", []),
            ("((", []),
        ],
    )
    def test_referenced(self, where, paths):
        assert referenced_json_paths(where) == paths

    @pytest.mark.parametrize(
        ("referenced", "seen", "missing"),
        [
            (["a.b"], {"a.b"}, []),
            (["a"], {"a.b.c"}, []),
            (["a.b"], {"a"}, ["a.b"]),
            (["x.y", "z"], {"x.q"}, ["x.y", "z"]),
            (["a"], set(), ["a"]),
            ([], {"a"}, []),
        ],
    )
    def test_unseen(self, referenced, seen, missing):
        assert unseen_paths(referenced, seen) == missing

    def test_the_read_is_bounded_by_rows_time_and_org(self):
        sql = seen_paths_sql("dfe", "main", "_timestamp_load", 60, tenant_ids=["t1"])
        assert "LIMIT 10000" in sql
        assert "INTERVAL 60 MINUTE" in sql
        assert "_org_id IN ('t1')" in sql
        assert "distinctJSONPaths" in sql

    def test_the_warning_names_paths_and_counts_the_rest(self):
        paths = [f"p{n}" for n in range(8)]
        warning = unseen_warning(paths, "dfe.main", 60)
        assert "_json.p0" in warning
        assert "_json.p5" not in warning
        assert "and 3 more" in warning
        assert "last 60 minutes of dfe.main" in warning
