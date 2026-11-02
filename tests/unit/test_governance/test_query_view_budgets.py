#  Project:      dfe-engine
#  File:         tests/unit/test_governance/test_query_view_budgets.py
#  Purpose:      The query_views budgets reach the restricted reader's settings profile
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""query_views.max_rows_to_read, max_memory_usage and max_execution_time set the reader's profile.

The views run as dfe_query_reader, so its settings profile is the consumption
envelope an operator configures through these three keys.
"""

import pytest

from dfe_engine.governance.ch.bootstrap import service_roles_for
from dfe_engine.governance.ch.render import render_service_role
from dfe_engine.settings import DFESettings, QueryViewSettings


def _reader(settings: DFESettings):
    (reader,) = [r for r in service_roles_for(settings) if r.name == "query_reader"]
    return reader


@pytest.mark.parametrize(
    ("budgets", "rendered"),
    [
        pytest.param(
            {"max_rows_to_read": 123, "max_memory_usage": 456, "max_execution_time": 7},
            ["max_execution_time = 7", "max_memory_usage = 456", "max_rows_to_read = 123"],
            id="configured",
        ),
        pytest.param(
            {},
            [
                "max_execution_time = 30",
                "max_memory_usage = 2147483648",
                "max_rows_to_read = 10000000",
            ],
            id="defaults",
        ),
    ],
)
def test_the_reader_profile_carries_the_configured_budgets(budgets, rendered):
    settings = DFESettings(env="dev", query_views=QueryViewSettings(**budgets))

    ddl = "\n".join(render_service_role(_reader(settings)))

    for setting in rendered:
        assert setting in ddl


def test_only_the_reader_changes():
    settings = DFESettings(env="dev", query_views=QueryViewSettings(max_rows_to_read=1))
    from dfe_engine.governance.ch.models import DEFAULT_SERVICE_ROLES

    others = [r for r in service_roles_for(settings) if r.name != "query_reader"]
    assert others == [r for r in DEFAULT_SERVICE_ROLES if r.name != "query_reader"]


def test_a_view_request_cannot_ask_for_longer_than_the_reader_budget():
    from dfe_engine.query.executor import view_timeout_ceiling

    qv = QueryViewSettings(max_timeout=300, max_execution_time=30)
    assert view_timeout_ceiling(qv) == 30
