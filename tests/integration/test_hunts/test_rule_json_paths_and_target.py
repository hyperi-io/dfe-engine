# Project:   DFE Engine
# File:      tests/integration/test_hunts/test_rule_json_paths_and_target.py
# Purpose:   The unseen _json path warning and the hunt target check, against a real ClickHouse
#
# License:   BUSL-1.1
# Copyright: (c) 2026 HYPERI PTY LIMITED

"""A ``_json`` path recent rows never carried warns, and a results table lacking a runner column is named."""

import json
import uuid

import pytest

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.rule_creation_service import RuleCreateRequest, RuleCreationService
from dfe_engine.settings import DetectionGuardSettings, HuntsSettings

pytestmark = pytest.mark.integration


@pytest.fixture
def database(ch_client):
    name = f"dfe_rule_paths_{uuid.uuid4().hex[:8]}"
    ch_client.command(f"CREATE DATABASE `{name}`")
    yield name
    ch_client.command(f"DROP DATABASE IF EXISTS `{name}` SYNC")


@pytest.fixture
def events(ch_client, database):
    """An ``events`` source table holding rows that carry ``user.name`` and ``attempts``."""
    ch_client.command(
        f"CREATE TABLE `{database}`.`events` (_timestamp_load DateTime DEFAULT now(), "
        "_org_id String, host String, _json JSON) ENGINE = MergeTree ORDER BY host"
    )
    payload = json.dumps({"user": {"name": "a"}, "attempts": 3})
    ch_client.command(
        f"INSERT INTO `{database}`.`events` (_org_id, host, _json) "
        f"SELECT 't1', 'h1', '{payload}'::JSON FROM numbers(5)"
    )
    return database


def _service(ch_client, database: str) -> RuleCreationService:
    hunts = HuntsSettings(detection_guard=DetectionGuardSettings(enabled=False))
    return RuleCreationService(
        ch_client=ch_client, data_database=database, hunts=hunts, is_source={"events"}.__contains__
    )


def _warnings(service: RuleCreationService, where: str, **kwargs) -> list[str]:
    result = service.create_rule(
        RuleCreateRequest(name="Rule", source="events", where_clause=where, **kwargs), "rule"
    )
    assert result.sql_errors == []
    return result.json_path_warnings


@pytest.mark.parametrize(
    ("where", "unseen"),
    [
        pytest.param("_json.user.name = 'a'", None, id="a path rows carry"),
        pytest.param("_json.user = 'a'", None, id="an object above a path rows carry"),
        pytest.param("_json.attempts > 2 AND host = 'h1'", None, id="a second carried path"),
        pytest.param("_json.user.nmae = 'a'", "_json.user.nmae", id="a misspelt path"),
        pytest.param("_json.user.name = 'a' AND _json.nope > 1", "_json.nope", id="one of two"),
    ],
)
def test_a_path_the_recent_rows_never_carried_warns(ch_client, events, where, unseen):
    warnings = _warnings(_service(ch_client, events), where)

    if unseen is None:
        assert warnings == []
    else:
        [warning] = warnings
        assert warning.startswith(f"{unseen} did not appear in the last 60 minutes")


def test_a_fence_that_holds_no_row_leaves_the_paths_unchecked(ch_client, events):
    service = _service(ch_client, events)

    request = RuleCreateRequest(name="Rule", source="events", where_clause="_json.nope = 1")
    fenced = service.create_rule(request, "rule", tenant_ids=["some-other-org"])

    assert fenced.json_path_warnings == []


@pytest.fixture
def results_tables(ch_client, database):
    """``detection`` with every column the runner inserts, and ``short`` missing the last two."""
    columns = HuntResultSchema().insert_columns()
    for table, held in (("detection", columns), ("short", columns[:-2])):
        listed = ", ".join(f"`{name}` String" for name in held)
        ch_client.command(
            f"CREATE TABLE `{database}`.`{table}` ({listed}) ENGINE = MergeTree ORDER BY tuple()"
        )
    return columns


def test_the_target_check_reads_system_columns(ch_client, database, results_tables):
    service = _service(ch_client, database)

    assert service.target_error(database, "detection") is None
    assert service.target_error(database, "short") == (
        f"{database}.short lacks the columns the runner writes: {', '.join(results_tables[-2:])}"
    )
    assert service.target_error(database, "absent") == (
        f"{database}.absent does not exist, so the runner has nowhere to write detections"
    )
