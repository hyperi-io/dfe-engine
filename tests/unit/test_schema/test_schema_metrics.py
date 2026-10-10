#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_schema_metrics.py
#  Purpose:      The schema gauges land as dfe_schema_* on any manager they are given
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""The schema phase's gauge names hold whatever namespace the manager carries.

The daemon hands over its own manager, which carries no namespace, and the
``dfe auto schema`` command builds one carrying the contract's ``dfe``. Both must
expose the same names.
"""

import pytest
from scalo.metrics import create_metrics

from dfe_engine.schema.metrics import SchemaMetrics

NAMES = (
    "dfe_schema_bootstrap_state",
    "dfe_schema_bootstrap_duration_seconds",
    "dfe_schema_objects_refused",
    'dfe_schema_version_info{schemas_version="0.2.9"}',
)


@pytest.mark.parametrize("namespace", ["", "dfe"], ids=["daemon-bare", "contract-dfe"])
def test_the_gauges_land_as_dfe_schema_whatever_the_managers_namespace(namespace):
    manager = create_metrics(
        "schema-metrics-names",
        backend="prometheus",
        enable_auto_update=False,
        metric_prefix=namespace,
    )

    SchemaMetrics(manager).report(state=1, duration_seconds=0.5, schemas_version="0.2.9", refused=0)

    exposition = manager.metrics_text
    rows = [row for row in exposition.splitlines() if not row.startswith("#")]
    for name in NAMES:
        assert any(row.startswith(name) for row in rows), exposition
    assert "dfe_dfe_" not in exposition


def test_no_manager_records_nothing():
    metrics = SchemaMetrics()

    metrics.report(state=1, duration_seconds=0.5, schemas_version="0.2.9", refused=0)

    assert metrics.enabled is False
