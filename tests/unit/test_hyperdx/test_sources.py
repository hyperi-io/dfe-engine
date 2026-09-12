#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_sources.py
#  Purpose:      Guard the HyperDX source a DFE source deploy creates and a delete removes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A deploy points HyperDX at the table it made, and nothing about that may fail it.

Two things are worth breaking the build over. A re-deploy that CREATES rather than
updates leaves a team with two sources of the same name and a UI that picks one at
random, and HyperDX being down or absent must cost the deploy nothing -- the
schema, the topics and the app routing are all already live by then.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dfe_engine.api.v1.sources import _remove_hyperdx_source, _sync_hyperdx_source
from dfe_engine.hyperdx.sources import (
    TEMPLATE_SOURCE_NAME,
    ensure_source,
    remove_source,
    source_spec,
    timestamp_column,
)

# The default `timeseries` header profile; `passthrough` is the one without an
# event-time column at all.
TIMESERIES_COLUMNS = ["_timestamp_load", "_timestamp", "_uuid", "_org_id", "_source", "_json"]
PASSTHROUGH_COLUMNS = ["_timestamp_load", "_uuid", "_org_id", "_json"]

CONNECTION_ID = "65f0000000000000000000aa"


def seeded_template() -> dict[str, Any]:
    """The source the fork seeds on every team, as ``GET /sources`` returns it."""
    return {
        "id": "65f0000000000000000000b1",
        "name": TEMPLATE_SOURCE_NAME,
        "kind": "log",
        "connection": CONNECTION_ID,
        "from": {"databaseName": "dfe", "tableName": "default"},
        "timestampValueExpression": "_timestamp",
    }


class FakeHyperDX:
    """The fork's ``/sources`` surface in memory, with the client's failure contract.

    The real client is non-fatal: every call returns None/False rather than
    raising, so ``reachable=False`` is what an unreachable HyperDX looks like to
    a caller.
    """

    def __init__(
        self,
        sources: list[dict[str, Any]] | None = None,
        connections: list[dict[str, Any]] | None = None,
        *,
        reachable: bool = True,
    ) -> None:
        self.sources = list(sources or [])
        self.connections = list(connections or [])
        self.reachable = reachable
        self.creates = 0
        self.updates = 0
        self.deletes = 0
        self._serial = 0

    async def list_sources(self) -> list[dict[str, Any]] | None:
        return list(self.sources) if self.reachable else None

    async def list_connections(self) -> list[dict[str, Any]] | None:
        return list(self.connections) if self.reachable else None

    async def create_source(self, source: dict[str, Any]) -> dict[str, Any] | None:
        if not self.reachable:
            return None
        self.creates += 1
        self._serial += 1
        created = {**source, "id": f"65f00000000000000000{self._serial:04d}"}
        self.sources.append(created)
        return created

    async def update_source(self, source_id: str, source: dict[str, Any]) -> bool:
        if not self.reachable:
            return False
        self.updates += 1
        for index, existing in enumerate(self.sources):
            if existing.get("id") == source_id:
                self.sources[index] = {**source, "id": source_id}
                return True
        return False

    async def delete_source(self, source_id: str) -> bool:
        if not self.reachable:
            return False
        self.deletes += 1
        remaining = [s for s in self.sources if s.get("id") != source_id]
        dropped = len(remaining) < len(self.sources)
        self.sources = remaining
        return dropped


def request_with(client: Any) -> SimpleNamespace:
    """A stand-in for the FastAPI request the router helpers read state off."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hyperdx_client=client)))


def dfe_source(name: str) -> SimpleNamespace:
    """The two attributes the router helper reads off a Source."""
    return SimpleNamespace(source=name, table_name=name)


# ---------------------------------------------------------------------------
# timestamp_column
# ---------------------------------------------------------------------------


class TestTimestampColumn:
    def test_prefers_the_event_time(self):
        assert timestamp_column(TIMESERIES_COLUMNS) == "_timestamp"

    def test_falls_back_to_the_insertion_time(self):
        # passthrough carries no _timestamp, and a source with an empty
        # timestamp expression is rejected by the fork outright.
        assert timestamp_column(PASSTHROUGH_COLUMNS) == "_timestamp_load"

    def test_no_timestamp_column_at_all(self):
        assert timestamp_column(["_uuid", "_json"]) is None


# ---------------------------------------------------------------------------
# source_spec
# ---------------------------------------------------------------------------


class TestSourceSpec:
    def test_mirrors_the_seeded_template(self):
        spec = source_spec(
            name="filebeat",
            database="dfe",
            table="filebeat",
            connection_id=CONNECTION_ID,
            timestamp="_timestamp",
        )
        assert spec["name"] == "filebeat"
        assert spec["kind"] == "log"
        assert spec["connection"] == CONNECTION_ID
        assert spec["from"] == {"databaseName": "dfe", "tableName": "filebeat"}
        assert spec["timestampValueExpression"] == "_timestamp"
        assert spec["displayedTimestampValueExpression"] == "_timestamp"

    def test_surfaces_the_structured_payload_not_the_raw_text(self):
        # _raw is captured only when asked for and may be NULL, so it is never
        # the body, the implicit search column or the default view.
        spec = source_spec(
            name="filebeat",
            database="dfe",
            table="filebeat",
            connection_id=CONNECTION_ID,
            timestamp="_timestamp",
        )
        assert spec["bodyExpression"] == "_json"
        assert spec["implicitColumnExpression"] == "_json"
        assert spec["defaultTableSelectExpression"] == "_timestamp,_json"


# ---------------------------------------------------------------------------
# ensure_source
# ---------------------------------------------------------------------------


class TestEnsureSource:
    async def test_creates_the_source_on_deploy(self):
        client = FakeHyperDX(sources=[seeded_template()])

        source_id = await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert source_id
        assert client.creates == 1
        created = next(s for s in client.sources if s["name"] == "filebeat")
        assert created["from"] == {"databaseName": "dfe", "tableName": "filebeat"}
        assert created["timestampValueExpression"] == "_timestamp"

    async def test_takes_the_connection_from_the_seeded_template(self):
        # Every DFE source on a team has to hang off the one connection the
        # fork seeded, or the fork refuses it as another team's.
        other = {**seeded_template(), "id": "other", "name": "hunts", "connection": "wrong"}
        client = FakeHyperDX(sources=[other, seeded_template()])

        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        created = next(s for s in client.sources if s["name"] == "filebeat")
        assert created["connection"] == CONNECTION_ID

    async def test_falls_back_to_the_teams_connection_list(self):
        client = FakeHyperDX(sources=[], connections=[{"id": CONNECTION_ID, "name": "platform"}])

        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        created = next(s for s in client.sources if s["name"] == "filebeat")
        assert created["connection"] == CONNECTION_ID

    async def test_redeploy_updates_and_never_duplicates(self):
        client = FakeHyperDX(sources=[seeded_template()])

        first = await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )
        second = await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert first == second
        assert client.creates == 1
        assert client.updates == 1
        assert [s["name"] for s in client.sources].count("filebeat") == 1

    async def test_redeploy_moves_the_source_to_the_new_table(self):
        client = FakeHyperDX(sources=[seeded_template()])
        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        await ensure_source(
            client,
            name="filebeat",
            database="dfe_other",
            table="filebeat",
            columns=PASSTHROUGH_COLUMNS,
        )

        current = next(s for s in client.sources if s["name"] == "filebeat")
        assert current["from"]["databaseName"] == "dfe_other"
        assert current["timestampValueExpression"] == "_timestamp_load"

    async def test_unreachable_hyperdx_writes_nothing(self):
        client = FakeHyperDX(sources=[seeded_template()], reachable=False)

        source_id = await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert source_id is None
        assert client.creates == 0
        assert client.updates == 0

    async def test_no_connection_to_hang_it_on(self):
        client = FakeHyperDX(sources=[], connections=[])

        assert (
            await ensure_source(
                client,
                name="filebeat",
                database="dfe",
                table="filebeat",
                columns=TIMESERIES_COLUMNS,
            )
            is None
        )
        assert client.creates == 0

    async def test_no_timestamp_column(self):
        client = FakeHyperDX(sources=[seeded_template()])

        assert (
            await ensure_source(
                client, name="filebeat", database="dfe", table="filebeat", columns=["_uuid"]
            )
            is None
        )
        assert client.creates == 0


# ---------------------------------------------------------------------------
# remove_source
# ---------------------------------------------------------------------------


class TestRemoveSource:
    async def test_removes_the_source_on_delete(self):
        client = FakeHyperDX(sources=[seeded_template()])
        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert await remove_source(client, name="filebeat") is True
        assert client.deletes == 1
        assert [s["name"] for s in client.sources] == [TEMPLATE_SOURCE_NAME]

    async def test_leaves_the_seeded_sources_alone(self):
        client = FakeHyperDX(sources=[seeded_template()])

        await remove_source(client, name="filebeat")

        assert client.deletes == 0
        assert [s["name"] for s in client.sources] == [TEMPLATE_SOURCE_NAME]

    async def test_absent_source_is_already_gone(self):
        client = FakeHyperDX(sources=[seeded_template()])
        assert await remove_source(client, name="filebeat") is True

    async def test_unreachable_hyperdx_reports_failure(self):
        client = FakeHyperDX(sources=[seeded_template()], reachable=False)
        assert await remove_source(client, name="filebeat") is False
        assert client.deletes == 0


# ---------------------------------------------------------------------------
# Router helpers: a HyperDX fault never fails a source write
# ---------------------------------------------------------------------------


class Exploding:
    """A client that raises rather than returning the non-fatal None."""

    async def list_sources(self):
        raise RuntimeError("connection refused")


class TestRouterHelpers:
    async def test_deploy_reports_the_new_source(self):
        client = FakeHyperDX(sources=[seeded_template()])

        source_id, error = await _sync_hyperdx_source(
            request_with(client), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert source_id
        assert error is None

    async def test_deploy_survives_an_unreachable_hyperdx(self):
        client = FakeHyperDX(sources=[seeded_template()], reachable=False)

        source_id, error = await _sync_hyperdx_source(
            request_with(client), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert source_id is None
        assert error

    async def test_deploy_survives_a_raising_client(self):
        source_id, error = await _sync_hyperdx_source(
            request_with(Exploding()), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert source_id is None
        assert "connection refused" in error

    async def test_deployment_without_hyperdx_reports_nothing(self):
        source_id, error = await _sync_hyperdx_source(
            request_with(None), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert (source_id, error) == (None, None)

    async def test_delete_survives_a_raising_client(self):
        await _remove_hyperdx_source(request_with(Exploding()), "filebeat")

    async def test_delete_without_hyperdx(self):
        await _remove_hyperdx_source(request_with(None), "filebeat")


@pytest.mark.parametrize("columns", [TIMESERIES_COLUMNS, PASSTHROUGH_COLUMNS])
async def test_every_core_header_profile_yields_a_source(columns):
    client = FakeHyperDX(sources=[seeded_template()])
    assert await ensure_source(
        client, name="filebeat", database="dfe", table="filebeat", columns=columns
    )
