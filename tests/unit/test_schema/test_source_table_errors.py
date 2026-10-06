#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_source_table_errors.py
#  Purpose:      One source whose build the type registry refuses never stops the others
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A header profile naming a type the registry lacks fails one source, not the request.

The build raises the registry's own error, not a schema build error. Pinning the
defaults commits before any table is touched, so that error has to come back as
that source's outcome; the source pass has to count it and carry on.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dfe_engine.schema.retention import apply_pinned_defaults, reconcile_source_ttls
from dfe_engine.source.deployment import SchemaDeployResult, SourceDeploymentStore
from dfe_engine.source.models import Source

_PROFILE = """\
current: "1.0.0"
versions:
  "1.0.0":
    date: "2026-10-07"
    type: revision
    summary: "a column whose type the registry does not have"
    columns:
      - name: _timestamp_load
        type: timestamp
        order: 0
      - name: _odd
        type: no_such_primitive
"""


class _Result:
    def __init__(self, rows: list) -> None:
        self.result_rows = rows


class _Server:
    """Every table named in *tables* exists as a MergeTree with no TTL; nothing else does."""

    def __init__(self, tables: list[str]) -> None:
        self._tables = tables
        self.commands: list[str] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        if "SELECT name, engine, engine_full FROM system.tables" in sql:
            return _Result(
                [(name, "MergeTree", "MergeTree ORDER BY tuple()") for name in self._tables]
            )
        if "SELECT 1 FROM system.tables" in sql:
            wanted = (parameters or {}).get("tbl")
            return _Result([[1]] if wanted in self._tables else [])
        return _Result([])

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.commands.append(sql)


@pytest.fixture
def broken_profile(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "common-header").mkdir()
    (tmp_path / "common-header" / "broken.yaml").write_text(_PROFILE, encoding="utf-8")
    monkeypatch.setenv("DFE_SCHEMAS_DIR", str(tmp_path))


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        clickhouse=SimpleNamespace(
            default_engine="MergeTree",
            default_ttl_days=90,
            effective_data_database="dfe",
            topology="single",
        ),
        schemas=SimpleNamespace(schemas_dir=""),
        source=SimpleNamespace(
            sources_dir=str(tmp_path / "sources"),
            builds_dir=None,
            plans_dir=None,
            deploys_dir=None,
        ),
    )


def _deployed(name: str) -> Source:
    return Source.model_validate(
        {
            "source": name,
            "current": "1.0.0",
            "deployed_version": "1.0.0",
            "versions": {
                "1.0.0": {
                    "date_time": "2026-10-07",
                    "match": {"field": "f", "value": name},
                    "header": {"type": "broken", "version": "1.0.0"},
                    "schema": {"ttl_days": 30, "engine": "MergeTree"},
                }
            },
        }
    )


def test_a_type_the_registry_lacks_fails_only_that_source(broken_profile, tmp_path):
    server = _Server(["odd"])

    outcomes = apply_pinned_defaults(
        lambda: server, settings=_settings(tmp_path), sources=[_deployed("odd")]
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == "failed"
    assert "no_such_primitive" in outcomes[0].reason
    assert server.commands == []


def test_the_source_pass_counts_it_skipped_and_carries_on(broken_profile, tmp_path):
    settings = _settings(tmp_path)
    store = SourceDeploymentStore.from_settings(settings)
    sources = [_deployed("odd"), _deployed("absent")]
    for source in sources:
        store.save_deploy(
            SchemaDeployResult(
                source_name=source.source,
                version="1.0.0",
                dry_run=False,
                applied=True,
                create_table="",
            ),
            source,
        )

    outcome = reconcile_source_ttls(_Server(["odd"]), settings=settings, sources=sources)

    assert outcome.sources_skipped == 2
    assert outcome.sources_reconciled == 0
