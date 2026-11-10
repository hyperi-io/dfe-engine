#  Project:      dfe-engine
#  File:         tests/unit/test_hyperdx/test_engine_sources.py
#  Purpose:      The engine's own HyperDX sources and the registration that retries them
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED

"""The all_sources and dlq HyperDX sources point every team at their tables, and their registration backs off while HyperDX stays away."""

import pytest

from dfe_engine.hyperdx.engine_sources import register_all_sources, register_dlq_source
from tests.unit.test_hyperdx.factories import (
    make_engine_sources_registration,
    make_exploding_hyperdx,
    make_fake_hyperdx,
)

NOT_REGISTERED = "HyperDX engine sources not registered, trying again until they are"
REGISTERED = "HyperDX engine sources registered"
ENGINE_SOURCES = ["all_sources", "dlq"]


def _registration_events(*, events: list[dict]) -> list[str]:
    """The registration's own log lines, in order."""
    return [entry["event"] for entry in events if entry["event"] in (NOT_REGISTERED, REGISTERED)]


def _log_spec(*, database: str, table: str, timestamp: str) -> dict:
    """The fork source body every engine source shares, over one table."""
    return {
        "kind": "log",
        "from": {"databaseName": database, "tableName": table},
        "timestampValueExpression": timestamp,
        "displayedTimestampValueExpression": timestamp,
        "implicitColumnExpression": "_json",
        "bodyExpression": "_json",
        "defaultTableSelectExpression": f"{timestamp},_json",
    }


@pytest.mark.parametrize(
    ("register", "name", "timestamp"),
    [
        (register_all_sources, "all_sources", "_timestamp_load"),
        (register_dlq_source, "dlq", "_timestamp"),
    ],
)
async def test_each_source_points_every_team_at_its_table(register, name, timestamp):
    client = make_fake_hyperdx()

    await register(client=client, database="dfe_prod")

    assert client.sources == {name: _log_spec(database="dfe_prod", table=name, timestamp=timestamp)}


class TestEngineSourcesRegistration:
    class TestAttempt:
        async def test_registers_both_once_hyperdx_answers(self):
            client = make_fake_hyperdx(reachable=False)
            registration = make_engine_sources_registration(client=client)

            await registration.attempt()
            client.reachable = True
            registered = await registration.attempt()

            assert (registered, sorted(client.sources)) == (True, ENGINE_SOURCES)

        async def test_logs_only_when_the_outcome_changes(self, audit_events: list[dict]):
            client = make_fake_hyperdx(reachable=False)
            registration = make_engine_sources_registration(client=client)

            for _attempt in range(3):
                await registration.attempt()
            client.reachable = True
            await registration.attempt()

            assert _registration_events(events=audit_events) == [NOT_REGISTERED, REGISTERED]

        async def test_a_raising_client_is_a_failed_attempt(self):
            registration = make_engine_sources_registration(client=make_exploding_hyperdx())

            assert await registration.attempt() is False

    class TestRun:
        async def test_doubles_the_wait_up_to_the_cap(self):
            async def sleep(seconds: float) -> None:
                waits.append(seconds)
                if len(waits) == 6:
                    client.reachable = True

            client = make_fake_hyperdx(reachable=False)
            waits = []
            registration = make_engine_sources_registration(
                client=client, first_retry_seconds=60.0, max_retry_seconds=900.0, sleep=sleep
            )

            await registration.run()

            assert (waits, sorted(client.sources)) == (
                [60.0, 120.0, 240.0, 480.0, 900.0, 900.0],
                ENGINE_SOURCES,
            )

        async def test_registered_at_once_never_waits(self):
            async def sleep(seconds: float) -> None:
                waits.append(seconds)

            client = make_fake_hyperdx()
            waits = []
            registration = make_engine_sources_registration(client=client, sleep=sleep)

            await registration.run()

            assert (waits, sorted(client.sources)) == ([], ENGINE_SOURCES)
