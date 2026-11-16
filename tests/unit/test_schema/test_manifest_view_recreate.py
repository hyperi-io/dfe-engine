#  Project:      dfe-engine
#  File:         tests/unit/test_schema/test_manifest_view_recreate.py
#  Purpose:      A manifest materialized view whose SELECT changed is dropped and created again
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A live materialized view is brought to the SELECT its definition now renders.

Every manifest view writes TO a table, so the rows outlive the view and a changed
SELECT costs nothing but the view itself. A fake client stands in for ClickHouse
and records every statement it is told to run.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest
from dfe_schemas.clickhouse import Platform
from dfe_schemas.render import RenderedObject

from dfe_engine.schema.applier import ManifestApplier
from dfe_engine.schema.ledger import LedgerRow

_CREATE = (
    "CREATE MATERIALIZED VIEW IF NOT EXISTS `dfe`.`query_log_archive_mv` "
    "TO `dfe`.`query_log_archive` AS\nSELECT event_time, query_id FROM system.query_log"
)


@dataclass
class _Result:
    result_rows: list


@dataclass
class _LiveView:
    """A view that exists. Every statement is recorded."""

    statements: list[str] = field(default_factory=list)

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Result:
        return _Result([[1]])

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        self.statements.append(sql)


class _Ledger:
    def __init__(self, rows: dict[tuple[str, str], LedgerRow]) -> None:
        self._rows = rows

    def checksums(self) -> dict[tuple[str, str], LedgerRow]:
        return dict(self._rows)


def _rendered(checksum: str) -> RenderedObject:
    return RenderedObject(
        id="data.query_log_archive_mv",
        kind="materialized_view",
        database="dfe",
        name="query_log_archive_mv",
        platform="self_hosted",
        additive=True,
        optional=False,
        statements=(_CREATE,),
        checksum=checksum,
    )


def _recorded(checksum: str) -> LedgerRow:
    return LedgerRow(
        database="dfe",
        object="query_log_archive_mv",
        kind="materialized_view",
        schemas_version="0.2.30",
        engine_version="1.22.91",
        checksum=checksum,
        action="created",
        platform="self_hosted",
    )


@pytest.mark.parametrize(
    ("recorded", "action", "statements"),
    [
        pytest.param(
            "old",
            "altered",
            [
                "ALTER TABLE `dfe`.`query_log_archive_mv` MODIFY QUERY "
                "SELECT event_time, query_id FROM system.query_log"
            ],
            id="changed-select-is-swapped-in-place",
        ),
        pytest.param("new", "unchanged", [], id="same-select-is-left-alone"),
        pytest.param(None, "unchanged", [], id="unrecorded-live-view-is-left-alone"),
    ],
)
def test_a_live_view_follows_its_select(recorded, action, statements):
    client = _LiveView()
    applier = ManifestApplier(client, Platform.SELF_HOSTED, schemas_version="0.2.30")
    rows = {("dfe", "query_log_archive_mv"): _recorded(recorded)} if recorded else {}
    applier.attach_ledger(_Ledger(rows))  # ty: ignore[invalid-argument-type]  # a stand-in carrying only checksums()

    outcome = applier.apply(_rendered("new"))

    assert outcome.action == action
    assert client.statements == statements
