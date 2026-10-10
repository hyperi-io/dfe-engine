#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_sources.py
#  Purpose:      Guard the HyperDX source a DFE source deploy creates and a delete removes
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""A deploy points HyperDX at the table it made, and nothing about that may fail it.

Two things are worth breaking the build over. The write must go to the fork's
cross-team route rather than the team-scoped ``/sources`` surface -- the engine's
own team holds no connection and no humans, so a source written there reaches
nobody. And HyperDX being down or absent must cost the deploy nothing: the
schema, the topics and the app routing are all already live by then.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dfe_engine.api.v1.sources import _remove_hyperdx_source, _sync_hyperdx_source
from dfe_engine.hyperdx.sources import (
    ensure_source,
    list_sources_by_team,
    remove_source,
    source_spec,
    timestamp_column,
)

# The default `timeseries` header profile; `passthrough` is the one without an
# event-time column at all.
TIMESERIES_COLUMNS = ["_timestamp_load", "_timestamp", "_uuid", "_org_id", "_source", "_json"]
PASSTHROUGH_COLUMNS = ["_timestamp_load", "_uuid", "_org_id", "_json"]

# Two human teams plus the engine's own service team, which has no connection.
HUMAN_TEAMS = ["dfe-admins", "customer-acme"]
SERVICE_TEAM = "dfe"


class FakeHyperDX:
    """The fork's ``/dfe/sources`` surface in memory, with the client's failure contract.

    The real client is non-fatal: every call returns None rather than raising, so
    ``reachable=False`` is what an unreachable HyperDX looks like to a caller.
    """

    def __init__(self, *, reachable: bool = True, owned: bool = True) -> None:
        self.reachable = reachable
        # False stands for the fork's 409: the name belongs to its seeded set.
        self.owned = owned
        self.sources: dict[str, dict[str, Any]] = {}
        self.puts = 0
        self.deletes = 0

    async def put_dfe_source(self, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        if not self.reachable or not self.owned:
            return None
        self.puts += 1
        self.sources[name] = spec
        return {"name": name, "written": list(HUMAN_TEAMS), "skipped": [SERVICE_TEAM]}

    async def delete_dfe_source(self, name: str) -> dict[str, Any] | None:
        if not self.reachable:
            return None
        self.deletes += 1
        removed = HUMAN_TEAMS if self.sources.pop(name, None) is not None else []
        return {"name": name, "removed": list(removed)}

    async def list_dfe_sources(self) -> dict[str, Any] | None:
        if not self.reachable:
            return None
        return {
            "teams": [
                {
                    "team": f"id-{team}",
                    "teamName": team,
                    "sources": [
                        {"id": f"{team}-{name}", "name": name, "from": spec["from"]}
                        for name, spec in self.sources.items()
                    ],
                }
                for team in HUMAN_TEAMS
            ]
        }


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
    def test_mirrors_the_seeded_landing_source(self):
        spec = source_spec(database="dfe", table="filebeat", timestamp="_timestamp")

        assert spec["kind"] == "log"
        assert spec["from"] == {"databaseName": "dfe", "tableName": "filebeat"}
        assert spec["timestampValueExpression"] == "_timestamp"
        assert spec["displayedTimestampValueExpression"] == "_timestamp"

    def test_carries_no_name_and_no_connection(self):
        # The name is the path segment and the connection is resolved per team;
        # sending either would pin every team to one team's connection.
        spec = source_spec(database="dfe", table="filebeat", timestamp="_timestamp")

        assert "name" not in spec
        assert "connection" not in spec

    def test_surfaces_the_structured_payload_not_the_raw_text(self):
        # _raw is captured only when asked for and may be NULL, so it is never
        # the body, the implicit search column or the default view.
        spec = source_spec(database="dfe", table="filebeat", timestamp="_timestamp")

        assert spec["bodyExpression"] == "_json"
        assert spec["implicitColumnExpression"] == "_json"
        assert spec["defaultTableSelectExpression"] == "_timestamp,_json"


# ---------------------------------------------------------------------------
# ensure_source
# ---------------------------------------------------------------------------


class TestEnsureSource:
    async def test_reports_every_team_the_source_landed_on(self):
        client = FakeHyperDX()

        teams = await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert teams == HUMAN_TEAMS
        assert client.puts == 1

    async def test_writes_the_table_the_deploy_just_made(self):
        client = FakeHyperDX()

        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert client.sources["filebeat"]["from"] == {
            "databaseName": "dfe",
            "tableName": "filebeat",
        }

    async def test_a_redeploy_moves_the_source_to_the_new_table(self):
        client = FakeHyperDX()
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

        current = client.sources["filebeat"]
        assert current["from"]["databaseName"] == "dfe_other"
        assert current["timestampValueExpression"] == "_timestamp_load"

    async def test_unreachable_hyperdx_writes_nothing(self):
        client = FakeHyperDX(reachable=False)

        teams = await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert teams is None
        assert client.puts == 0

    async def test_a_name_the_fork_seeded_is_refused(self):
        # The fork 409s a name it seeded itself (`main`, `hunts`, the otel set);
        # replacing it would retarget the landing view of every team.
        client = FakeHyperDX(owned=False)

        assert (
            await ensure_source(
                client, name="main", database="dfe", table="main", columns=TIMESERIES_COLUMNS
            )
            is None
        )

    async def test_no_timestamp_column_never_reaches_the_fork(self):
        client = FakeHyperDX()

        assert (
            await ensure_source(
                client, name="filebeat", database="dfe", table="filebeat", columns=["_uuid"]
            )
            is None
        )
        assert client.puts == 0


# ---------------------------------------------------------------------------
# remove_source
# ---------------------------------------------------------------------------


class TestRemoveSource:
    async def test_removes_the_source_on_delete(self):
        client = FakeHyperDX()
        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        assert await remove_source(client, name="filebeat") is True
        assert client.sources == {}

    async def test_absent_source_is_already_gone(self):
        client = FakeHyperDX()

        assert await remove_source(client, name="filebeat") is True

    async def test_unreachable_hyperdx_reports_failure(self):
        client = FakeHyperDX(reachable=False)

        assert await remove_source(client, name="filebeat") is False
        assert client.deletes == 0


# ---------------------------------------------------------------------------
# list_sources_by_team
# ---------------------------------------------------------------------------


class TestListSourcesByTeam:
    async def test_reports_the_source_on_each_human_team(self):
        client = FakeHyperDX()
        await ensure_source(
            client, name="filebeat", database="dfe", table="filebeat", columns=TIMESERIES_COLUMNS
        )

        listing = await list_sources_by_team(client)

        assert [entry["teamName"] for entry in listing] == HUMAN_TEAMS
        assert [source["name"] for source in listing[0]["sources"]] == ["filebeat"]

    async def test_unreachable_hyperdx_is_not_an_empty_listing(self):
        # An empty list reads as "the source is missing", which is a different
        # fault from "HyperDX did not answer".
        assert await list_sources_by_team(FakeHyperDX(reachable=False)) is None


# ---------------------------------------------------------------------------
# Router helpers: a HyperDX fault never fails a source write
# ---------------------------------------------------------------------------


class Exploding:
    """A client that raises rather than returning the non-fatal None."""

    async def put_dfe_source(self, name, spec):
        raise RuntimeError("connection refused")

    async def delete_dfe_source(self, name):
        raise RuntimeError("connection refused")


class TestRouterHelpers:
    async def test_deploy_reports_how_many_teams_carry_the_source(self):
        client = FakeHyperDX()

        teams, error = await _sync_hyperdx_source(
            request_with(client), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert teams == len(HUMAN_TEAMS)
        assert error is None

    async def test_deploy_survives_an_unreachable_hyperdx(self):
        client = FakeHyperDX(reachable=False)

        teams, error = await _sync_hyperdx_source(
            request_with(client), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert teams is None
        assert error

    async def test_deploy_survives_a_raising_client(self):
        teams, error = await _sync_hyperdx_source(
            request_with(Exploding()), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert teams is None
        assert "Search did not accept the source" in error
        assert "connection refused" not in error

    async def test_deployment_without_hyperdx_reports_nothing(self):
        teams, error = await _sync_hyperdx_source(
            request_with(None), dfe_source("filebeat"), "dfe", TIMESERIES_COLUMNS
        )

        assert (teams, error) == (None, None)

    async def test_delete_survives_a_raising_client(self):
        await _remove_hyperdx_source(request_with(Exploding()), "filebeat")

    async def test_delete_without_hyperdx(self):
        await _remove_hyperdx_source(request_with(None), "filebeat")


@pytest.mark.parametrize("columns", [TIMESERIES_COLUMNS, PASSTHROUGH_COLUMNS])
async def test_every_core_header_profile_yields_a_source(columns):
    client = FakeHyperDX()

    assert await ensure_source(
        client, name="filebeat", database="dfe", table="filebeat", columns=columns
    )
