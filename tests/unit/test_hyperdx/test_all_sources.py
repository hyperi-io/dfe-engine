#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_all_sources.py
#  Purpose:      The all_sources HyperDX source and the registration that retries it
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The all_sources HyperDX source points every team at the Merge table, and its registration backs off while HyperDX stays away."""

from dfe_engine.hyperdx.all_sources import register_all_sources
from tests.unit.test_hyperdx.factories import (
    make_all_sources_registration,
    make_exploding_hyperdx,
    make_fake_hyperdx,
)

NOT_REGISTERED = "HyperDX all_sources source not registered; trying again until it is"
REGISTERED = "HyperDX all_sources source registered"


def _registration_events(*, events: list[dict]) -> list[str]:
    """The registration's own log lines, in order."""
    return [entry["event"] for entry in events if entry["event"] in (NOT_REGISTERED, REGISTERED)]


class TestRegisterAllSources:
    async def test_points_every_team_at_the_merge_table(self):
        client = make_fake_hyperdx()

        await register_all_sources(client=client, database="dfe")

        assert client.sources == {
            "all_sources": {
                "kind": "log",
                "from": {"databaseName": "dfe", "tableName": "all_sources"},
                "timestampValueExpression": "_timestamp_load",
                "displayedTimestampValueExpression": "_timestamp_load",
                "implicitColumnExpression": "_json",
                "bodyExpression": "_json",
                "defaultTableSelectExpression": "_timestamp_load,_json",
            }
        }


class TestAllSourcesRegistration:
    class TestAttempt:
        async def test_registers_once_hyperdx_answers(self):
            client = make_fake_hyperdx(reachable=False)
            registration = make_all_sources_registration(client=client)

            await registration.attempt()
            client.reachable = True
            registered = await registration.attempt()

            assert (registered, list(client.sources)) == (True, ["all_sources"])

        async def test_logs_only_when_the_outcome_changes(self, audit_events: list[dict]):
            client = make_fake_hyperdx(reachable=False)
            registration = make_all_sources_registration(client=client)

            for _attempt in range(3):
                await registration.attempt()
            client.reachable = True
            await registration.attempt()

            assert _registration_events(events=audit_events) == [NOT_REGISTERED, REGISTERED]

        async def test_a_raising_client_is_a_failed_attempt(self):
            registration = make_all_sources_registration(client=make_exploding_hyperdx())

            assert await registration.attempt() is False

    class TestRun:
        async def test_doubles_the_wait_up_to_the_cap(self):
            async def sleep(seconds: float) -> None:
                waits.append(seconds)
                if len(waits) == 6:
                    client.reachable = True

            client = make_fake_hyperdx(reachable=False)
            waits = []
            registration = make_all_sources_registration(
                client=client, first_retry_seconds=60.0, max_retry_seconds=900.0, sleep=sleep
            )

            await registration.run()

            assert (waits, list(client.sources)) == (
                [60.0, 120.0, 240.0, 480.0, 900.0, 900.0],
                ["all_sources"],
            )

        async def test_registered_at_once_never_waits(self):
            async def sleep(seconds: float) -> None:
                waits.append(seconds)

            client = make_fake_hyperdx()
            waits = []
            registration = make_all_sources_registration(client=client, sleep=sleep)

            await registration.run()

            assert (waits, list(client.sources)) == ([], ["all_sources"])
