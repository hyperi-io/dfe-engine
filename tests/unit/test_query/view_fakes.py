#  Project:      dfe-engine
#  File:         tests/unit/test_query/view_fakes.py
#  Purpose:      Stand-ins for the view catalogue and client, shared by the view tests
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A one-view catalogue and a recording client for tests that check the SQL a view request builds.

Real-ClickHouse behaviour is covered in tests/integration/test_query/test_view_keyset.py.
"""

from types import SimpleNamespace

from dfe_engine.query.models import ViewDefinition, ViewParameter

ORG_ONLY = [
    ViewParameter(name="org_id", clickhouse_type="String", python_type="string", reserved=True)
]


def view_definition(params: list[ViewParameter] | None = None) -> ViewDefinition:
    """A tenant-isolated view, ``analytics/events``, with the given parameters."""
    return ViewDefinition(
        name="dfe_v_analytics_events",
        label="analytics/events",
        database="testdb",
        parameters=ORG_ONLY if params is None else params,
        create_sql="CREATE VIEW dfe_v_analytics_events AS ...",
        namespace="analytics",
        short_name="events",
    )


class OneViewCatalog:
    """Serves one view definition for any label."""

    def __init__(self, view_def: ViewDefinition) -> None:
        self._view_def = view_def

    def get_view(self, label: str) -> ViewDefinition:
        return self._view_def


class RecordingClient:
    """Stands in for the restricted clickhouse-connect client and keeps what it was sent."""

    def __init__(self, error: Exception | None = None) -> None:
        self.sql = ""
        self.parameters: dict = {}
        self._error = error

    def query(self, sql, parameters, settings):
        self.sql = sql
        self.parameters = parameters
        if self._error is not None:
            raise self._error
        return SimpleNamespace(column_names=["n"], result_rows=[])
