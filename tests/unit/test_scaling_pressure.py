#  Project:      dfe-engine
#  File:         tests/unit/test_scaling_pressure.py
#  Purpose:      The one rule deciding which gauge rows ARE scaling pressure
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""One matching rule, substituted into both query catalogues.

scalo registers the composite bare and an app may prefix it with its own metrics
namespace, so the same signal lands in ClickHouse as ``scaling_pressure``,
``dfe_scaling_pressure``, ``dfe_loader_scaling_pressure`` or
``dfe_fetcher_scaling_pressure``. The KEDA shim and the app-management reader both
have to match all four, and they have to agree - hence one module and a sentinel
rather than the predicate copied into two YAML files.
"""

from __future__ import annotations

import pytest

from dfe_engine import scaling_pressure


def test_the_canonical_wire_name_is_the_bare_gauge_scalo_registers():
    assert scaling_pressure.GAUGE == "scaling_pressure"


def test_the_rule_matches_the_bare_gauge_and_any_namespace_prefix():
    rule = scaling_pressure.match_sql()

    assert "MetricName = 'scaling_pressure'" in rule
    assert "endsWith(MetricName, '_scaling_pressure')" in rule


def test_the_rule_reads_whichever_column_it_is_given():
    assert scaling_pressure.match_sql("m") == (
        "(m = 'scaling_pressure' OR endsWith(m, '_scaling_pressure'))"
    )


def test_a_matched_row_is_reported_under_the_canonical_bare_name():
    name = scaling_pressure.name_sql()

    assert name.startswith("if(")
    assert name.endswith(", 'scaling_pressure', MetricName)")


@pytest.mark.parametrize(
    "sentinel", [scaling_pressure.MATCH_SENTINEL, scaling_pressure.NAME_SENTINEL]
)
def test_apply_leaves_no_sentinel_behind(sentinel):
    out = scaling_pressure.apply(f"SELECT {sentinel} FROM t")

    assert sentinel not in out


def test_apply_touches_nothing_else_in_the_statement():
    sql = "SELECT __PRESSURE_NAME__ FROM t WHERE __PRESSURE_MATCH__ AND S = {service:String}"

    out = scaling_pressure.apply(sql)

    assert out.startswith("SELECT if(")
    assert "AND S = {service:String}" in out


def test_sql_carrying_no_sentinel_is_returned_unchanged():
    assert scaling_pressure.apply("SELECT 1") == "SELECT 1"
