#  Project:      dfe-engine
#  File:         tests/integration/test_hunt_lookups.py
#  Purpose:      A rule looks values up in a listed lookup database, on real ClickHouse
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A rule may read a table of ``hunts.lookup_databases``, and nothing that can hold an org's rows.

The lookup here is a list of tenant markers, so a match proves ClickHouse read the
lookup table, and the second org's row in it proves the hunt's own orgs still fence
what lands. The startup check runs against what this server actually holds: a
reference table on each kept engine passes, and a table with ``_org_id``, a view over
the landing table, or a ``Set`` or ``Join`` table does not.
"""

import time
import uuid

import pytest
from common.hunt_files import write_hunt, write_rule

from dfe_engine.hunt_runner import ChCoordinator, HuntRunner, HuntWorker, cli, load_specs
from dfe_engine.hunt_runner.spread import current_fire
from dfe_engine.hunts.lookups import (
    LookupDatabaseError,
    refuse_tenant_lookups,
    refuse_tenant_lookups_if_reachable,
)
from dfe_engine.settings import ClickHouseSettings, DFESettings, HuntsSettings

_INTERVAL = 60


@pytest.fixture
def lookup_db(ch_client):
    """A database holding one reference table of markers, dropped afterwards."""
    db = f"threat_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{db}`")
    ch_client.command(f"CREATE TABLE `{db}`.iocs (ioc String) ENGINE = MergeTree ORDER BY ioc")
    try:
        yield db
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS `{db}`")


def _fire_one(ch_client, dfe_db, tmp_path, hunt: str, lookups: tuple[str, ...]) -> int:
    coord = ChCoordinator(ch_client, database=dfe_db, settle_seconds=0.0, sleep=lambda _s: None)
    coord.ensure_schema()
    specs = load_specs(tmp_path / "hunts", rules_dir=tmp_path / "rules", lookup_databases=lookups)
    runner = HuntRunner(coord, HuntWorker(ch_client, coord), specs, cap=8)
    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    return runner.tick(fire + 1)


def _detections(ch_client, dfe_db, hunt: str) -> list[str]:
    rows = ch_client.query(
        f"SELECT _org_id FROM `{dfe_db}`.detection WHERE hunt_name = {{h:String}}",
        parameters={"h": hunt},
    ).result_rows
    return sorted(row[0] for row in rows)


@pytest.mark.parametrize(
    "condition",
    ["_org_id IN (SELECT ioc FROM `{db}`.iocs)", "_org_id IN `{db}`.iocs"],
    ids=["subquery", "table"],
)
def test_a_hunt_matches_through_a_listed_lookup_and_only_for_its_orgs(
    ch_client, dfe_db, lookup_db, tmp_path, condition
):
    hunt = f"lookup_hunt_{uuid.uuid4().hex[:8]}"
    tag = uuid.uuid4().hex[:8]
    named, other, unlisted = f"org-a-{tag}", f"org-b-{tag}", f"org-c-{tag}"
    ch_client.command(f"INSERT INTO `{lookup_db}`.iocs VALUES ('{named}'), ('{other}')")
    write_rule(tmp_path / "rules", f"{hunt}_rule", condition.format(db=lookup_db))
    write_hunt(tmp_path / "hunts", hunt, f"{hunt}_rule", dfe_db, customers=[named, unlisted])

    fire = current_fire(hunt, _INTERVAL, int(time.time()))
    values = ", ".join(
        f"(toDateTime64({fire - 1}, 3), toDateTime64({fire - 1}, 3), '{tenant}')"
        for tenant in (named, other, unlisted)
    )
    ch_client.command(
        f"INSERT INTO `{dfe_db}`.`main` (_timestamp_load, _timestamp, _org_id) VALUES {values}"
    )

    assert _fire_one(ch_client, dfe_db, tmp_path, hunt, (lookup_db,)) == 1

    # The lookup holds the named and the other org, and the hunt reads the named and
    # one the lookup lacks, so only the named org's row is in both.
    assert _detections(ch_client, dfe_db, hunt) == [named]


def test_a_lookup_in_an_unlisted_database_runs_nothing(ch_client, dfe_db, lookup_db, tmp_path):
    hunt = f"lookup_hunt_{uuid.uuid4().hex[:8]}"
    marker = f"org-{uuid.uuid4().hex[:8]}"
    ch_client.command(f"INSERT INTO `{lookup_db}`.iocs VALUES ('{marker}')")
    write_rule(
        tmp_path / "rules", f"{hunt}_rule", f"_org_id IN (SELECT ioc FROM `{lookup_db}`.iocs)"
    )
    write_hunt(tmp_path / "hunts", hunt, f"{hunt}_rule", dfe_db, customers=[marker])

    specs = load_specs(
        tmp_path / "hunts", rules_dir=tmp_path / "rules", lookup_databases=("intel",)
    )

    assert specs[hunt].queries == []


def test_a_listed_database_of_reference_tables_passes_the_check(ch_client, lookup_db):
    refuse_tenant_lookups(ch_client, [lookup_db])


def test_a_listed_database_holding_org_ids_is_refused(ch_client, lookup_db):
    ch_client.command(
        f"CREATE TABLE `{lookup_db}`.sightings (_org_id String, ioc String) "
        "ENGINE = MergeTree ORDER BY ioc"
    )

    with pytest.raises(LookupDatabaseError, match=rf"{lookup_db}\.sightings has an _org_id column"):
        refuse_tenant_lookups(ch_client, [lookup_db])


def test_a_listed_database_holding_a_view_over_the_landing_table_is_refused(
    ch_client, dfe_db, lookup_db
):
    """The view carries no _org_id column of its own, and still reads every org's rows."""
    ch_client.command(
        f"CREATE VIEW `{lookup_db}`.everything AS SELECT toString(_json) AS ioc "
        f"FROM `{dfe_db}`.`main`"
    )

    with pytest.raises(LookupDatabaseError, match=rf"{lookup_db}\.everything is a View table"):
        refuse_tenant_lookups(ch_client, [lookup_db])


@pytest.mark.parametrize(
    "engine",
    [
        "MergeTree ORDER BY ioc",
        "ReplacingMergeTree ORDER BY ioc",
        "Memory",
        "Log",
        "TinyLog",
        "StripeLog",
        "EmbeddedRocksDB PRIMARY KEY ioc",
    ],
)
def test_a_table_answering_from_its_own_rows_passes_the_check(ch_client, lookup_db, engine):
    ch_client.command(f"CREATE TABLE `{lookup_db}`.kept (ioc String) ENGINE = {engine}")
    ch_client.command(f"INSERT INTO `{lookup_db}`.kept VALUES ('x')")

    assert ch_client.query(f"SELECT ioc FROM `{lookup_db}`.kept").result_rows == [("x",)]
    refuse_tenant_lookups(ch_client, [lookup_db])


@pytest.mark.parametrize(
    ("engine", "name"),
    [("Set", "Set"), ("Join(ANY, LEFT, ioc)", "Join")],
    ids=["Set", "Join"],
)
def test_a_set_or_join_table_is_refused(ch_client, lookup_db, engine, name):
    ch_client.command(f"CREATE TABLE `{lookup_db}`.packed (ioc String) ENGINE = {engine}")

    with pytest.raises(LookupDatabaseError, match=rf"{lookup_db}\.packed is a {name} table"):
        refuse_tenant_lookups(ch_client, [lookup_db])


def _runner_settings(ch_params: dict, dfe_db: str, lookups: list[str]) -> DFESettings:
    return DFESettings(
        env="test",
        clickhouse=ClickHouseSettings(
            host=ch_params["host"],
            port=ch_params["port"],
            username=ch_params["username"],
            password=ch_params["password"],
            secure=ch_params["secure"],
            data_database=dfe_db,
        ),
        hunts=HuntsSettings(lookup_databases=lookups),
    )


def test_the_hunt_runner_refuses_to_start_on_a_listed_database_holding_org_ids(
    ch_client, ch_params, dfe_db, lookup_db
):
    ch_client.command(
        f"CREATE TABLE `{lookup_db}`.sightings (_org_id String) ENGINE = MergeTree ORDER BY _org_id"
    )

    with pytest.raises(LookupDatabaseError, match="sightings"):
        cli._connect_coordinator(_runner_settings(ch_params, dfe_db, [lookup_db]))


def test_the_hunt_runner_starts_on_a_listed_database_of_reference_tables(
    ch_params, dfe_db, lookup_db
):
    client, _coord = cli._connect_coordinator(_runner_settings(ch_params, dfe_db, [lookup_db]))
    client.close()


def test_the_engine_refuses_to_start_on_a_listed_database_holding_org_ids(ch_client, lookup_db):
    ch_client.command(
        f"CREATE TABLE `{lookup_db}`.sightings (_org_id String) ENGINE = MergeTree ORDER BY _org_id"
    )

    with pytest.raises(LookupDatabaseError, match="sightings"):
        refuse_tenant_lookups_if_reachable(lambda: ch_client, [lookup_db])
