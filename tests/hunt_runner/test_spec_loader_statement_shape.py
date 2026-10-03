#  Project:      dfe-engine
#  File:         tests/hunt_runner/test_spec_loader_statement_shape.py
#  Purpose:      A hunt's direct query must be one SELECT, or one INSERT INTO a table from one
#  Language:     Python
#
#  License:      BUSL-1.1
#  Copyright:    (c) 2026 HYPERI PTY LIMITED
"""A direct ``query`` that would change or drop data drops its hunt, logged.

The runner sends the query to ClickHouse as written, as a user that may write, and
the function guard only refuses calls, so ``DROP TABLE`` and ``ALTER ... DELETE``
passed it. A hunt writes its detections with ``INSERT INTO <table> SELECT``, so
that shape, and a plain SELECT, still load.
"""

from pathlib import Path

import pytest
from scalo.logger import logger

from dfe_engine.hunt_runner.models import HuntStatement
from dfe_engine.hunt_runner.spec_loader import load_specs
from dfe_engine.yaml_utils import yaml_dump_string

REFUSED = {
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


@pytest.mark.parametrize(("query", "reason"), REFUSED.values(), ids=list(REFUSED))
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


def test_a_refused_query_leaves_the_other_hunts_loading(tmp_path: Path):
    _write(tmp_path, "mutate", REFUSED["drop table"][0])
    _write(tmp_path, "reads", ACCEPTED["insert select"])

    specs, errors = _load(tmp_path)

    assert set(specs) == {"reads"}
    [message] = errors
    assert "skipping hunt mutate" in message
