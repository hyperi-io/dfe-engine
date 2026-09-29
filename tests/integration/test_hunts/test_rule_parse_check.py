# Project:   DFE Engine
# File:      tests/integration/test_hunts/test_rule_parse_check.py
# Purpose:   The rule parse check gets its answer from ClickHouse's own parser
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""The rule parse check asks ClickHouse ``EXPLAIN AST`` and never falls back to sqlglot."""

import pytest

from dfe_engine.hunts import rule_creation_service
from dfe_engine.hunts.rule_creation_service import RuleCreationService

pytestmark = pytest.mark.integration


@pytest.fixture
def no_sqlglot(monkeypatch):
    """Fail the test if the parse check reaches the sqlglot fallback."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("the parse check fell back to sqlglot")

    monkeypatch.setattr(rule_creation_service.sqlglot, "parse", refuse)


@pytest.mark.parametrize("client_fixture", ["ch_client", "manager_client"])
def test_valid_sql_is_parsed_by_clickhouse(request, client_fixture, no_sqlglot):
    service = RuleCreationService(ch_client=request.getfixturevalue(client_fixture))

    errors = service.validate_sql(
        "SELECT * FROM dfe.main WHERE _json.process.name = 'certutil.exe' AND toUInt32(_json.port) > 1024"
    )

    assert errors == []


@pytest.mark.parametrize("client_fixture", ["ch_client", "manager_client"])
def test_syntax_error_comes_from_clickhouse(request, client_fixture, no_sqlglot):
    service = RuleCreationService(ch_client=request.getfixturevalue(client_fixture))

    sql = "SELECT * FROM dfe.main WHERE host = = 'a'"

    errors = service.validate_sql(sql)

    assert len(errors) == 1
    assert errors[0].message.startswith("ClickHouse could not parse this SQL: Syntax error")
    # The position is the rule's own, not shifted by the EXPLAIN AST prefix.
    assert errors[0].position == sql.index("= 'a'")
    assert f"failed at position {sql.index("= 'a'") + 1}" in errors[0].message
